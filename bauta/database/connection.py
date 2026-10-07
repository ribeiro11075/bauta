from __future__ import annotations

import contextlib
import os
from types import TracebackType
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple, Type

from .driver import Connection, Cursor
from .maintenance import TableMaintenance
from .values import WANTS_COLUMN_TYPES, UniqueKeyClashError, ValuePreparer, decodedJson, prepareParameters, refuseNonFinite
from ..configuration.sqltext import codeOnly
from ..configuration import (WATERMARK_PLACEHOLDER, ConfigurationError, ConnectionConfig, DatabaseConfig, DatabaseType, DuckDBConnection, FilesConnection,
                             IcebergConnection, SQLiteConnection, isSchemePath)
from .dialects import ColumnDefinition, DatabaseDialect, DuckDBDialect, ForeignKey, MariaDBDialect, MSSQLDialect, MySQLDialect, OracleDialect, PostgreSQLDialect, \
    SQLiteDialect, catalogName, quoteFoldedTable, quoteIdentifier, splitTableName, tooLongName

DIALECTS: Dict[DatabaseType, DatabaseDialect] = {
    DatabaseType.MYSQL: MySQLDialect(),
    DatabaseType.ORACLE: OracleDialect(),
    DatabaseType.POSTGRESQL: PostgreSQLDialect(),
    DatabaseType.MSSQL: MSSQLDialect(),
    DatabaseType.SQLITE: SQLiteDialect(),
    DatabaseType.MARIADB: MariaDBDialect(),
    DatabaseType.DUCKDB: DuckDBDialect(),
    }


class RowStream:
    """The chunks of one streamed query, and the cursor they come from.

    A class rather than a generator, whose `finally` is skipped if it never
    started: close() must always release unread rows, which on MySQL would
    otherwise block the connection. Closes itself when exhausted or failing.
    """

    def __init__(self, database: 'Database', cursor: Cursor, chunkSize: int, firstChunk: List[Tuple[Any, ...]]) -> None:
        self._database = database
        self._cursor = cursor
        self._chunkSize = chunkSize
        self._pending: Optional[List[Tuple[Any, ...]]] = firstChunk
        self.closed = False
        # The query's cursor.description, for what the driver says of each
        # column beyond its name.
        self.description: Optional[Sequence[Sequence[Any]]] = None


    def __iter__(self) -> 'RowStream':

        return self


    def __next__(self) -> List[Tuple[Any, ...]]:

        if self.closed:
            raise StopIteration

        if self._pending is not None:
            chunk, self._pending = self._pending, None
        else:
            try:
                chunk = self._cursor.fetchmany(self._chunkSize)
            except BaseException:
                self.close()
                raise

        if not chunk:
            self.close()
            raise StopIteration

        return list(chunk)


    def close(self) -> None:
        """Best-effort: the connection may already be gone, and an error here
        must not replace whatever error led to the close.
        """

        if self.closed:
            return
        self.closed = True
        self._pending = None
        self._database._streams.discard(self)

        try:
            self._database.dialect.discardRemaining(self._database.connection, self._cursor)
        except Exception:
            pass
        try:
            self._cursor.close()
        except Exception:
            pass


    def __enter__(self) -> 'RowStream':

        return self


    def __exit__(self, excType: Optional[Type[BaseException]], excValue: Optional[BaseException], traceback: Optional[TracebackType]) -> None:

        self.close()


class Database(TableMaintenance):

    def __init__(self, connectionSettings: ConnectionConfig, create: bool = False) -> None:
        """`create` lets a SQLite or DuckDB file that doesn't exist yet be
        created, as their drivers otherwise would for any connection. Only
        what may create tables asks for it: `schema --apply`, and a job's
        target, whose preTargetAdhocQueries can. Anything else is refused
        instead, or a misspelled source path would open an empty database,
        and discover, coverage or a job would report it as one.
        """

        if isinstance(connectionSettings, (FilesConnection, IcebergConnection)):
            # A job writes files through bauta.lake; anything else that asks
            # for a connection -- discover, coverage, run state -- asks for a
            # database.
            raise ConfigurationError('{} is a {} connection, not a database: jobs can write to it, but nothing reads from it'.format(
                connectionSettings.describeTarget(), connectionSettings.type.value))

        self.connectionSettings: DatabaseConfig = connectionSettings
        self.type: DatabaseType = connectionSettings.type
        self.dialect = DIALECTS[self.type]
        self.primaryKeyCache: Dict[str, List[str]] = {}
        self.columnNameCache: Dict[str, List[str]] = {}
        self.columnTypeCache: Dict[str, Dict[str, Any]] = {}
        # One per table loaded, so each learns from its table's chunks.
        self._preparers: Dict[str, ValuePreparer] = {}
        self._streams: Set[RowStream] = set()
        self._create = create
        self.connect()


    def connect(self) -> None:

        settings = self.connectionSettings
        if (not self._create and isinstance(settings, (SQLiteConnection, DuckDBConnection)) and settings.path != ':memory:'
                and not isSchemePath(settings.path) and not os.path.exists(settings.path)):
            raise ConfigurationError('{} does not exist. A relative path in connections.yaml is relative to that file; '
                                     'check the path, or create the database first'.format(settings.describeTarget()))

        self.connection: Connection
        self.cursor: Cursor
        self.connection, self.cursor = self.dialect.connect(self.connectionSettings)

        if getattr(settings, 'requireEncryption', False):
            self._refuseUnencrypted()

        timeout = getattr(settings, 'statementTimeoutSeconds', None)
        if timeout:
            self.dialect.limitStatements(self.connection, self.cursor, float(timeout))
            self.connection.commit()

        if getattr(settings, 'readOnly', False):
            statement = self.dialect.readOnlySessionStatement()
            if statement is not None:
                self.cursor.execute(statement)
                self.connection.commit()


    def _refuseWrite(self, what: str) -> None:
        """Raises ConfigurationError where the connection is readOnly, before
        `what` -- a load, DDL, a delete -- reaches it. Every write bauta makes
        comes through here, whichever command makes it, so the setting holds
        on databases that can't make a session read-only themselves.
        """

        if getattr(self.connectionSettings, 'readOnly', False):
            raise ConfigurationError('{} is readOnly, and this would {}; nothing was written. Point the job or command at another '
                                     'connection'.format(self.connectionSettings.describeTarget(), what))


    def _refuseUnencrypted(self) -> None:
        """Closes the session and raises ConfigurationError unless the server
        says it is encrypted: not encrypted, and can't say -- SQL Server to a
        login without VIEW SERVER STATE -- alike, since requireEncryption
        promises the one answer.
        """

        encrypted = self.isEncrypted()
        if encrypted is True:
            return

        try:
            self.close()
        except Exception:
            pass
        why = 'says this session is not encrypted' if encrypted is False else 'could not say whether this session is encrypted'
        raise ConfigurationError('{} has requireEncryption, and the server {}; nothing was read or written. Set TLS in its options -- see '
                                 'docs/reference/connections.md#driver-options-and-tls{}'.format(
                                     self.connectionSettings.describeTarget(), why,
                                     '' if encrypted is False else ' -- or give the login VIEW SERVER STATE, which SQL Server asks for to say'))


    def close(self) -> None:
        """Closes any stream still open first, so its unread rows can't make
        closing the cursor fail. The connection is closed even if the cursor
        can't be.
        """

        for stream in list(self._streams):
            stream.close()

        try:
            self.cursor.close()
        finally:
            self.connection.close()


    def __enter__(self) -> 'Database':

        return self


    def __exit__(self, excType: Optional[Type[BaseException]], excValue: Optional[BaseException], traceback: Optional[TracebackType]) -> None:

        if excType is None:
            self.close()
            return

        # Already failing: a close error would replace the error that matters.
        try:
            self.close()
        except Exception:
            pass


    def query(self, query: str) -> List[Tuple[Any, ...]]:

        self.cursor.execute(query)

        return self.cursor.fetchall()


    def bindWatermark(self, query: str, watermark: Any) -> Tuple[str, Tuple[Any, ...]]:
        """`query` with each {{ watermark }} as this dialect's bind placeholder,
        and the parameters to bind: the watermark once for each, so it is
        bound rather than interpolated. Once in all was bound before, so a
        query naming it twice -- `created > {{ watermark }} OR updated >
        {{ watermark }}` -- passed validation and failed every run on a
        count of parameters.
        """

        placeholder = self.dialect.placeholders(1)[0]
        parts: List[str] = []
        position = 0
        # Only in the statement itself, found where codeOnly leaves them: one
        # in a comment or a string literal is no placeholder to SQLite, DuckDB
        # or Oracle, so binding it too gave them a value more than they had
        # places for.
        found = list(WATERMARK_PLACEHOLDER.finditer(codeOnly(query, identifiers=True)))
        for match in found:
            parts.extend((query[position:match.start()], placeholder))
            position = match.end()
        parts.append(query[position:])

        return ''.join(parts), (watermark,) * len(found)


    def stream(self, query: str, chunkSize: int, parameters: Optional[Sequence[Any]] = None) -> Tuple[List[str], RowStream]:
        """Runs `query` and returns (columnNames, chunks), chunks being a
        RowStream to iterate and, if it isn't read to the end, to close.

        The first chunk is fetched eagerly, since psycopg's server-side cursors
        only describe their columns once rows are fetched. Nothing here commits,
        which would invalidate such a cursor.

        With `parameters`, on the %s dialects, a literal % elsewhere in the
        query must be doubled to %%.
        """

        cursor = self.dialect.streamingCursor(self.connection, chunkSize=chunkSize)
        stream = RowStream(self, cursor, chunkSize, [])
        self._streams.add(stream)

        try:
            if parameters is None:
                cursor.execute(query)
            else:
                cursor.execute(query, prepareParameters(self.type, parameters))

            stream._pending = list(cursor.fetchmany(chunkSize))
            stream.description = list(cursor.description)
            columns = [row[0] for row in cursor.description]
        except BaseException:
            stream.close()
            raise

        return columns, stream


    def execute(self, statement: str) -> int:
        """Runs `statement` in the open transaction, without committing, and
        returns the rows it changed. For work that must commit or roll back as
        one, with commit() and rollback().
        """

        self._refuseWrite('change rows')

        self.cursor.execute(statement)

        return self.cursor.rowcount


    def commit(self) -> None:

        self.connection.commit()


    def rollback(self) -> None:
        """Undoes what the open transaction did, if one is open. A failed
        statement leaves PostgreSQL's transaction refusing anything more until
        it is rolled back.
        """

        self.connection.rollback()


    def alter(self, query: str) -> None:

        self._refuseWrite('run a statement that changes it')
        self.cursor.execute(query)
        self.connection.commit()


    def truncate(self, table: str) -> None:

        self._refuseWrite('empty {}'.format(table))
        query = self.dialect.truncateQuery(table=self.statementName(table))
        self.cursor.execute(query)
        self.connection.commit()


    def checkName(self, table: str) -> None:
        """Refuses a name this database would silently cut to its length limit.

        Cutting is not an error on any database, so two tables named alike up
        to the limit would be one table, and two jobs would load and swap over
        each other.
        """

        problem = tooLongName(self.type, table)
        if problem is not None:
            raise ConfigurationError('table {}: {}. Load it into a shorter name'.format(table, problem))


    def statementName(self, table: str) -> str:
        """`table` as a statement must spell it: quoted, so a table named for a
        reserved word works, and folded as this database folds an unquoted name,
        so a name written plainly still means the table it named unquoted.

        A name already quoted -- the only way to write one this database folds
        differently -- keeps its own spelling. Catalog lookups take the name as
        written and unquote it themselves.
        """

        self.checkName(table)

        return quoteFoldedTable(self.type, table)


    def _describe(self, table: str) -> Sequence[Sequence[Any]]:
        """The table's cursor.description. WHERE 1=0 is valid ANSI SQL on every
        database here, and describes the columns without scanning or fetching
        a row.
        """

        self.cursor.execute('SELECT * FROM {} WHERE 1=0'.format(self.statementName(table)))

        return self.cursor.description


    def getAllColumnTypes(self, table: str) -> List[Any]:

        return [row[1] for row in self._describe(table)]


    def getAllColumnNames(self, table: str) -> List[str]:
        """The table's columns, in order. Their types are kept from the same
        statement, for the conversions that depend on the column; see values.
        """

        description = self._describe(table)
        self.columnTypeCache[table] = {row[0]: row[1] for row in description}

        return [row[0] for row in description]


    def _columnTypes(self, table: str, columns: Sequence[str]) -> Optional[List[Any]]:
        """The type codes of `columns`, catalog-spelled, where the conversions
        for this database depend on the column (WANTS_COLUMN_TYPES), and None
        for the rest. Read with the column names, which every load resolves
        first, so they cost no statement.
        """

        types = self.columnTypeCache.get(table)
        if self.type not in WANTS_COLUMN_TYPES or types is None:
            return None

        return [types.get(column) for column in columns]


    def catalogColumns(self, table: str, columns: Optional[Sequence[str]] = None) -> List[str]:
        """`columns` as the catalog spells them -- or all of the table's, in its
        order -- since quoting makes names case-sensitive on Oracle and
        PostgreSQL. An exact match wins, else the one match ignoring case.
        """

        if table not in self.columnNameCache:
            self.columnNameCache[table] = self.getAllColumnNames(table=table)
        catalog = self.columnNameCache[table]

        if columns is None:
            return list(catalog)

        resolved = []
        for column in columns:
            if column in catalog:
                resolved.append(column)
                continue
            matches = [name for name in catalog if name.upper() == column.upper()]
            if not matches:
                raise ConfigurationError('{} has no column {} (it has: {})'.format(table, column, ', '.join(catalog)))
            if len(matches) > 1:
                raise ConfigurationError('{} has columns {} that differ only in case; name the one meant exactly'.format(table, ', '.join(matches)))
            resolved.append(matches[0])

        return resolved


    def quoted(self, columns: Sequence[str]) -> List[str]:

        return [quoteIdentifier(self.type, column) for column in columns]


    def getPrimaryColumnNames(self, table: str) -> List[str]:
        """The table's declared primary key, in key order, from its own schema.
        Memoized for this Database's life, one job, since upsert asks per chunk.
        """

        if table not in self.primaryKeyCache:
            self.primaryKeyCache[table] = self.dialect.primaryKey(self.cursor, table)

        return self.primaryKeyCache[table]


    def listTables(self, schema: Optional[str] = None) -> List[str]:
        """The base tables a job could copy, sorted, named so that each can be
        handed straight back to catalogColumns or getPrimaryColumnNames.

        Views and the server's own tables are left out. Without `schema` these
        are the connection's own tables -- the schema an unqualified name
        already resolves in, `currentSchema` included -- and named bare, as a
        job would write them. With one, each is qualified with it.

        A name the catalog spells in a way this database would not fold an
        unquoted name to -- a mixed-case name on Oracle or PostgreSQL, a
        reserved word, a name with a space -- comes back quoted, since that is
        the only spelling that reads back as the same table. See "How names are
        written" in docs/concepts/how-it-works.md.
        """

        names = [self._asWritten(name) for name in self.dialect.listTables(self.cursor, schema)]

        if schema is not None:
            # As the catalog holds it, as each name is: `other` written bare
            # is OTHER on Oracle, and quoting it as written names another schema.
            qualifier = self._asWritten(catalogName(self.type, schema))
            names = ['{}.{}'.format(qualifier, name) for name in names]

        # Sorted here rather than left to the server: each orders by its own
        # collation -- SQL Server's ignores case, Oracle's does not -- and a
        # name that had to be quoted sorts by its quote. One order on all seven
        # is what makes a report comparable between them.
        return sorted(names)


    def _asWritten(self, name: str) -> str:
        """A name the catalog reported, spelled so that reading it back means
        this same name and no other.

        Left bare where it already reads back as itself, which is the spelling
        a person would write. Quoted otherwise: a name this database would fold
        (`Orders` on Oracle or PostgreSQL), and a name carrying a dot, which
        would otherwise be read as `schema.table`.
        """

        schema, bare = splitTableName(name)

        if schema is None and catalogName(self.type, bare) == name:
            return name

        return quoteIdentifier(self.type, name)


    def getForeignKeys(self, schema: Optional[str] = None) -> List[ForeignKey]:
        """Every foreign key on a table in `schema`, or in the current schema
        without one -- what subsetting follows. A table is named as
        listTables(schema) names it: bare in the current schema, and
        `schema.table` in the one named.
        """

        return self.dialect.foreignKeys(self.cursor, schema)


    def getForeignKeysFor(self, tables: Sequence[str]) -> List[ForeignKey]:
        """The foreign keys of every schema `tables` are in, a table written
        bare being in the current schema, so a job's `app.orders` is matched
        with the keys `app` declares rather than the current schema's.
        """

        schemas = sorted({splitTableName(table)[0] or '' for table in tables}) or ['']
        keys = []

        for schema in schemas:
            keys.extend(self.getForeignKeys(schema or None))

        return keys


    def foreignKeysElsewhere(self, schemas: Sequence[Optional[str]]) -> Dict[str, int]:
        """The schemas other than `schemas` (None being the current one) that
        declare foreign keys, and how many each: where to look when a check
        found none in the schemas it read.
        """

        counts, current = self.dialect.foreignKeyCounts(self.cursor)
        read = {current if schema is None else catalogName(self.type, schema) for schema in schemas}

        return {schema: count for schema, count in sorted(counts.items()) if schema not in read and count}


    def getCheckConstraints(self, table: str) -> List[str]:
        """The definitions of the table's CHECK constraints, as its catalog
        spells them; see generate.checks.
        """

        return self.dialect.checkConstraints(self.cursor, table)


    def getIndexes(self, table: str) -> List[Tuple[str, Tuple[str, ...]]]:
        """(name, columns) for each of the table's plain indexes over columns,
        for `schema` to recreate; see DatabaseDialect.sourceIndexes.
        """

        return self.dialect.sourceIndexes(self.cursor, table)


    def getColumnDefinitions(self, table: str) -> List[ColumnDefinition]:
        """Each column's catalog type, size and nullability -- what DDL needs."""

        return self.dialect.columnDefinitions(self.cursor, table)


    def isEncrypted(self) -> Optional[bool]:
        """Whether the server reports this connection as encrypted; None if it
        can't say, including when this login isn't allowed to ask.
        """

        try:
            return self.dialect.isEncrypted(self.cursor)
        except Exception:
            # A failed statement aborts PostgreSQL's transaction until rolled back.
            self.connection.rollback()
            return None


    def tableExists(self, table: str) -> bool:
        """Checked here as well as in statementName, since `schema --apply`
        asks this before creating a table and writes its own DDL.
        """

        self.checkName(table)

        return self.dialect.tableExists(self.cursor, table)


    def sample(self, query: str, rows: int) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        """Column names and up to `rows` rows of `query`, without reading the
        rest or needing dialect-specific LIMIT syntax.
        """

        columns, chunks = self.stream(query=query, chunkSize=rows)
        with chunks:
            firstChunk = next(chunks, [])

        # JSON parsed, since it is classified rather than loaded; see decodedJson.
        return columns, decodedJson(firstChunk, getattr(chunks, 'description', None), self.type)


    def _getColumnBuckets(self, table: str, columns: Optional[List[str]] = None) -> Tuple[List[str], List[str], List[str]]:
        """(all, primary-key, non-primary-key) columns for an upsert. `columns`
        overrides the table's own list; the key always comes from the table.

        The split ignores case: a key column left in the non-primary bucket
        would be updated while being joined on (ORA-38104 on Oracle). A table
        without a primary key is a ConfigurationError, so it isn't retried.
        """

        allColumns = self.catalogColumns(table=table, columns=columns)
        primaryColumns = self.getPrimaryColumnNames(table=table)

        if not primaryColumns:
            raise ConfigurationError('{} has no primary key, so an upsert cannot match its rows -- add one, or use insertStrategy: swap'.format(table))

        primaryColumnsNormalized = {column.upper() for column in primaryColumns}
        nonPrimaryColumns = [column for column in allColumns if column.upper() not in primaryColumnsNormalized]

        return allColumns, primaryColumns, nonPrimaryColumns


    @staticmethod
    def _batches(data: List[Tuple[Any, ...]], chunkSize: int) -> Iterator[List[Tuple[Any, ...]]]:
        """chunkSize-sized slices, and none at all for empty data -- some
        drivers reject an executemany with no rows.
        """

        for index in range(0, len(data), chunkSize):
            yield data[index:index + chunkSize]


    def _preparer(self, table: str) -> ValuePreparer:

        if table not in self._preparers:
            self._preparers[table] = ValuePreparer(self.type)

        return self._preparers[table]


    _DISPLAY_NAMES = {DatabaseType.MYSQL: 'MySQL', DatabaseType.MARIADB: 'MariaDB', DatabaseType.MSSQL: 'SQL Server', DatabaseType.ORACLE: 'Oracle',
                      DatabaseType.SQLITE: 'SQLite'}

    def _refuseUnloadable(self, table: str, batch: List[Tuple[Any, ...]], columns: Sequence[str], columnTypes: Optional[Sequence[Any]]) -> None:
        """Raises UnloadableValueError for a float the target can't hold; see
        DatabaseDialect.REFUSED_FLOATS.
        """

        refuseNonFinite(self.dialect.REFUSED_FLOATS, batch, columns, table, self._DISPLAY_NAMES.get(self.type, self.type.value),
                        lambda index: columnTypes is not None and index < len(columnTypes) and self.dialect.holdsNonFinite(columnTypes[index]))


    def insert(self, table: str, data: List[Tuple[Any, ...]], chunkSize: int = 100, columns: Optional[List[str]] = None) -> None:
        """`columns` defaults to all of the table's, in its order; `data` must
        match. Each batch uses the dialect's bulk path where it has one, and
        commits on its own.
        """

        self._refuseWrite('load rows into {}'.format(table))

        catalogColumns = self.catalogColumns(table=table, columns=columns)
        columnTypes = self._columnTypes(table, catalogColumns)
        resolvedColumns = self.quoted(catalogColumns)
        statementTable = self.statementName(table)
        query = 'INSERT INTO {} ({}) VALUES ({})'.format(
            statementTable, ', '.join(resolvedColumns), ', '.join(self.dialect.placeholders(len(resolvedColumns))))

        preparer = self._preparer(table)
        for batch in self._batches(data, chunkSize):
            batch = preparer.prepare(batch, columnTypes)
            self._refuseUnloadable(table, batch, catalogColumns, columnTypes)
            if not self.dialect.bulkInsert(self.cursor, statementTable, resolvedColumns, batch):
                self.dialect.bindTypes(self.cursor, columnTypes)
                self.cursor.executemany(query, batch)
            self.connection.commit()


    def upsert(self, table: str, data: List[Tuple[Any, ...]], chunkSize: int = 100, columns: Optional[List[str]] = None) -> None:
        """Like insert(). For the bulk path, a batch is first reduced to its
        last row per key, since one statement can't update a row twice.
        """

        self._refuseWrite('load rows into {}'.format(table))

        allColumns, primaryKeyColumns, nonPrimaryKeyColumns = self._getColumnBuckets(table=table, columns=columns)
        columnTypes = self._columnTypes(table, allColumns)

        normalizedColumns = [column.upper() for column in allColumns]
        keyIndexes = [normalizedColumns.index(column.upper()) for column in primaryKeyColumns if column.upper() in normalizedColumns]
        # Only a dialect with a bulk path loads the collapsed batch; for the
        # rest, collapsing it was work thrown away.
        canCollapse = len(keyIndexes) == len(primaryKeyColumns) and type(self.dialect).bulkUpsert is not DatabaseDialect.bulkUpsert

        catalogColumns = allColumns
        allColumns, primaryKeyColumns, nonPrimaryKeyColumns = self.quoted(allColumns), self.quoted(primaryKeyColumns), self.quoted(nonPrimaryKeyColumns)
        statementTable = self.statementName(table)
        query = self.dialect.upsertQuery(table=statementTable, allColumns=allColumns, primaryKeyColumns=primaryKeyColumns,
                                         nonPrimaryKeyColumns=nonPrimaryKeyColumns)

        preparer = self._preparer(table)
        for batch in self._batches(data, chunkSize):
            batch = preparer.prepare(batch, columnTypes)
            self._refuseUnloadable(table, batch, catalogColumns, columnTypes)
            with self._uniqueKeyClashes(table):
                loaded = False
                if canCollapse:
                    lastPerKey = list({tuple(row[index] for index in keyIndexes): row for row in batch}.values())
                    loaded = self.dialect.bulkUpsert(self.cursor, statementTable, allColumns, primaryKeyColumns, nonPrimaryKeyColumns, lastPerKey)
                if not loaded:
                    self.dialect.bindTypes(self.cursor, columnTypes)
                    self.cursor.executemany(query, batch)
            self.connection.commit()


    @contextlib.contextmanager
    def _uniqueKeyClashes(self, table: str) -> Iterator[None]:
        """Turns the dialect's refusal of a row that matched a different row
        by another unique key into an error that says so, rolled back and
        never retried: the same rows would match the same way.
        """

        try:
            yield
        except Exception as error:
            if not self.dialect.isUniqueKeyClash(error):
                raise
            self.connection.rollback()
            raise UniqueKeyClashError('{}: a row matched a different row already there by a unique key other than the primary key, '
                                      'and an upsert matches rows by primary key alone, so the load stopped rather than overwrite that '
                                      'row with it. Two rows share a unique value -- a masked one that collided, say -- or the target\'s '
                                      'unique keys differ from the source\'s'.format(table)) from None


    def upsertFromStage(self, targetTable: str, stageTable: str, columns: Optional[List[str]] = None) -> None:

        self._refuseWrite('load rows into {}'.format(targetTable))
        allColumns, primaryKeyColumns, nonPrimaryKeyColumns = (self.quoted(bucket) for bucket in self._getColumnBuckets(table=targetTable, columns=columns))
        query = self.dialect.upsertFromStageQuery(targetTable=self.statementName(targetTable), stageTable=self.statementName(stageTable),
                                                    allColumns=allColumns,
                                                    primaryKeyColumns=primaryKeyColumns, nonPrimaryKeyColumns=nonPrimaryKeyColumns)
        with self._uniqueKeyClashes(targetTable):
            self.alter(query=query)

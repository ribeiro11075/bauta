from __future__ import annotations

import datetime
import decimal
import hashlib
import logging
import math
import re
import uuid
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from .configuration import IDENTIFIER, ConfigurationError, DatabaseConnectionConfig, DatabaseType
from .log import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)


class ForeignKey(NamedTuple):
    """One foreign-key constraint. Composite keys list their columns in order."""

    table: str
    columns: Tuple[str, ...]
    referencedTable: str
    referencedColumns: Tuple[str, ...]
    name: str


def _groupForeignKeys(rows: Sequence[Sequence[Any]]) -> List[ForeignKey]:
    """Folds (table, column, referencedTable, referencedColumn, constraint) rows,
    already ordered by position within each constraint, into ForeignKeys.
    """

    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for table, column, referencedTable, referencedColumn, name in rows:
        entry = grouped.setdefault((table, name), {'referencedTable': referencedTable, 'columns': [], 'referencedColumns': []})
        entry['columns'].append(column)
        entry['referencedColumns'].append(referencedColumn)

    return [
        ForeignKey(table=table, columns=tuple(entry['columns']), referencedTable=entry['referencedTable'],
                   referencedColumns=tuple(entry['referencedColumns']), name=name)
        for (table, name), entry in grouped.items()
        ]


class ColumnDefinition(NamedTuple):
    """One column as the database's catalog describes it, for generating DDL.

    `dataType` is the catalog's own type name (`character varying`, `VARCHAR2`,
    `nvarchar`, ...); schema.py maps it to a portable type. `length` is in
    characters, and is None for unbounded text or where it doesn't apply.
    """

    name: str
    dataType: str
    length: Optional[int]
    precision: Optional[int]
    scale: Optional[int]
    nullable: bool


def _columnDefinitions(rows: Sequence[Sequence[Any]]) -> List[ColumnDefinition]:
    """Rows of (name, type, length, precision, scale, nullable) -> ColumnDefinitions.

    Catalogs disagree on how they say "nullable" (YES, Y, 1) and sometimes
    return numbers as Decimal, so both are normalized here.
    """

    def number(value: Any) -> Optional[int]:
        return None if value is None else int(value)

    return [
        ColumnDefinition(name=name, dataType=str(dataType), length=number(length), precision=number(precision), scale=number(scale),
                         nullable=str(nullable).upper() in ('YES', 'Y', '1', 'TRUE'))
        for name, dataType, length, precision, scale, nullable in rows
        ]


class ColumnCategory(str, Enum):
    NUMBER = 'number'
    DATE = 'date'
    TEXT = 'text'


def _mergeUpdateInsertClause(targetAlias: str, sourceAlias: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:
    """The ON/WHEN MATCHED/WHEN NOT MATCHED tail of an Oracle or SQL Server
    MERGE. A key-only table gets no WHEN MATCHED, since an empty SET is invalid.
    """

    onClause = ' AND '.join('{}.{} = {}.{}'.format(targetAlias, column, sourceAlias, column) for column in primaryKeyColumns)
    insertColumns = ', '.join(allColumns)
    insertValues = ', '.join('{}.{}'.format(sourceAlias, column) for column in allColumns)

    whenMatched = ''
    if nonPrimaryKeyColumns:
        updateClause = ', '.join('{}.{} = {}.{}'.format(targetAlias, column, sourceAlias, column) for column in nonPrimaryKeyColumns)
        whenMatched = 'WHEN MATCHED THEN UPDATE SET {} '.format(updateClause)

    return 'ON ({}) {}WHEN NOT MATCHED THEN INSERT ({}) VALUES ({})'.format(onClause, whenMatched, insertColumns, insertValues)


# How each database quotes an identifier, where it isn't with double quotes.
_IDENTIFIER_QUOTES = {DatabaseType.MYSQL: ('`', '`'), DatabaseType.MARIADB: ('`', '`'), DatabaseType.MSSQL: ('[', ']')}

# How a database folds an unquoted identifier, where it doesn't keep it as
# written. The others compare identifiers case-insensitively anyway.
_UNQUOTED_CASE = {DatabaseType.ORACLE: str.upper, DatabaseType.POSTGRESQL: str.lower}

# Every quoting style the dialects use, for reading a name written in any of
# them. No database allows these characters in a name written without quotes,
# so a name carrying one was quoted.
_QUOTE_PAIRS = dict([('"', '"')] + list(_IDENTIFIER_QUOTES.values()))

# The longest name each database keeps, and whether it counts bytes or
# characters. A longer name isn't refused: it is silently cut to the limit, so
# two names alike up to it become one table. SQLite has no limit.
IDENTIFIER_LIMITS = {
    DatabaseType.POSTGRESQL: (63, 'bytes'),
    DatabaseType.MYSQL: (64, 'characters'),
    DatabaseType.MARIADB: (64, 'characters'),
    DatabaseType.ORACLE: (128, 'bytes'),
    DatabaseType.MSSQL: (128, 'characters'),
    }


def quoteIdentifier(databaseType: DatabaseType, name: str) -> str:
    """`name`, quoted, so a reserved word (`rank`, `order`) works as a column
    name. Quoting makes the name case-sensitive on Oracle and PostgreSQL, so
    `name` must be spelled as the catalog spells it.
    """

    opening, closing = _IDENTIFIER_QUOTES.get(databaseType, ('"', '"'))

    return opening + name.replace(closing, closing * 2) + closing


def quoteFolded(databaseType: DatabaseType, name: str) -> str:
    """`name` quoted as the database would store it unquoted -- upper case on
    Oracle, lower case on PostgreSQL -- for a column being created, which
    then answers to the same unquoted name it would have without quotes.
    """

    fold = _UNQUOTED_CASE.get(databaseType)
    if fold is not None and IDENTIFIER.match(name):
        name = fold(name)

    return quoteIdentifier(databaseType, name)


def durationText(value: datetime.timedelta) -> str:
    """A duration as `[-]HH:MM:SS[.ffffff]`, the way MySQL writes a TIME.

    MySQL's TIME is a duration, from -838:59:59 to 838:59:59, and its driver
    returns a timedelta. Nothing else takes one: SQL Server's and SQLite's
    drivers refuse it outright, psycopg writes it as an interval -- which
    PostgreSQL then squeezed into a TIME column as a wrong time of day, without
    a word -- and Oracle stored Python's own `-35 days, 1:00:01`. Every
    database parses this spelling back into whatever the column is.
    """

    sign = '-' if value < datetime.timedelta(0) else ''
    magnitude = abs(value)
    hours, rest = divmod(int(magnitude.total_seconds()), 3600)
    minutes, seconds = divmod(rest, 60)
    fraction = '.{:06d}'.format(magnitude.microseconds) if magnitude.microseconds else ''

    return '{}{:02d}:{:02d}:{:02d}{}'.format(sign, hours, minutes, seconds, fraction)


def splitTableName(table: str) -> Tuple[Optional[str], str]:
    """`schema.table` -> ('schema', 'table'); a bare `table` -> (None, 'table'),
    None meaning the connection's current schema. Both parts keep the spelling
    they were written in, quotes and all, so they can go back into a statement.

    A dot inside quotes belongs to the name: `dbo.[a.b]` is one table `a.b` in
    schema `dbo`, not a schema `dbo.[a`.
    """

    parts: List[str] = []
    current: List[str] = []
    closing = None

    for character in table:
        if closing is not None:
            closing = None if character == closing else closing
        elif character in _QUOTE_PAIRS:
            closing = _QUOTE_PAIRS[character]
        elif character == '.':
            parts.append(''.join(current))
            current = []
            continue
        current.append(character)

    parts.append(''.join(current))
    schema = '.'.join(parts[:-1])

    return schema or None, parts[-1]


def unqualifiedName(table: str) -> str:
    """The name without its schema -- what `RENAME TO` and sp_rename take."""

    return splitTableName(table)[1]


def _isQuoted(databaseType: DatabaseType, name: str) -> bool:

    opening, closing = _IDENTIFIER_QUOTES.get(databaseType, ('"', '"'))

    return len(name) > 1 and name.startswith(opening) and name.endswith(closing)


def catalogName(databaseType: DatabaseType, name: str) -> str:
    """`name` as the catalog holds it: a quoted name unquoted, and a name
    written without quotes folded the way this database folds one.

    Catalog queries bind these, so a table a person can only name in quotes --
    a reserved word, mixed case on Oracle or PostgreSQL, a space -- is found
    rather than looked up with its quotes still on, which used to report every
    such table as having no columns and no primary key.

    A name no database would accept without quotes is taken as it is written,
    since there is no unquoted spelling of it to fold -- the same rule
    quoteFolded follows, so the two agree on what a written name means.
    """

    if _isQuoted(databaseType, name):
        return bareName(name)

    fold = _UNQUOTED_CASE.get(databaseType)

    return fold(name) if fold is not None and IDENTIFIER.match(name) else name


def bareName(name: str) -> str:
    """`name` without whatever quotes it was written in, whichever database's
    style they are.

    For matching a name written in one database's spelling against a name read
    from another's, which audit and verify-references do ignoring case. A
    lookup against one database binds catalogName instead.
    """

    closing = _QUOTE_PAIRS.get(name[:1])
    if closing is not None and len(name) > 1 and name.endswith(closing):
        return name[1:-1].replace(closing * 2, closing)

    return name


def catalogTableName(databaseType: DatabaseType, table: str) -> Tuple[Optional[str], str]:
    """The schema and table a catalog query binds, from a name as written."""

    schema, name = splitTableName(table)

    return (None if schema is None else catalogName(databaseType, schema)), catalogName(databaseType, name)


def _quotedParts(databaseType: DatabaseType, table: str, quote: Callable[[DatabaseType, str], str]) -> str:

    schema, name = splitTableName(table)
    # A part written in quotes is spelled exactly as it means to be, whichever
    # quoting the caller asked for; only a bare part is the caller's to fold.
    parts = [quoteIdentifier(databaseType, bareName(part)) if _isQuoted(databaseType, part) else quote(databaseType, part)
             for part in ([schema, name] if schema else [name])]

    return '.'.join(parts)


def tooLongName(databaseType: DatabaseType, table: str) -> Optional[str]:
    """Says which part of `table` this database would cut, and to what, or None.

    PostgreSQL cutting a name to 63 bytes is silent: two jobs whose targets
    differ only past that loaded the same table, and the second swap then
    renamed over the first's rows.
    """

    limit = IDENTIFIER_LIMITS.get(databaseType)
    if limit is None:
        return None

    length, unit = limit
    for part in catalogTableName(databaseType, table):
        if part is None:
            continue
        measured = len(part.encode('utf-8')) if unit == 'bytes' else len(part)
        if measured > length:
            return '{} is {} {} long, and {} keeps only {}, so it names whatever other table shares its first {}'.format(
                part, measured, unit, databaseType.value, length, length)

    return None


def catalogTable(databaseType: DatabaseType, table: str) -> str:
    """A table name as the catalog holds it, schema and all: catalogTableName
    written back as one name.
    """

    schema, name = catalogTableName(databaseType, table)

    return '{}.{}'.format(schema, name) if schema else name


def quoteTableName(databaseType: DatabaseType, table: str) -> str:
    """A table name a catalog reported, quoted part by part for a statement --
    so `group` becomes `"group"` and stays one identifier, and a qualified name
    stays two. The spelling is kept as it is, which is how the catalog holds it.
    """

    return _quotedParts(databaseType, table, quoteIdentifier)


def quoteFoldedTable(databaseType: DatabaseType, table: str) -> str:
    """quoteTableName for a table being created, whose name is folded the way
    this database folds an unquoted one -- see quoteFolded -- so it answers to
    the same name unquoted. A name already quoted keeps its own spelling.
    """

    return _quotedParts(databaseType, table, quoteFolded)


def suffixedName(databaseType: DatabaseType, table: str, suffix: str) -> str:
    """`table` with `suffix` on its own name, keeping the spelling it was
    written in: a quoted name grows inside its quotes, since `[group]_tmp` is
    not a name SQL Server can parse.
    """

    schema, name = splitTableName(table)
    if _isQuoted(databaseType, name):
        name = quoteIdentifier(databaseType, bareName(name) + suffix)
    else:
        name += suffix

    return '{}.{}'.format(schema, name) if schema else name


class DatabaseDialect(ABC):
    """Everything that differs between database types lives here, not in Database.

    `databaseType` lets a dialect read a name the way its own database writes
    one, for the catalog lookups and the statements it builds.
    """

    databaseType: DatabaseType

    @abstractmethod
    def connect(self, settings: DatabaseConnectionConfig) -> Tuple[Any, Any]:
        """Returns (connection, cursor). Drivers are imported here, so only the
        one in use needs installing.
        """

    @abstractmethod
    def _ownConnectArguments(self, settings: DatabaseConnectionConfig, password: Optional[str]) -> Dict[str, Any]:
        """The driver keyword arguments the connection fields map to."""

    def connectArguments(self, settings: DatabaseConnectionConfig, resolvePassword: bool = True) -> Dict[str, Any]:
        """The fields' driver arguments plus settings.options, refusing an
        option that duplicates a field. resolvePassword=False lets `validate`
        check this without running a passwordCommand.
        """

        own = self._ownConnectArguments(settings, settings.plainPassword() if resolvePassword else None)
        clashes = sorted(set(own) & set(settings.options))

        if clashes:
            raise ConfigurationError('options {} duplicate what the connection fields already set for {}; use the fields instead'.format(
                ', '.join(clashes), settings.type.value))

        return {**own, **settings.options}

    def streamingCursor(self, connection: Any, chunkSize: int) -> Any:
        """A cursor that doesn't buffer the whole result set client-side, which
        fetchmany() alone doesn't prevent. A plain cursor already streams on
        sqlite3 and pymssql.
        """

        return connection.cursor()


    def prepareValues(self, rows: List[Tuple[Any, ...]]) -> List[Tuple[Any, ...]]:
        """A batch as this driver must receive it. Every dialect writes a
        duration as text; PostgreSQL and SQL Server have more to do.
        """

        if not any(isinstance(value, datetime.timedelta) for row in rows for value in row):
            return rows

        return [tuple(durationText(value) if isinstance(value, datetime.timedelta) else value for value in row) for row in rows]


    def discardRemaining(self, connection: Any, cursor: Any) -> None:
        """Release rows left unread by an abandoned stream, so `connection` stays
        usable. Closing the cursor is enough everywhere but MySQL.
        """


    @abstractmethod
    def placeholders(self, count: int) -> List[str]:
        """Parameter placeholder markers, one per bound value, in this dialect's paramstyle."""

    def supportsMaterializedSelections(self) -> bool:
        """Whether `WITH name AS MATERIALIZED (...)` is accepted, and worth
        using: planSubset's queries then compute each selection once.
        """

        return False

    def isEncrypted(self, cursor: Any) -> Optional[bool]:
        """Whether the server reports this connection as encrypted in transit;
        None where there's no network or no way to tell.
        """

        return None

    def bulkInsert(self, cursor: Any, table: str, columns: List[str], rows: Sequence[Sequence[Any]]) -> bool:
        """Loads `rows` in fewer round trips than executemany, for drivers whose
        executemany sends a statement per row. False means nothing was sent.
        """

        return False

    def bulkUpsert(self, cursor: Any, table: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str],
                   rows: Sequence[Sequence[Any]]) -> bool:
        """bulkInsert for an upsert: the same contract. `rows` hold no two rows
        with the same key.
        """

        return False

    def truncateQuery(self, table: str) -> str:
        """TRUNCATE TABLE, which every dialect but SQLite has."""

        return 'TRUNCATE TABLE {}'.format(table)

    def columnCategory(self, dataType: Any) -> Optional[ColumnCategory]:
        """The category of a driver-specific cursor.description type_code, for
        discovery. None means unrecognized, and discovery samples values instead.
        """

        return None

    # The three catalog queries below each bind two parameters, the schema and
    # the table, from catalogTableName: unquoted, and folded as this database
    # folds a name written without quotes. A NULL schema means the current one.

    def primaryKeyQuery(self) -> str:
        """One table's declared primary-key columns, in key order. Not UNIQUE
        constraints, which would make an upsert treat a changed row as new.
        """

        raise NotImplementedError('{} cannot describe primary keys'.format(type(self).__name__))

    def columnsQuery(self) -> str:
        """One table's columns, in order, as rows of (name, type, length,
        precision, scale, nullable).
        """

        raise NotImplementedError('{} cannot describe columns'.format(type(self).__name__))

    def tableExistsQuery(self) -> str:
        """A count of tables with the bound name in the bound schema."""

        raise NotImplementedError('{} cannot check for tables'.format(type(self).__name__))

    def _catalog(self, cursor: Any, query: str, table: str) -> List[Any]:

        cursor.execute(query.format(*self.placeholders(2)), catalogTableName(self.databaseType, table))

        return cursor.fetchall()

    def primaryKey(self, cursor: Any, table: str) -> List[str]:

        return [row[0] for row in self._catalog(cursor, self.primaryKeyQuery(), table)]

    def columnDefinitions(self, cursor: Any, table: str) -> List[ColumnDefinition]:

        return _columnDefinitions(self._catalog(cursor, self.columnsQuery(), table))

    def tableExists(self, cursor: Any, table: str) -> bool:

        return bool(self._catalog(cursor, self.tableExistsQuery(), table)[0][0])

    def foreignKeysQuery(self) -> str:
        """Every foreign key in the connection's current schema, as rows of
        (table, column, referencedTable, referencedColumn, constraintName),
        ordered by table, constraint and position.
        """

        raise NotImplementedError('{} cannot list foreign keys'.format(type(self).__name__))

    def foreignKeys(self, cursor: Any) -> List[ForeignKey]:
        """For planning subsets. SQLite, which can't do it in one query,
        overrides this.
        """

        cursor.execute(self.foreignKeysQuery())

        return _groupForeignKeys(cursor.fetchall())

    @abstractmethod
    def upsertQuery(self, table: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:
        ...

    @abstractmethod
    def upsertFromStageQuery(self, targetTable: str, stageTable: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:
        ...

    @abstractmethod
    def swapQueries(self, targetTable: str, stageTable: str, tempTable: str) -> List[str]:
        """One or more statements to execute in order, then commit once.

        `tempTable` is in the stage table's schema, and `stageTable` must share
        the target's (configuration checks that), since a rename never moves a
        table between schemas. Renames take the new name unqualified.
        """

    def swap(self, cursor: Any, targetTable: str, stageTable: str, tempTable: str) -> None:
        """Runs the swap on `cursor`; the caller commits. A dialect with more to
        do around the renames overrides this.
        """

        for query in self.swapQueries(targetTable=targetTable, stageTable=stageTable, tempTable=tempTable):
            cursor.execute(query)


class _OnConflictDialect(DatabaseDialect):
    """PostgreSQL and SQLite share `INSERT ... ON CONFLICT` word for word. A
    key-only table gets DO NOTHING, since an empty SET is invalid.

    The stage form's `WHERE true` is SQLite's documented workaround for reading
    ON as the start of a join constraint.
    """

    @staticmethod
    def _onConflict(primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:

        if not nonPrimaryKeyColumns:
            return 'ON CONFLICT({}) DO NOTHING'.format(','.join(primaryKeyColumns))

        return 'ON CONFLICT({}) DO UPDATE SET {}'.format(
            ','.join(primaryKeyColumns), ', '.join('{0}=excluded.{0}'.format(column) for column in nonPrimaryKeyColumns))

    def upsertQuery(self, table: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:

        return 'INSERT INTO {} ({}) VALUES ({}) {}'.format(table, ', '.join(allColumns), ', '.join(self.placeholders(len(allColumns))),
                                                          self._onConflict(primaryKeyColumns, nonPrimaryKeyColumns))

    def upsertFromStageQuery(self, targetTable: str, stageTable: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:

        return 'INSERT INTO {} ({}) SELECT {} FROM {} WHERE true {}'.format(targetTable, ', '.join(allColumns), ', '.join(allColumns), stageTable,
                                                                          self._onConflict(primaryKeyColumns, nonPrimaryKeyColumns))


class MySQLDialect(DatabaseDialect):

    databaseType = DatabaseType.MYSQL

    _NUMBER_TYPES = {'INT', 'BIGINT'}
    _DATE_TYPES = {'DATETIME', 'TIMESTAMP', 'DATE'}
    _TEXT_TYPES = {'TEXT', 'VARCHAR', 'CHAR'}

    def _ownConnectArguments(self, settings: DatabaseConnectionConfig, password: Optional[str]) -> Dict[str, Any]:

        return {'user': settings.user, 'password': password, 'host': settings.host, 'database': settings.database,
                'port': settings.port}


    def connect(self, settings: DatabaseConnectionConfig) -> Tuple[Any, Any]:

        import mysql.connector

        connection = mysql.connector.connect(**self.connectArguments(settings))
        cursor = connection.cursor(buffered=True)

        return connection, cursor


    def streamingCursor(self, connection: Any, chunkSize: int) -> Any:
        """Unbuffered, unlike connect()'s cursor. It holds the connection until
        drained: no other statement may run on it while a stream is open.
        """

        return connection.cursor(buffered=False)


    def discardRemaining(self, connection: Any, cursor: Any) -> None:
        """mysql.connector queues unread rows on the connection, where they fail
        the next statement ("Unread result found"). consume_results() reads and
        discards them -- bounded in memory, but it transfers every unread row.
        """

        connection.consume_results()


    def placeholders(self, count: int) -> List[str]:

        return count * ['%s']


    def columnCategory(self, dataType: Any) -> Optional[ColumnCategory]:
        """mysql.connector reports type names as strings, e.g. "VARCHAR"."""

        if not isinstance(dataType, str):
            return None

        dataType = dataType.upper()

        if dataType in self._NUMBER_TYPES:
            return ColumnCategory.NUMBER
        if dataType in self._DATE_TYPES:
            return ColumnCategory.DATE
        if dataType in self._TEXT_TYPES:
            return ColumnCategory.TEXT

        return None


    def isEncrypted(self, cursor: Any) -> Optional[bool]:

        cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
        row = cursor.fetchone()

        return None if row is None else bool(row[1])


    def foreignKeysQuery(self) -> str:

        return ("SELECT table_name, column_name, "
                "CASE WHEN referenced_table_schema = DATABASE() THEN referenced_table_name "
                "ELSE CONCAT(referenced_table_schema, '.', referenced_table_name) END, "
                "referenced_column_name, constraint_name "
                "FROM information_schema.key_column_usage "
                "WHERE table_schema = DATABASE() AND referenced_table_name IS NOT NULL "
                "ORDER BY table_name, constraint_name, ordinal_position")


    def columnsQuery(self) -> str:
        """column_type rather than data_type, since only the first says
        `unsigned` -- an `INT UNSIGNED` column holds values no target's `INT`
        can, and looked exactly like an `INT` here. It carries the declared
        size too (`varchar(20)`, `enum('x','y')`), which portableType drops.
        """

        return ("SELECT column_name, column_type, character_maximum_length, numeric_precision, numeric_scale, is_nullable "
                "FROM information_schema.columns WHERE table_schema = COALESCE({}, DATABASE()) AND table_name = {} ORDER BY ordinal_position")


    def primaryKeyQuery(self) -> str:

        return ("SELECT column_name FROM information_schema.key_column_usage "
                "WHERE table_schema = COALESCE({}, DATABASE()) AND table_name = {} AND constraint_name = 'PRIMARY' ORDER BY ordinal_position")


    def tableExistsQuery(self) -> str:

        return "SELECT count(*) FROM information_schema.tables WHERE table_schema = COALESCE({}, DATABASE()) AND table_name = {}"


    @staticmethod
    def _onDuplicateKey(table: str, primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:
        """A key-only table gets a no-op assignment of its key, since an empty
        SET is invalid. Not INSERT IGNORE, which also silences truncation,
        NOT NULL and foreign-key errors.
        """

        if not nonPrimaryKeyColumns:
            return 'ON DUPLICATE KEY UPDATE {0}.{1}={0}.{1}'.format(table, primaryKeyColumns[0])

        return 'ON DUPLICATE KEY UPDATE {}'.format(', '.join('{0}=VALUES({0})'.format(column) for column in nonPrimaryKeyColumns))


    def upsertQuery(self, table: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:

        return 'INSERT INTO {} ({}) VALUES ({}) {}'.format(table, ', '.join(allColumns), ', '.join(self.placeholders(len(allColumns))),
                                                          self._onDuplicateKey(table, primaryKeyColumns, nonPrimaryKeyColumns))


    def upsertFromStageQuery(self, targetTable: str, stageTable: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:

        return 'INSERT INTO {} ({}) SELECT {} FROM {} {}'.format(targetTable, ', '.join(allColumns), ', '.join(allColumns), stageTable,
                                                                self._onDuplicateKey(targetTable, primaryKeyColumns, nonPrimaryKeyColumns))


    def swapQueries(self, targetTable: str, stageTable: str, tempTable: str) -> List[str]:
        """One atomic statement; RENAME TABLE takes qualified names on both sides."""

        return ['RENAME TABLE {} TO {}, {} TO {}, {} TO {}'.format(stageTable, tempTable, targetTable, stageTable, tempTable, targetTable)]


class _Unencodable(Exception):
    """A value COPY's text format has no safe spelling for, here."""


_COPY_ESCAPES = str.maketrans({'\\': '\\\\', '\t': '\\t', '\n': '\\n', '\r': '\\r'})


def _copyField(value: Any) -> str:
    """One value in PostgreSQL's COPY text format.

    Only types whose text form PostgreSQL parses back exactly are handled;
    anything else -- a list, a dict, a timedelta -- raises _Unencodable, and
    the chunk goes through the driver's own adapters instead.
    """

    if value is None:
        return '\\N'
    if isinstance(value, bool):
        return 't' if value else 'f'
    if isinstance(value, (int, decimal.Decimal)):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return 'NaN'
        if math.isinf(value):
            return 'Infinity' if value > 0 else '-Infinity'
        return repr(value)
    if isinstance(value, str):
        return value.translate(_COPY_ESCAPES)
    if isinstance(value, datetime.datetime):
        return value.isoformat(sep=' ')
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        # bytea's hex input, with its backslash escaped for the text format.
        return '\\\\x' + bytes(value).hex()

    raise _Unencodable(type(value).__name__)


def _copyText(rows: Sequence[Sequence[Any]]) -> Optional[str]:
    """The rows in COPY's text format, or None if any value can't be encoded."""

    try:
        return ''.join('\t'.join(_copyField(value) for value in row) + '\n' for row in rows)
    except _Unencodable:
        return None


def _copyIn(cursor: Any, statement: str, text: str) -> None:

    with cursor.copy(statement) as copy:
        copy.write(text)


class PostgreSQLDialect(_OnConflictDialect):

    databaseType = DatabaseType.POSTGRESQL

    _NUMBER_OIDS = {20, 21, 23}
    _DATE_OIDS = {1114, 1018}
    _TEXT_OIDS = {1043, 18, 25}

    def connect(self, settings: DatabaseConnectionConfig) -> Tuple[Any, Any]:

        import psycopg

        # ClientCursor writes parameters into the statement, as the other
        # drivers do, rather than binding them server-side: a server-side
        # parameter takes one fixed type, which a value can't always fit.
        connection = psycopg.connect(**self.connectArguments(settings), cursor_factory=psycopg.ClientCursor)
        cursor = connection.cursor()

        # Only this schema, with no fallback such as `public` that catalog
        # lookups wouldn't see. Committed, or a later rollback would undo it.
        if settings.currentSchema:
            cursor.execute('SET search_path TO {}'.format(settings.currentSchema))
            connection.commit()

        return connection, cursor


    def _ownConnectArguments(self, settings: DatabaseConnectionConfig, password: Optional[str]) -> Dict[str, Any]:

        return {'user': settings.user, 'password': password, 'host': settings.host, 'dbname': settings.database,
                'port': settings.port}


    def streamingCursor(self, connection: Any, chunkSize: int) -> Any:
        """A named, server-side cursor: psycopg buffers everything through an
        unnamed one. A commit on the connection invalidates it, so the extract
        side never commits.
        """

        cursor = connection.cursor(name='bauta_{}'.format(uuid.uuid4().hex))
        cursor.itersize = chunkSize

        return cursor


    def placeholders(self, count: int) -> List[str]:

        return count * ['%s']


    def supportsMaterializedSelections(self) -> bool:
        """PostgreSQL 12 and later. Without it, PostgreSQL copies a selection
        used once into its user, and planning a 12-table subset took minutes.
        """

        return True


    def isEncrypted(self, cursor: Any) -> Optional[bool]:

        cursor.execute('SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()')
        row = cursor.fetchone()

        return None if row is None else bool(row[0])


    def bulkInsert(self, cursor: Any, table: str, columns: List[str], rows: Sequence[Sequence[Any]]) -> bool:
        """COPY FROM STDIN: one round trip per chunk, where executemany sends
        one statement per row.
        """

        text = _copyText(rows)
        if text is None:
            return False

        _copyIn(cursor, 'COPY {} ({}) FROM STDIN'.format(table, ', '.join(columns)), text)

        return True


    def bulkUpsert(self, cursor: Any, table: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str],
                   rows: Sequence[Sequence[Any]]) -> bool:
        """COPY into a temporary table, then one INSERT ... ON CONFLICT from it.
        The table empties at every commit, so each chunk reuses it.
        """

        text = _copyText(rows)
        if text is None:
            return False

        columns = ', '.join(allColumns)
        staging = 'bauta_upsert_{}'.format(hashlib.sha1('{}|{}'.format(table, columns).encode('utf-8')).hexdigest()[:12])

        cursor.execute('CREATE TEMPORARY TABLE IF NOT EXISTS {} ON COMMIT DELETE ROWS AS SELECT {} FROM {} WITH NO DATA'.format(staging, columns, table))
        _copyIn(cursor, 'COPY {} ({}) FROM STDIN'.format(staging, columns), text)
        cursor.execute(self.upsertFromStageQuery(table, staging, allColumns, primaryKeyColumns, nonPrimaryKeyColumns))

        return True


    def columnCategory(self, dataType: Any) -> Optional[ColumnCategory]:
        """psycopg's cursor.description reports types as numeric OIDs, not names."""

        if dataType in self._NUMBER_OIDS:
            return ColumnCategory.NUMBER
        if dataType in self._DATE_OIDS:
            return ColumnCategory.DATE
        if dataType in self._TEXT_OIDS:
            return ColumnCategory.TEXT

        return None


    def foreignKeysQuery(self) -> str:
        """From pg_catalog rather than information_schema, which can't pair a
        composite key's columns with the columns they reference.
        """

        return ("SELECT cl.relname, att.attname, "
                "CASE WHEN rns.nspname = current_schema() THEN rcl.relname ELSE rns.nspname || '.' || rcl.relname END, "
                "ratt.attname, con.conname "
                "FROM pg_constraint con "
                "JOIN pg_class cl ON cl.oid = con.conrelid "
                "JOIN pg_namespace ns ON ns.oid = cl.relnamespace "
                "JOIN pg_class rcl ON rcl.oid = con.confrelid "
                "JOIN pg_namespace rns ON rns.oid = rcl.relnamespace "
                "CROSS JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS k(attnum, refattnum, position) "
                "JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum "
                "JOIN pg_attribute ratt ON ratt.attrelid = con.confrelid AND ratt.attnum = k.refattnum "
                "WHERE con.contype = 'f' AND ns.nspname = current_schema() "
                "ORDER BY cl.relname, con.conname, k.position")


    # The bound names arrive folded, so the lookups compare them as they are;
    # a name quoted in a job keeps the case it was quoted with. ::text gives a
    # NULL schema a type COALESCE can use.

    def columnsQuery(self) -> str:

        return ("SELECT column_name, data_type, character_maximum_length, numeric_precision, numeric_scale, is_nullable "
                "FROM information_schema.columns WHERE table_schema = COALESCE({}::text, current_schema()) "
                "AND table_name = {}::text ORDER BY ordinal_position")


    def primaryKeyQuery(self) -> str:

        return ("SELECT att.attname FROM pg_index idx "
                "JOIN pg_class cl ON cl.oid = idx.indrelid "
                "JOIN pg_namespace ns ON ns.oid = cl.relnamespace "
                "CROSS JOIN LATERAL unnest(idx.indkey) WITH ORDINALITY AS k(attnum, position) "
                "JOIN pg_attribute att ON att.attrelid = cl.oid AND att.attnum = k.attnum "
                "WHERE idx.indisprimary AND ns.nspname = COALESCE({}::text, current_schema()) AND cl.relname = {}::text "
                "ORDER BY k.position")


    def tableExistsQuery(self) -> str:

        return ("SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = COALESCE({}::text, current_schema()) AND table_name = {}::text")


    def prepareValues(self, rows: List[Tuple[Any, ...]]) -> List[Tuple[Any, ...]]:
        """Dictionaries as JSON, which is what they came from.

        psycopg reads a `json` or `jsonb` column as a dict and then refuses to
        write one back ("cannot adapt type 'dict'"), so copying a table with a
        JSON column failed at the first chunk. A list is left alone: psycopg
        writes one as an array, which is what a `text[]` column needs, and it
        can't be told apart from a JSON array here. A `jsonb` column holding
        one has to be selected as text.
        """

        rows = super().prepareValues(rows)

        if not any(isinstance(value, dict) for row in rows for value in row):
            return rows

        from psycopg.types.json import Jsonb

        return [tuple(Jsonb(value) if isinstance(value, dict) else value for value in row) for row in rows]


    def swapQueries(self, targetTable: str, stageTable: str, tempTable: str) -> List[str]:
        """Three renames in one transaction: PostgreSQL DDL is transactional, so
        a failure part-way leaves both tables as they were.
        """

        return ['ALTER TABLE {} RENAME TO {}; ALTER TABLE {} RENAME TO {}; ALTER TABLE {} RENAME TO {}'.format(
            stageTable, unqualifiedName(tempTable), targetTable, unqualifiedName(stageTable), tempTable, unqualifiedName(targetTable))]


    # Views built directly on a table: their names, and their definitions as
    # PostgreSQL would write them now, table names and all.
    DEPENDENT_VIEWS_QUERY = (
        "SELECT DISTINCT view.oid::regclass::text, pg_get_viewdef(view.oid) "
        "FROM pg_depend dependency "
        "JOIN pg_rewrite rewrite ON rewrite.oid = dependency.objid "
        "JOIN pg_class view ON view.oid = rewrite.ev_class "
        "WHERE dependency.classid = 'pg_rewrite'::regclass AND dependency.refobjid = %s::regclass "
        "AND view.oid <> dependency.refobjid AND view.relkind = 'v'")

    def swap(self, cursor: Any, targetTable: str, stageTable: str, tempTable: str) -> None:
        """Renames, then recreates each view on the target from its definition
        captured beforehand, since a PostgreSQL view follows the table, not the
        name. CREATE OR REPLACE keeps grants and views built on it. See "How a
        swap works" in docs/design.md.
        """

        cursor.execute(self.DEPENDENT_VIEWS_QUERY, (targetTable,))
        views = cursor.fetchall()

        super().swap(cursor, targetTable, stageTable, tempTable)

        for name, definition in views:
            cursor.execute('CREATE OR REPLACE VIEW {} AS {}'.format(name, definition))


def _oracleDatetimesAsTimestamps(cursor: Any, value: Any, arraysize: int) -> Any:
    """Bind a datetime as a TIMESTAMP, keeping its fraction of a second.

    oracledb binds one as DB_TYPE_DATE, which holds whole seconds only, so
    microseconds were silently dropped even into a TIMESTAMP(6) column. A
    DATE column still takes a TIMESTAMP bind, truncating as Oracle's own
    conversion does.
    """

    import oracledb

    if isinstance(value, datetime.datetime):
        return cursor.var(oracledb.DB_TYPE_TIMESTAMP_TZ if value.tzinfo else oracledb.DB_TYPE_TIMESTAMP, arraysize=arraysize)

    return None


def _oracleValues(cursor: Any, metadata: Any) -> Any:
    """How a column is fetched, per connection rather than through oracledb's
    process-wide defaults, so an embedding application keeps its own.

    CLOB, NCLOB and BLOB come back as str and bytes, not LOB handles, which no
    other driver can bind. A NUMBER with a scale comes back as a Decimal:
    oracledb's default is a float, which loses digits an Oracle NUMBER holds
    (123456789012345.6789 arrived as 123456789012345.67) and turns a large
    value into one no target can store. A scale of 0 stays an int, and
    BINARY_FLOAT and BINARY_DOUBLE stay floats, which is what they are.
    """

    import oracledb

    conversions = {
        oracledb.DB_TYPE_CLOB: oracledb.DB_TYPE_LONG,
        oracledb.DB_TYPE_NCLOB: oracledb.DB_TYPE_LONG_NVARCHAR,
        oracledb.DB_TYPE_BLOB: oracledb.DB_TYPE_LONG_RAW,
        }
    conversion = conversions.get(metadata.type_code)
    if conversion is not None:
        return cursor.var(conversion, arraysize=cursor.arraysize)

    if metadata.type_code is oracledb.DB_TYPE_NUMBER and metadata.scale != 0:
        return cursor.var(decimal.Decimal, arraysize=cursor.arraysize)

    return None


def _renameSteps(targetTable: str, stageTable: str, tempTable: str) -> List[Tuple[str, str]]:
    """The three renames a swap is, as (from, to) pairs: the stage out of the
    way, the target into its place, and the stage into the target's name.
    """

    return [(stageTable, tempTable), (targetTable, stageTable), (tempTable, targetTable)]


def _renameStatement(fromTable: str, toTable: str) -> str:
    """RENAME TO takes the new name unqualified; the table stays in its schema."""

    return 'ALTER TABLE {} RENAME TO {}'.format(fromTable, unqualifiedName(toTable))


def _renameInThreeSteps(targetTable: str, stageTable: str, tempTable: str) -> List[str]:

    return [_renameStatement(*step) for step in _renameSteps(targetTable, stageTable, tempTable)]


class OracleDialect(DatabaseDialect):

    databaseType = DatabaseType.ORACLE

    _NUMBER_TYPE_NAMES = {'DB_TYPE_NUMBER', 'DB_TYPE_BINARY_INTEGER', 'DB_TYPE_BINARY_FLOAT', 'DB_TYPE_BINARY_DOUBLE'}
    _DATE_TYPE_NAMES = {'DB_TYPE_DATE', 'DB_TYPE_TIMESTAMP', 'DB_TYPE_TIMESTAMP_TZ', 'DB_TYPE_TIMESTAMP_LTZ'}
    _TEXT_TYPE_NAMES = {'DB_TYPE_VARCHAR', 'DB_TYPE_CHAR', 'DB_TYPE_NVARCHAR', 'DB_TYPE_NCHAR', 'DB_TYPE_CLOB', 'DB_TYPE_NCLOB', 'DB_TYPE_LONG'}

    # ISO 8601 for implicit text-date conversions, so ISO text from other
    # databases, or a watermarkInitial, loads into a DATE. See "Moving values
    # between drivers" in docs/design.md.
    SESSION_FORMATS = ("ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS' "
                       "NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF' "
                       "NLS_TIMESTAMP_TZ_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF TZH:TZM'")

    def connect(self, settings: DatabaseConnectionConfig) -> Tuple[Any, Any]:

        import oracledb

        connection = oracledb.connect(**self.connectArguments(settings))
        connection.outputtypehandler = _oracleValues
        connection.inputtypehandler = _oracleDatetimesAsTimestamps
        cursor = connection.cursor()
        cursor.execute(self.SESSION_FORMATS)

        if settings.currentSchema:
            cursor.execute('ALTER SESSION SET CURRENT_SCHEMA = {}'.format(settings.currentSchema))

        return connection, cursor


    def _ownConnectArguments(self, settings: DatabaseConnectionConfig, password: Optional[str]) -> Dict[str, Any]:

        return {'user': settings.user, 'password': password, 'host': settings.host, 'port': settings.port,
                'service_name': settings.serviceName, 'sid': settings.sid}


    def streamingCursor(self, connection: Any, chunkSize: int) -> Any:
        """A chunk per round trip rather than oracledb's default 100 rows.
        prefetchrows one above arraysize is oracledb's documented pairing.
        """

        cursor = connection.cursor()
        cursor.arraysize = chunkSize
        cursor.prefetchrows = chunkSize + 1

        return cursor


    def placeholders(self, count: int) -> List[str]:

        return [':{}'.format(i + 1) for i in range(count)]


    def columnCategory(self, dataType: Any) -> Optional[ColumnCategory]:
        """Matches oracledb's DB_TYPE_* objects by `.name`, so the driver isn't
        imported here.
        """

        typeName = getattr(dataType, 'name', None)

        if typeName in self._NUMBER_TYPE_NAMES:
            return ColumnCategory.NUMBER
        if typeName in self._DATE_TYPE_NAMES:
            return ColumnCategory.DATE
        if typeName in self._TEXT_TYPE_NAMES:
            return ColumnCategory.TEXT

        return None


    def foreignKeysQuery(self) -> str:
        """From all_* views in the session's current schema: user_* views read
        the login's own schema, whatever currentSchema says. Constraint names
        are unique per owner only, so every join matches the owner too.
        """

        return ("SELECT c.table_name, cc.column_name, "
                "CASE WHEN rc.owner = c.owner THEN rc.table_name ELSE rc.owner || '.' || rc.table_name END, "
                "rcc.column_name, c.constraint_name "
                "FROM all_constraints c "
                "JOIN all_cons_columns cc ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name "
                "JOIN all_constraints rc ON rc.owner = c.r_owner AND rc.constraint_name = c.r_constraint_name "
                "JOIN all_cons_columns rcc ON rcc.owner = rc.owner AND rcc.constraint_name = rc.constraint_name AND rcc.position = cc.position "
                "WHERE c.constraint_type = 'R' AND c.owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') "
                "ORDER BY c.table_name, c.constraint_name, cc.position")


    # all_* views filtered to the bound schema or the session's current one,
    # which user_* views wouldn't follow after ALTER SESSION SET CURRENT_SCHEMA.
    # The bound names arrive folded, so a table quoted in a job -- the only way
    # to name a lower-case one on Oracle -- is looked up as it is spelled.
    OWNER = "COALESCE({}, SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA'))"

    def columnsQuery(self) -> str:
        """CHAR_LENGTH rather than DATA_LENGTH, which is in bytes."""

        return ("SELECT column_name, data_type, CASE WHEN char_length > 0 THEN char_length END, data_precision, data_scale, nullable "
                "FROM all_tab_columns WHERE owner = " + self.OWNER + " AND table_name = {} ORDER BY column_id")


    def isEncrypted(self, cursor: Any) -> Optional[bool]:

        cursor.execute("SELECT SYS_CONTEXT('USERENV', 'NETWORK_PROTOCOL') FROM dual")
        protocol = cursor.fetchone()[0]

        return None if protocol is None else protocol.lower() == 'tcps'


    def primaryKeyQuery(self) -> str:

        return ("SELECT cols.column_name FROM all_constraints cons "
                "JOIN all_cons_columns cols ON cols.owner = cons.owner AND cols.constraint_name = cons.constraint_name "
                "WHERE cons.constraint_type = 'P' AND cons.owner = " + self.OWNER + " AND cons.table_name = {} ORDER BY cols.position")


    def tableExistsQuery(self) -> str:

        return "SELECT count(*) FROM all_tables WHERE owner = " + self.OWNER + " AND table_name = {}"


    def upsertQuery(self, table: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:

        bindColumns = ', '.join('{} {}'.format(placeholder, column) for placeholder, column in zip(self.placeholders(len(allColumns)), allColumns))
        mergeClause = _mergeUpdateInsertClause('target', 'source', allColumns, primaryKeyColumns, nonPrimaryKeyColumns)

        return 'MERGE INTO {} target USING (SELECT {} FROM dual) source {}'.format(table, bindColumns, mergeClause)


    def upsertFromStageQuery(self, targetTable: str, stageTable: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:

        mergeClause = _mergeUpdateInsertClause('target', 'source', allColumns, primaryKeyColumns, nonPrimaryKeyColumns)

        return 'MERGE INTO {} target USING {} source {}'.format(targetTable, stageTable, mergeClause)


    def swapQueries(self, targetTable: str, stageTable: str, tempTable: str) -> List[str]:
        """Three statements, since cursor.execute() runs one. Not atomic:
        Oracle commits each DDL statement.
        """

        return _renameInThreeSteps(targetTable, stageTable, tempTable)


    def swap(self, cursor: Any, targetTable: str, stageTable: str, tempTable: str) -> None:
        """Renames one at a time, undoing the ones that worked if one fails.

        Oracle commits every DDL statement, so there is no transaction to roll
        back. A rename that failed part-way -- another session holding the
        table, which is ORA-00054 -- used to leave the stage table under the
        temporary name, so the stage table was gone and every later run failed
        with ORA-00942 until someone renamed it back by hand.

        An undo that fails is left for the error about the swap itself, which
        says more about what went wrong.
        """

        undo: List[Tuple[str, str]] = []

        for fromTable, toTable in _renameSteps(targetTable, stageTable, tempTable):
            try:
                cursor.execute(_renameStatement(fromTable, toTable))
            except Exception:
                for undoFrom, undoTo in reversed(undo):
                    try:
                        cursor.execute(_renameStatement(undoTo, undoFrom))
                    except Exception:
                        logger.warning('could not undo the rename of {} to {} after the swap failed; the tables are '
                                       'as the failure left them'.format(undoFrom, undoTo))
                raise
            undo.append((fromTable, toTable))


class MSSQLDialect(DatabaseDialect):

    databaseType = DatabaseType.MSSQL

    def connect(self, settings: DatabaseConnectionConfig) -> Tuple[Any, Any]:

        import pymssql

        connection = pymssql.connect(**self.connectArguments(settings))
        cursor = connection.cursor()

        return connection, cursor


    def _ownConnectArguments(self, settings: DatabaseConnectionConfig, password: Optional[str]) -> Dict[str, Any]:
        """pymssql takes the port as a str, and fails on None, so it's left out
        when unset.
        """

        arguments: Dict[str, Any] = {'server': settings.host, 'user': settings.user, 'password': password, 'database': settings.database}
        if settings.port is not None:
            arguments['port'] = str(settings.port)

        return arguments


    def placeholders(self, count: int) -> List[str]:

        return count * ['%s']


    def foreignKeysQuery(self) -> str:

        return ("SELECT CASE WHEN sp.name = SCHEMA_NAME() THEN tp.name ELSE sp.name + '.' + tp.name END, cp.name, "
                "CASE WHEN sr.name = SCHEMA_NAME() THEN tr.name ELSE sr.name + '.' + tr.name END, cr.name, fk.name "
                "FROM sys.foreign_keys fk "
                "JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
                "JOIN sys.tables tp ON tp.object_id = fkc.parent_object_id "
                "JOIN sys.columns cp ON cp.object_id = fkc.parent_object_id AND cp.column_id = fkc.parent_column_id "
                "JOIN sys.tables tr ON tr.object_id = fkc.referenced_object_id "
                "JOIN sys.columns cr ON cr.object_id = fkc.referenced_object_id AND cr.column_id = fkc.referenced_column_id "
                "JOIN sys.schemas sp ON sp.schema_id = tp.schema_id "
                "JOIN sys.schemas sr ON sr.schema_id = tr.schema_id "
                "ORDER BY tp.name, fk.name, fkc.constraint_column_id")


    def columnsQuery(self) -> str:
        """character_maximum_length is -1 for (MAX), which schema.py reads as unbounded."""

        return ("SELECT column_name, data_type, character_maximum_length, numeric_precision, numeric_scale, is_nullable "
                "FROM information_schema.columns WHERE table_schema = COALESCE({}, SCHEMA_NAME()) AND table_name = {} ORDER BY ordinal_position")


    def isEncrypted(self, cursor: Any) -> Optional[bool]:
        """Needs VIEW SERVER STATE; without it the query fails, and the answer
        is None.
        """

        cursor.execute('SELECT encrypt_option FROM sys.dm_exec_connections WHERE session_id = @@SPID')
        row = cursor.fetchone()

        return None if row is None else str(row[0]).upper() == 'TRUE'


    def primaryKeyQuery(self) -> str:

        return ("SELECT k.column_name FROM information_schema.table_constraints t "
                "JOIN information_schema.key_column_usage k ON k.constraint_name = t.constraint_name AND k.table_schema = t.table_schema "
                "WHERE t.constraint_type = 'PRIMARY KEY' AND t.table_schema = COALESCE({}, SCHEMA_NAME()) AND t.table_name = {} "
                "ORDER BY k.ordinal_position")


    def tableExistsQuery(self) -> str:

        return "SELECT count(*) FROM information_schema.tables WHERE table_schema = COALESCE({}, SCHEMA_NAME()) AND table_name = {}"


    def upsertQuery(self, table: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str], rowCount: int = 1) -> str:

        rowValues = ', '.join(['({})'.format(', '.join(self.placeholders(len(allColumns))))] * rowCount)
        columnNames = ', '.join(allColumns)
        mergeClause = _mergeUpdateInsertClause('target', 'source', allColumns, primaryKeyColumns, nonPrimaryKeyColumns)

        # MERGE requires a terminating semicolon in T-SQL, unlike Oracle
        return 'MERGE INTO {} AS target USING (VALUES {}) AS source ({}) {};'.format(table, rowValues, columnNames, mergeClause)


    # A VALUES list in an INSERT takes at most 1000 rows. pymssql binds
    # parameters by quoting them into the statement itself, so SQL Server's
    # 2100-parameter limit doesn't apply.
    VALUES_ROW_LIMIT = 1000

    def prepareValues(self, rows: List[Tuple[Any, ...]]) -> List[Tuple[Any, ...]]:
        """Times as ISO text, which SQL Server converts exactly: pymssql renders
        a bound datetime with milliseconds only, so the microseconds a
        DATETIME2 column holds were silently lost.
        """

        def text(value: Any) -> Any:
            if isinstance(value, datetime.datetime):
                return value.isoformat(sep=' ')
            if isinstance(value, datetime.time):
                return value.isoformat()
            return value

        rows = super().prepareValues(rows)

        if not any(isinstance(value, (datetime.datetime, datetime.time)) for row in rows for value in row):
            return rows

        return [tuple(text(value) for value in row) for row in rows]


    @staticmethod
    def _multiRowSafe(rows: Sequence[Sequence[Any]]) -> bool:
        """Whether one VALUES list can carry `rows` unchanged.

        A table value constructor takes one type per column, by SQL Server's
        data-type precedence, and converts the rest to it: a single integer in
        a text column turns '00001' into '1'. A Decimal that spells itself with
        an exponent ('1E-10') types the column float, which rounds every exact
        value beside it. Both go to the row-by-row path, which converts each
        value on its own.
        """

        kinds: Dict[int, str] = {}

        for row in rows:
            for index, value in enumerate(row):
                if value is None:
                    continue
                if isinstance(value, decimal.Decimal) and 'E' in str(value).upper():
                    return False
                if isinstance(value, bytes):
                    kind = 'bytes'
                elif isinstance(value, str):
                    kind = 'text'
                elif isinstance(value, (bool, int, float, decimal.Decimal)):
                    kind = 'number'
                else:
                    kind = 'other'
                if kinds.setdefault(index, kind) != kind:
                    return False

        return True

    def bulkInsert(self, cursor: Any, table: str, columns: List[str], rows: Sequence[Sequence[Any]]) -> bool:
        """Multi-row INSERT ... VALUES: pymssql's executemany sends a statement per row."""

        if not self._multiRowSafe(rows):
            return False

        rowValues = '({})'.format(', '.join(self.placeholders(len(columns))))

        for offset in range(0, len(rows), self.VALUES_ROW_LIMIT):
            batch = rows[offset:offset + self.VALUES_ROW_LIMIT]
            cursor.execute('INSERT INTO {} ({}) VALUES {}'.format(table, ', '.join(columns), ', '.join([rowValues] * len(batch))),
                           tuple(value for row in batch for value in row))

        return True


    def bulkUpsert(self, cursor: Any, table: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str],
                   rows: Sequence[Sequence[Any]]) -> bool:
        """One MERGE per thousand rows. `rows` hold one row per key, which MERGE
        requires: it refuses to update a target row twice.
        """

        if not self._multiRowSafe(rows):
            return False

        for offset in range(0, len(rows), self.VALUES_ROW_LIMIT):
            batch = rows[offset:offset + self.VALUES_ROW_LIMIT]
            cursor.execute(self.upsertQuery(table, allColumns, primaryKeyColumns, nonPrimaryKeyColumns, rowCount=len(batch)),
                           tuple(value for row in batch for value in row))

        return True


    def upsertFromStageQuery(self, targetTable: str, stageTable: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:

        mergeClause = _mergeUpdateInsertClause('target', 'source', allColumns, primaryKeyColumns, nonPrimaryKeyColumns)

        return 'MERGE INTO {} AS target USING {} AS source {};'.format(targetTable, stageTable, mergeClause)


    def swapQueries(self, targetTable: str, stageTable: str, tempTable: str) -> List[str]:
        """One execute(), inside the transaction, so atomic. sp_rename takes the
        new name literally, so it must be unqualified, and bare: brackets in it
        would become part of the name, and an unbalanced one is a syntax error.
        """

        def renamed(table: str) -> str:
            return bareName(unqualifiedName(table))

        return ["EXEC sp_rename '{}', '{}'; EXEC sp_rename '{}', '{}'; EXEC sp_rename '{}', '{}';".format(
            stageTable, renamed(tempTable), targetTable, renamed(stageTable), tempTable, renamed(targetTable))]

    # No columnCategory: pymssql's type codes can't be told apart without
    # importing it, so discovery samples values instead.


class MariaDBDialect(MySQLDialect):
    """MySQL's dialect and driver, unchanged: MariaDB is compatible with
    everything this uses.
    """

    databaseType = DatabaseType.MARIADB


def _registerSqliteAdapters(sqlite3: Any) -> None:
    """Teach sqlite3 the value types other drivers hand back: Decimal, which it
    refuses, and dates, whose built-in adapters are deprecated since 3.12.
    Process-wide, which is harmless for these.
    """

    import datetime
    import decimal

    sqlite3.register_adapter(decimal.Decimal, str)
    sqlite3.register_adapter(datetime.date, lambda value: value.isoformat())
    sqlite3.register_adapter(datetime.datetime, lambda value: value.isoformat(sep=' '))
    sqlite3.register_adapter(datetime.time, lambda value: value.isoformat())
    sqlite3.register_adapter(uuid.UUID, str)


class SQLiteDialect(_OnConflictDialect):
    """settings.database is a file path or ":memory:". No columnCategory:
    sqlite3 reports no column types.
    """

    databaseType = DatabaseType.SQLITE

    def connect(self, settings: DatabaseConnectionConfig) -> Tuple[Any, Any]:

        import sqlite3

        _registerSqliteAdapters(sqlite3)

        # WAL, so a writer can proceed while a stream reads the same file; the
        # default journal fails it with "database is locked". It persists in
        # the file, and needs a local filesystem, not NFS or SMB.
        connection = sqlite3.connect(**self.connectArguments(settings))
        connection.execute('PRAGMA journal_mode=WAL')
        # Declared foreign keys are enforced, as on every other database.
        # SQLite leaves them off unless each connection asks.
        connection.execute('PRAGMA foreign_keys=ON')
        cursor = connection.cursor()

        return connection, cursor


    def _ownConnectArguments(self, settings: DatabaseConnectionConfig, password: Optional[str]) -> Dict[str, Any]:

        return {'database': settings.database, 'timeout': 30.0}


    def placeholders(self, count: int) -> List[str]:

        return count * ['?']


    def supportsMaterializedSelections(self) -> bool:
        """SQLite 3.35 and later, which not every Python links against."""

        import sqlite3

        return sqlite3.sqlite_version_info >= (3, 35)


    def truncateQuery(self, table: str) -> str:
        """SQLite has no TRUNCATE; DELETE FROM is its equivalent."""

        return 'DELETE FROM {}'.format(table)


    # SQLite describes tables through pragma table-valued functions, whose
    # optional second argument is the attached database -- SQLite's schema.
    # Both are bound unquoted, since a pragma takes a name, not a statement.

    def primaryKey(self, cursor: Any, table: str) -> List[str]:

        schema, name = catalogTableName(self.databaseType, table)
        cursor.execute('SELECT name FROM pragma_table_info(?, ?) WHERE pk > 0 ORDER BY pk', (name, schema or 'main'))

        return [row[0] for row in cursor.fetchall()]


    def columnDefinitions(self, cursor: Any, table: str) -> List[ColumnDefinition]:
        """SQLite keeps only the declared type text, e.g. `VARCHAR(50)` or
        `DECIMAL(10,2)`; the length, precision and scale are parsed out of it.
        """

        schema, name = catalogTableName(self.databaseType, table)
        cursor.execute('SELECT name, type, "notnull", pk FROM pragma_table_info(?, ?) ORDER BY cid', (name, schema or 'main'))
        definitions = []

        for columnName, declared, notNull, primaryKey in cursor.fetchall():
            match = re.match(r'^\s*([A-Za-z ]+?)\s*(?:\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\))?\s*$', declared or '')
            dataType = match.group(1) if match else (declared or '')
            first = int(match.group(2)) if match and match.group(2) else None
            second = int(match.group(3)) if match and match.group(3) else None
            numeric = any(word in dataType.upper() for word in ('DEC', 'NUM'))
            definitions.append(ColumnDefinition(
                name=columnName, dataType=dataType, length=None if numeric else first, precision=first if numeric else None,
                scale=second if numeric else None, nullable=not notNull and not primaryKey))

        return definitions


    def tableExists(self, cursor: Any, table: str) -> bool:

        schema, name = catalogTableName(self.databaseType, table)
        cursor.execute("SELECT count(*) FROM {}.sqlite_master WHERE type = 'table' AND lower(name) = lower(?)".format(
            quoteIdentifier(self.databaseType, schema) if schema else 'main'), (name,))

        return bool(cursor.fetchone()[0])


    def foreignKeys(self, cursor: Any) -> List[ForeignKey]:
        """SQLite keeps foreign keys per table, behind a pragma, so this lists
        the tables and asks each. A reference that omits its columns means the
        referenced table's primary key, which is resolved here.
        """

        cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]
        rows = []

        for table in tables:
            cursor.execute('SELECT id, "table", "from", "to" FROM pragma_foreign_key_list(?) ORDER BY id, seq', (table,))
            references = cursor.fetchall()

            primaryKeys: Dict[str, List[str]] = {}
            positions: Dict[int, int] = {}

            for constraintId, referencedTable, column, referencedColumn in references:
                position = positions.get(constraintId, 0)
                positions[constraintId] = position + 1

                if referencedColumn is None:
                    if referencedTable not in primaryKeys:
                        primaryKeys[referencedTable] = self.primaryKey(cursor, referencedTable)
                    referencedColumn = primaryKeys[referencedTable][position]

                rows.append((table, column, referencedTable, referencedColumn, '{}_fk{}'.format(table, constraintId)))

        return _groupForeignKeys(rows)


    def swapQueries(self, targetTable: str, stageTable: str, tempTable: str) -> List[str]:
        """Three statements in an explicit transaction: sqlite3 doesn't open
        one before DDL, so each rename would otherwise commit alone.
        """

        return ['BEGIN'] + _renameInThreeSteps(targetTable, stageTable, tempTable)


    def swap(self, cursor: Any, targetTable: str, stageTable: str, tempTable: str) -> None:
        """Renames with legacy_alter_table on, so a view built on the target
        keeps reading the target.

        SQLite rewrites the views and triggers that name a renamed table, to
        follow it. A swap renames the table out of the way, so every view on
        the target was rewritten to read the stage table -- the old rows, and
        emptied by the next run -- and stayed that way. Renaming the name
        rather than the table is what a swap means; see "How a swap works" in
        docs/design.md.
        """

        cursor.execute('PRAGMA legacy_alter_table=ON')
        try:
            super().swap(cursor, targetTable, stageTable, tempTable)
        finally:
            cursor.execute('PRAGMA legacy_alter_table=OFF')

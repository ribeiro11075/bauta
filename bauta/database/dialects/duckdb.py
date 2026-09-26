"""DuckDB, in-process, through its own Python driver."""
from __future__ import annotations

import decimal
import json
import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, cast

from ..driver import Connection, Cursor
from ...configuration import ConfigurationError, ConnectionConfig, DatabaseType, DuckDBConnection
from .base import ColumnCategory, ForeignKey, settingsOf, _OnConflictDialect, _groupForeignKeys, _renameInThreeSteps
from .names import catalogTableName


# How long a connection waits for another process to let go of the file.
# DuckDB lets one process at a time open a database file. The runner starts
# one job at a time per DuckDB connection, so what holds it is something
# else: another run, or a program with the file open. Past this, it fails
# saying why.
LOCK_WAIT_SECONDS = 60.0

LOCK_POLL_SECONDS = 0.5

# What DuckDB says when another process holds the file.
_LOCK_MESSAGE = 'Could not set lock on file'

# The chunk is registered under this name for the statement that loads it.
_CHUNK_VIEW = 'bauta_chunk'


def _arrowValue(value: Any) -> Any:
    """A value as Arrow should see it: a dictionary, which a JSON column is
    read as elsewhere, as its JSON text rather than an Arrow struct.
    """

    return json.dumps(value) if isinstance(value, dict) else value


# What a column Arrow can't type may hold and still be sent as text: DuckDB
# casts the text to the column's type, as it casts each value row by row.
_AS_TEXT = (int, float, decimal.Decimal, str)


def _asArrowTable(rows: Sequence[Sequence[Any]]) -> Any:
    """`rows` as a pyarrow Table, or None where pyarrow isn't installed or a
    column can't be sent through Arrow at all.

    A column Arrow can't give one type -- SQLite hands back numbers and text
    together in one, and a Python integer can outgrow any Arrow integer --
    goes as text where it holds only numbers and text, rather than sending the
    whole chunk row by row.
    """

    try:
        import pyarrow
    except ImportError:
        return None

    arrays = {}
    for index, values in enumerate(zip(*rows)):
        try:
            arrays['c{}'.format(index)] = pyarrow.array([_arrowValue(value) for value in values])
        except (pyarrow.ArrowException, TypeError, ValueError, OverflowError):
            if not all(value is None or (isinstance(value, _AS_TEXT) and not isinstance(value, bool)) for value in values):
                return None
            arrays['c{}'.format(index)] = pyarrow.array([None if value is None else str(value) for value in values], type=pyarrow.string())

    return pyarrow.table(arrays)


# Statements whose result is the number of rows they changed, which DuckDB
# returns as a row rather than as the cursor's rowcount.
_COUNTING_STATEMENT = re.compile(r'^\s*(INSERT|UPDATE|DELETE)\b', re.IGNORECASE)


class _Session:
    """A DuckDB connection that behaves as the other drivers do (see driver.py):
    a statement opens a transaction, which lasts until commit() or rollback(),
    and rollback() with nothing open does nothing. DuckDB itself commits each
    statement on its own.

    It is the cursor as well as the connection: DuckDB's cursor() opens a
    second connection with a transaction of its own, which this one's commit()
    doesn't commit.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection  # duckdb.DuckDBPyConnection, which is what this makes a Connection of
        self._inTransaction = False
        self.rowcount = -1


    def execute(self, query: str, parameters: Optional[Sequence[Any]] = None) -> '_Session':

        self._begin()
        self._connection.execute(query, parameters)
        self._countRows(query)

        return self


    def executemany(self, query: str, parameters: Sequence[Sequence[Any]]) -> '_Session':

        self._begin()
        self._connection.executemany(query, parameters)
        self.rowcount = -1

        return self


    def _begin(self) -> None:

        if not self._inTransaction:
            self._connection.execute('BEGIN TRANSACTION')
            self._inTransaction = True


    def _countRows(self, query: str) -> None:

        self.rowcount = -1
        if _COUNTING_STATEMENT.match(query):
            row = self._connection.fetchone()
            self.rowcount = int(row[0]) if row else -1


    def commit(self) -> None:

        if self._inTransaction:
            self._inTransaction = False
            self._connection.execute('COMMIT')


    def rollback(self) -> None:

        if self._inTransaction:
            self._inTransaction = False
            self._connection.execute('ROLLBACK')


    def close(self) -> None:
        """Closing rolls back whatever wasn't committed, as it does elsewhere."""

        self._inTransaction = False
        self._connection.close()


    @property
    def description(self) -> Any:

        return self._connection.description


    def fetchone(self) -> Any:

        return self._connection.fetchone()


    def fetchmany(self, size: int) -> List[Any]:

        return self._connection.fetchmany(size)


    def fetchall(self) -> List[Any]:

        return self._connection.fetchall()


    def cursor(self) -> Any:

        return self._connection.cursor()


    def register(self, name: str, value: Any) -> None:

        self._connection.register(name, value)


    def unregister(self, name: str) -> None:

        self._connection.unregister(name)


class DuckDBDialect(_OnConflictDialect):
    """settings.path is a file path or ":memory:". Its SQL is close to
    PostgreSQL's, and like SQLite it runs in this process, with no server or
    login.

    Views resolve their tables by name, so a swap needs nothing done to them.
    A table in a foreign key can't be swapped at all; see swap().
    """

    databaseType = DatabaseType.DUCKDB

    _NUMBER_TYPES = {'TINYINT', 'SMALLINT', 'INTEGER', 'BIGINT', 'HUGEINT', 'UTINYINT', 'USMALLINT', 'UINTEGER', 'UBIGINT', 'UHUGEINT',
                     'FLOAT', 'DOUBLE', 'DECIMAL'}
    _DATE_TYPES = {'DATE', 'TIMESTAMP', 'TIMESTAMP WITH TIME ZONE', 'TIMESTAMP_S', 'TIMESTAMP_MS', 'TIMESTAMP_NS'}
    _TEXT_TYPES = {'VARCHAR'}

    def openConnection(self, settings: ConnectionConfig) -> Any:

        import duckdb

        arguments = self.connectArguments(settings)
        deadline = time.monotonic() + LOCK_WAIT_SECONDS

        while True:
            try:
                return _Session(duckdb.connect(**arguments))
            except duckdb.IOException as error:
                if _LOCK_MESSAGE not in str(error):
                    raise
                if time.monotonic() >= deadline:
                    raise ConfigurationError(
                        '{} is held open by another process, and DuckDB lets one process at a time open a file. Close whatever else has '
                        'it open -- another run, or a program reading it -- and run again'.format(settings.describeTarget())) from error
                time.sleep(LOCK_POLL_SECONDS)


    def prepareSession(self, connection: Connection, settings: ConnectionConfig) -> Any:
        """The session is its own cursor; see _Session. Its time zone is UTC:
        DuckDB's default is the machine's own, which it converts through
        whenever a time-zone-aware value meets a column without one, so the
        same job would store different times on machines in different zones.
        """

        _prepare(connection, settingsOf(settings, DuckDBConnection).currentSchema)
        connection.commit()

        return connection


    def streamingCursor(self, connection: Connection, chunkSize: int) -> Any:
        """A cursor of its own, so a statement run on the session mid-stream
        doesn't cut the stream short. Being a connection of its own, it starts
        in the default schema, so it is given the session's. It sees only what
        the session has committed.
        """

        schema = _session(connection).execute('SELECT current_schema()').fetchone()[0]
        cursor = connection.cursor()
        _prepare(cursor, schema)

        return cursor


    def _ownConnectArguments(self, settings: ConnectionConfig, password: Optional[str]) -> Dict[str, Any]:

        return {'database': settingsOf(settings, DuckDBConnection).path}


    def connectArguments(self, settings: ConnectionConfig, resolvePassword: bool = True) -> Dict[str, Any]:
        """`options` are DuckDB's own settings (`memory_limit`, `threads`, ...),
        which duckdb.connect takes as one `config` mapping.
        """

        arguments = self._ownConnectArguments(settings, None)
        if settings.options:
            arguments['config'] = dict(settings.options)

        return arguments


    def placeholders(self, count: int) -> List[str]:

        return count * ['?']


    def supportsMaterializedSelections(self) -> bool:

        return True


    def checksForeignKeysWithinTransaction(self) -> bool:

        return False


    def bulkInsert(self, cursor: Cursor, table: str, columns: List[str], rows: Sequence[Sequence[Any]]) -> bool:
        """The chunk as an Arrow table, loaded by one INSERT ... SELECT: DuckDB's
        executemany runs its statement once per row, about a hundred times
        slower.
        """

        return self._loadThroughArrow(cursor, rows, lambda view: 'INSERT INTO {} ({}) SELECT * FROM {}'.format(
            table, ', '.join(columns), view))


    def bulkUpsert(self, cursor: Cursor, table: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str],
                   rows: Sequence[Sequence[Any]]) -> bool:

        return self._loadThroughArrow(cursor, rows, lambda view: 'INSERT INTO {} ({}) SELECT * FROM {} WHERE true {}'.format(
            table, ', '.join(allColumns), view, self._onConflict(primaryKeyColumns, nonPrimaryKeyColumns)))


    def _loadThroughArrow(self, cursor: Cursor, rows: Sequence[Sequence[Any]], statement: Callable[[str], str]) -> bool:

        chunk = _asArrowTable(rows)
        if chunk is None:
            return False

        _session(cursor).register(_CHUNK_VIEW, chunk)
        try:
            cursor.execute(statement(_CHUNK_VIEW))
        finally:
            _session(cursor).unregister(_CHUNK_VIEW)

        return True


    def columnCategory(self, dataType: Any) -> Optional[ColumnCategory]:
        """DuckDB's cursor.description gives a type object whose text is the
        SQL type, `DECIMAL(10,2)` included.
        """

        name = str(dataType).upper().split('(')[0].strip()

        if name in self._NUMBER_TYPES:
            return ColumnCategory.NUMBER
        if name in self._DATE_TYPES:
            return ColumnCategory.DATE
        if name in self._TEXT_TYPES:
            return ColumnCategory.TEXT

        return None


    # The bound names arrive as the catalog holds them. DuckDB compares names
    # without regard to case, so the lookups do too.

    def columnsQuery(self) -> str:
        """DuckDB keeps no length for VARCHAR(n), so `length` is always NULL."""

        return ("SELECT column_name, data_type, character_maximum_length, numeric_precision, numeric_scale, is_nullable "
                "FROM information_schema.columns WHERE lower(table_schema) = lower(COALESCE({}, current_schema())) "
                "AND lower(table_name) = lower({}) AND table_catalog = current_database() ORDER BY ordinal_position")


    def primaryKeyQuery(self) -> str:

        return ("SELECT unnest(constraint_column_names) FROM duckdb_constraints() "
                "WHERE constraint_type = 'PRIMARY KEY' AND lower(schema_name) = lower(COALESCE({}, current_schema())) "
                "AND lower(table_name) = lower({}) AND database_name = current_database()")


    def tableExistsQuery(self) -> str:

        return ("SELECT count(*) FROM information_schema.tables "
                "WHERE lower(table_schema) = lower(COALESCE({}, current_schema())) AND lower(table_name) = lower({}) "
                "AND table_catalog = current_database()")


    def listTablesQuery(self) -> str:

        return ("SELECT table_name FROM information_schema.tables "
                "WHERE lower(table_schema) = lower(COALESCE({}, current_schema())) AND table_type = 'BASE TABLE' "
                "AND table_catalog = current_database() ORDER BY table_name")


    def foreignKeys(self, cursor: Cursor) -> List[ForeignKey]:
        """DuckDB lists a constraint's columns as arrays, in key order, and only
        allows a foreign key within one schema.
        """

        cursor.execute("SELECT table_name, constraint_column_names, referenced_table, referenced_column_names, constraint_name "
                       "FROM duckdb_constraints() WHERE constraint_type = 'FOREIGN KEY' AND schema_name = current_schema() "
                       "AND database_name = current_database() ORDER BY table_name, constraint_name")

        rows: List[Tuple[Any, ...]] = []
        for table, columns, referencedTable, referencedColumns, name in cursor.fetchall():
            rows.extend((table, column, referencedTable, referencedColumn, name) for column, referencedColumn in zip(columns, referencedColumns))

        return _groupForeignKeys(rows)


    def swap(self, cursor: Cursor, targetTable: str, stageTable: str, tempTable: str) -> None:
        """Refused, before anything is renamed, where either table is in a
        foreign key or has an index of its own. DuckDB won't rename a table
        another references, or one with an index; and renaming one that
        references another leaves that other table naming it by its old name:
        from then on it can't be dropped, even once the referencing table is
        gone. A primary key is no obstacle.
        """

        problems = []
        for table in (targetTable, stageTable):
            schema, name = catalogTableName(self.databaseType, table)
            where = "lower(schema_name) = lower(COALESCE(?, current_schema())) AND database_name = current_database()"
            cursor.execute("SELECT count(*) FROM duckdb_constraints() WHERE constraint_type = 'FOREIGN KEY' AND {} "
                           "AND (lower(table_name) = lower(?) OR lower(referenced_table) = lower(?))".format(where), (schema, name, name))
            if cursor.fetchone()[0]:
                problems.append('{} is in a foreign key'.format(table))
            cursor.execute('SELECT count(*) FROM duckdb_indexes() WHERE {} AND lower(table_name) = lower(?)'.format(where), (schema, name))
            if cursor.fetchone()[0]:
                problems.append('{} has an index'.format(table))

        if problems:
            raise ConfigurationError('{}, and DuckDB cannot swap such a table: it refuses to rename a table with an index or one another '
                                     'references, and renaming one that references another corrupts its catalog. Use insertStrategy: '
                                     'upsert'.format('; '.join(problems)))

        super().swap(cursor, targetTable, stageTable, tempTable)


    def swapQueries(self, targetTable: str, stageTable: str, tempTable: str) -> List[str]:
        """Three renames in the session's one transaction, so a failure
        part-way leaves both tables as they were.
        """

        return _renameInThreeSteps(targetTable, stageTable, tempTable)


def _prepare(connection: Any, schema: Optional[str]) -> None:
    """What every connection to DuckDB is given: UTC, and the schema."""

    connection.execute("SET TimeZone = 'UTC'")
    if schema:
        connection.execute('SET schema = {}'.format(_quotedString(schema)))


def _session(connectionOrCursor: Any) -> _Session:
    """The session a DuckDB connection's cursor is -- see _Session -- for the
    driver features it passes through.
    """

    return cast(_Session, connectionOrCursor)


def _quotedString(value: str) -> str:

    return "'{}'".format(value.replace("'", "''"))

"""SQL Server, through pymssql."""
from __future__ import annotations

import decimal
from typing import Any, Dict, List, Optional, Sequence

from ..driver import Cursor
from ...configuration import ConnectionConfig, DatabaseType, MSSQLConnection
from .base import DatabaseDialect, settingsOf, _mergeUpdateInsertClause
from .names import bareName, unqualifiedName


def _mssqlKind(kind: type) -> str:
    """Which of SQL Server's type families a Python type lands in, for
    MSSQLDialect._multiRowSafe.
    """

    if issubclass(kind, bytes):
        return 'bytes'
    if issubclass(kind, str):
        return 'text'
    if issubclass(kind, (bool, int, float, decimal.Decimal)):
        return 'number'

    return 'other'


class MSSQLDialect(DatabaseDialect):

    databaseType = DatabaseType.MSSQL

    def openConnection(self, settings: ConnectionConfig) -> Any:

        import pymssql

        return pymssql.connect(**self.connectArguments(settings))


    def _ownConnectArguments(self, settings: ConnectionConfig, password: Optional[str]) -> Dict[str, Any]:
        """pymssql takes the port as a str, and fails on None, so it's left out
        when unset.
        """

        settings = settingsOf(settings, MSSQLConnection)
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


    def isEncrypted(self, cursor: Cursor) -> Optional[bool]:
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


    def listTablesQuery(self) -> str:
        """sys.tables rather than information_schema, for is_ms_shipped: a
        database carries tables SQL Server installed in it -- `master` has
        MSreplication_options and the spt_ ones -- and information_schema
        reports those as ordinary base tables of dbo. sys.tables also holds no
        views, which live in sys.views.
        """

        return ("SELECT t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
                "WHERE s.name = COALESCE({}, SCHEMA_NAME()) AND t.is_ms_shipped = 0 ORDER BY t.name")


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

        # Column by column, over each column's distinct types rather than its
        # values, since this runs on every chunk.
        for column in zip(*rows):
            types = set(map(type, column))
            types.discard(type(None))
            if len({_mssqlKind(kind) for kind in types}) > 1:
                return False
            if any(issubclass(kind, decimal.Decimal) for kind in types) and \
                    any(isinstance(value, decimal.Decimal) and 'E' in str(value).upper() for value in column):
                return False

        return True

    def bulkInsert(self, cursor: Cursor, table: str, columns: List[str], rows: Sequence[Sequence[Any]]) -> bool:
        """Multi-row INSERT ... VALUES: pymssql's executemany sends a statement per row."""

        if not self._multiRowSafe(rows):
            return False

        rowValues = '({})'.format(', '.join(self.placeholders(len(columns))))

        for offset in range(0, len(rows), self.VALUES_ROW_LIMIT):
            batch = rows[offset:offset + self.VALUES_ROW_LIMIT]
            cursor.execute('INSERT INTO {} ({}) VALUES {}'.format(table, ', '.join(columns), ', '.join([rowValues] * len(batch))),
                           tuple(value for row in batch for value in row))

        return True


    def bulkUpsert(self, cursor: Cursor, table: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str],
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

        def literal(text: str) -> str:
            # Both arguments are string literals, so a quote in a table's name
            # ends the literal unless doubled.
            return "'{}'".format(text.replace("'", "''"))

        def renamed(table: str) -> str:
            return literal(bareName(unqualifiedName(table)))

        return ['EXEC sp_rename {}, {}; EXEC sp_rename {}, {}; EXEC sp_rename {}, {};'.format(
            literal(stageTable), renamed(tempTable), literal(targetTable), renamed(stageTable), literal(tempTable), renamed(targetTable))]

    # No columnCategory: pymssql's type codes can't be told apart without
    # importing it, so discovery samples values instead.

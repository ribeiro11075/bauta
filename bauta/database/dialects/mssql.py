"""SQL Server, through pymssql."""
from __future__ import annotations

import decimal
from typing import Any, Dict, List, Optional, Sequence

from ..driver import Cursor
from ...configuration import DatabaseConfig, DatabaseType, MSSQLConnection
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

    REFUSED_FLOATS = frozenset({'nan', 'inf'})

    def openConnection(self, settings: DatabaseConfig) -> Any:

        import pymssql

        return pymssql.connect(**self.connectArguments(settings))


    def _ownConnectArguments(self, settings: DatabaseConfig, password: Optional[str]) -> Dict[str, Any]:
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


    # Bound twice: without a schema, every schema's keys, as SQL Server has
    # always been read, so a subset follows a child table in another schema.
    FOREIGN_KEY_SCHEMA_BINDS = 2

    def foreignKeysQuery(self) -> str:

        return ("SELECT sp.name, tp.name, cp.name, sr.name, tr.name, cr.name, fk.name, SCHEMA_NAME() "
                "FROM sys.foreign_keys fk "
                "JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
                "JOIN sys.tables tp ON tp.object_id = fkc.parent_object_id "
                "JOIN sys.columns cp ON cp.object_id = fkc.parent_object_id AND cp.column_id = fkc.parent_column_id "
                "JOIN sys.tables tr ON tr.object_id = fkc.referenced_object_id "
                "JOIN sys.columns cr ON cr.object_id = fkc.referenced_object_id AND cr.column_id = fkc.referenced_column_id "
                "JOIN sys.schemas sp ON sp.schema_id = tp.schema_id "
                "JOIN sys.schemas sr ON sr.schema_id = tr.schema_id "
                "WHERE {} IS NULL OR sp.name = {} "
                "ORDER BY sp.name, tp.name, fk.name, fkc.constraint_column_id")


    def foreignKeyCountsQuery(self) -> str:

        return ("SELECT s.name, count(*), SCHEMA_NAME() FROM sys.foreign_keys fk JOIN sys.schemas s ON s.schema_id = fk.schema_id "
                "GROUP BY s.name")


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


    def uniqueKeysQuery(self) -> str:
        """Unique indexes, which a unique constraint is enforced by, without a
        filter and only their key columns, not those they INCLUDE. A computed
        column comes back NULL, since a stage table has none to match.
        """

        return ("SELECT i.name, CASE WHEN c.is_computed = 0 THEN c.name END FROM sys.indexes i "
                "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
                "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
                "JOIN sys.tables t ON t.object_id = i.object_id JOIN sys.schemas s ON s.schema_id = t.schema_id "
                "WHERE i.is_unique = 1 AND i.is_primary_key = 0 AND i.has_filter = 0 AND i.is_disabled = 0 AND ic.is_included_column = 0 "
                "AND s.name = COALESCE({}, SCHEMA_NAME()) AND t.name = {} "
                "ORDER BY i.name, ic.key_ordinal")


    # How sys.columns' type, size and collation are written back into a
    # column definition, for making a column NOT NULL: ALTER COLUMN restates
    # the whole of it, and a size or collation left out would change it.
    _SIZED_IN_BYTES = {'varchar', 'char', 'varbinary', 'binary'}
    _SIZED_IN_CHARACTERS = {'nvarchar', 'nchar'}
    _SCALED = {'datetime2', 'time', 'datetimeoffset'}

    def _columnDefinition(self, typeName: str, maxLength: int, precision: int, scale: int, collation: Optional[str]) -> str:

        if typeName in self._SIZED_IN_BYTES | self._SIZED_IN_CHARACTERS:
            size = 'max' if maxLength == -1 else str(maxLength // 2 if typeName in self._SIZED_IN_CHARACTERS else maxLength)
            rendered = '{}({})'.format(typeName, size)
        elif typeName in ('decimal', 'numeric'):
            rendered = '{}({}, {})'.format(typeName, precision, scale)
        elif typeName in self._SCALED:
            rendered = '{}({})'.format(typeName, scale)
        else:
            rendered = typeName

        return rendered + (' COLLATE {}'.format(collation) if collation else '')


    def addKeys(self, cursor: Cursor, table: str, catalogTable: str, primaryKey: Sequence[str], uniqueKeys: Sequence[Sequence[str]]) -> None:
        """SQL Server refuses a primary key over a column that allows NULL, as
        the other databases quietly make it NOT NULL, so each such column is
        made NOT NULL first, restated with its own type and collation.
        """

        if primaryKey:
            cursor.execute('SELECT c.name, TYPE_NAME(c.user_type_id), c.max_length, c.precision, c.scale, c.collation_name '
                           'FROM sys.columns c WHERE c.object_id = OBJECT_ID(%s) AND c.is_nullable = 1', (table,))
            nullable = {row[0].upper(): row[1:] for row in cursor.fetchall()}
            for column in primaryKey:
                found = nullable.get(bareName(column).upper())
                if found is not None:
                    cursor.execute('ALTER TABLE {} ALTER COLUMN {} {} NOT NULL'.format(table, column, self._columnDefinition(*found)))

        super().addKeys(cursor, table, catalogTable, primaryKey, uniqueKeys)


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


    def upsertQuery(self, table: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str], rowCount: int = 1,
                    placeholders: Optional[List[str]] = None) -> str:

        rowValues = ', '.join(['({})'.format(', '.join(placeholders or self.placeholders(len(allColumns))))] * rowCount)
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

    def _valuePlaceholders(self, rows: Sequence[Sequence[Any]], count: int) -> Optional[List[str]]:
        """The placeholders for `rows`, or None where the plain ones do.

        pymssql writes an empty bytes value into the statement as '', a
        varchar, which SQL Server won't convert to a binary column on its
        own: an empty bytea failed to load into VARBINARY(MAX). A column
        holding one is converted explicitly, which SQL Server allows; a
        non-empty value, written as 0x..., converts the same.
        """

        empty = set()
        for index, column in enumerate(zip(*rows)):
            # A bytes column's types are few, and checked before its values.
            if bytes in set(map(type, column)) and any(type(value) is bytes and not value for value in column):
                empty.add(index)

        if not empty:
            return None

        return ['CONVERT(VARBINARY(MAX), %s)' if index in empty else '%s' for index in range(count)]


    def _rowByRow(self, cursor: Cursor, statement: str, rows: Sequence[Sequence[Any]]) -> None:
        """Each row on its own, the way executemany would, for rows the
        plain placeholders can't carry.
        """

        for row in rows:
            cursor.execute(statement, tuple(row))


    def bulkInsert(self, cursor: Cursor, table: str, columns: List[str], rows: Sequence[Sequence[Any]]) -> bool:
        """Multi-row INSERT ... VALUES: pymssql's executemany sends a statement per row."""

        placeholders = self._valuePlaceholders(rows, len(columns))

        if not self._multiRowSafe(rows):
            if placeholders is None:
                return False
            self._rowByRow(cursor, 'INSERT INTO {} ({}) VALUES ({})'.format(table, ', '.join(columns), ', '.join(placeholders)), rows)
            return True

        rowValues = '({})'.format(', '.join(placeholders or self.placeholders(len(columns))))

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

        placeholders = self._valuePlaceholders(rows, len(allColumns))

        if not self._multiRowSafe(rows):
            if placeholders is None:
                return False
            self._rowByRow(cursor, self.upsertQuery(table, allColumns, primaryKeyColumns, nonPrimaryKeyColumns, placeholders=placeholders), rows)
            return True

        for offset in range(0, len(rows), self.VALUES_ROW_LIMIT):
            batch = rows[offset:offset + self.VALUES_ROW_LIMIT]
            cursor.execute(self.upsertQuery(table, allColumns, primaryKeyColumns, nonPrimaryKeyColumns, rowCount=len(batch), placeholders=placeholders),
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

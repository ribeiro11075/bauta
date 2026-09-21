"""SQLite, from Python's own sqlite3."""
from __future__ import annotations

import datetime
import decimal
import re
import uuid
from typing import Any, Dict, List, Optional

from ...configuration import DatabaseConnectionConfig, DatabaseType
from .base import ColumnDefinition, ForeignKey, _OnConflictDialect, _groupForeignKeys, _renameInThreeSteps
from .names import catalogName, catalogTableName, quoteIdentifier



def _registerSqliteAdapters(sqlite3: Any) -> None:
    """Teach sqlite3 the value types other drivers hand back: Decimal, which it
    refuses, and dates, whose built-in adapters are deprecated since 3.12.
    Process-wide, which is harmless for these.
    """


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

    def openConnection(self, settings: DatabaseConnectionConfig) -> Any:

        import sqlite3

        _registerSqliteAdapters(sqlite3)

        return sqlite3.connect(**self.connectArguments(settings))


    def prepareSession(self, connection: Any, settings: DatabaseConnectionConfig) -> Any:

        # WAL, so a writer can proceed while a stream reads the same file; the
        # default journal fails it with "database is locked". It persists in
        # the file, and needs a local filesystem, not NFS or SMB.
        connection.execute('PRAGMA journal_mode=WAL')
        # Declared foreign keys are enforced, as on every other database.
        # SQLite leaves them off unless each connection asks.
        connection.execute('PRAGMA foreign_keys=ON')

        return connection.cursor()


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


    def listTables(self, cursor: Any, schema: Optional[str] = None) -> List[str]:
        """SQLite has no catalog to bind a name against: its schema is an
        attached database, which names the sqlite_master to read rather than a
        value in one, so it is quoted into the statement as tableExists does.

        `sqlite_%` is reserved for SQLite's own tables, which are not a copy's.
        """

        attached = quoteIdentifier(self.databaseType, catalogName(self.databaseType, schema)) if schema else 'main'
        cursor.execute("SELECT name FROM {}.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name".format(attached))

        return [row[0] for row in cursor.fetchall()]


    def foreignKeys(self, cursor: Any) -> List[ForeignKey]:
        """SQLite keeps foreign keys per table, behind a pragma, so this lists
        the tables and asks each. A reference that omits its columns means the
        referenced table's primary key, which is resolved here.
        """

        tables = self.listTables(cursor)
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

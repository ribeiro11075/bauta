"""SQLite, from Python's own sqlite3."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..driver import Connection, Cursor, native
from ...configuration import ConnectionConfig, DatabaseType, SQLiteConnection
from .base import ColumnDefinition, ForeignKey, settingsOf, _OnConflictDialect, _groupForeignKeys, _renameInThreeSteps
from .names import catalogName, catalogTableName, quoteIdentifier


class SQLiteDialect(_OnConflictDialect):
    """settings.path is a file path or ":memory:". No columnCategory:
    sqlite3 reports no column types.
    """

    databaseType = DatabaseType.SQLITE

    def openConnection(self, settings: ConnectionConfig) -> Any:

        import sqlite3

        return sqlite3.connect(**self.connectArguments(settings))


    def prepareSession(self, connection: Connection, settings: ConnectionConfig) -> Any:

        # WAL, so a writer can proceed while a stream reads the same file; the
        # default journal fails it with "database is locked". It persists in
        # the file, and needs a local filesystem, not NFS or SMB.
        native(connection).execute('PRAGMA journal_mode=WAL')
        # Declared foreign keys are enforced, as on every other database.
        # SQLite leaves them off unless each connection asks.
        native(connection).execute('PRAGMA foreign_keys=ON')

        return connection.cursor()


    def _ownConnectArguments(self, settings: ConnectionConfig, password: Optional[str]) -> Dict[str, Any]:

        return {'database': settingsOf(settings, SQLiteConnection).path, 'timeout': 30.0}


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

    def primaryKey(self, cursor: Cursor, table: str) -> List[str]:

        schema, name = catalogTableName(self.databaseType, table)
        cursor.execute('SELECT name FROM pragma_table_info(?, ?) WHERE pk > 0 ORDER BY pk', (name, schema or 'main'))

        return [row[0] for row in cursor.fetchall()]


    def columnDefinitions(self, cursor: Cursor, table: str) -> List[ColumnDefinition]:
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


    def tableExists(self, cursor: Cursor, table: str) -> bool:

        schema, name = catalogTableName(self.databaseType, table)
        cursor.execute("SELECT count(*) FROM {}.sqlite_master WHERE type = 'table' AND lower(name) = lower(?)".format(
            quoteIdentifier(self.databaseType, schema) if schema else 'main'), (name,))

        return bool(cursor.fetchone()[0])


    def listTables(self, cursor: Cursor, schema: Optional[str] = None) -> List[str]:
        """SQLite has no catalog to bind a name against: its schema is an
        attached database, which names the sqlite_master to read rather than a
        value in one, so it is quoted into the statement as tableExists does.

        `sqlite_%` is reserved for SQLite's own tables, which are not a copy's.
        """

        attached = quoteIdentifier(self.databaseType, catalogName(self.databaseType, schema)) if schema else 'main'
        cursor.execute("SELECT name FROM {}.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name".format(attached))

        return [row[0] for row in cursor.fetchall()]


    def foreignKeys(self, cursor: Cursor) -> List[ForeignKey]:
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


    def swap(self, cursor: Cursor, targetTable: str, stageTable: str, tempTable: str) -> None:
        """Renames with legacy_alter_table on, so a view built on the target
        keeps reading the target.

        SQLite otherwise rewrites the views and triggers that name a renamed
        table, to follow it: the swap renames the target out of the way, so
        every view on it would go on reading the stage table -- the old rows,
        emptied by the next run. Renaming the name rather than the table is
        what a swap means; see "How a swap works" in docs/design.md.
        """

        cursor.execute('PRAGMA legacy_alter_table=ON')
        try:
            super().swap(cursor, targetTable, stageTable, tempTable)
        finally:
            cursor.execute('PRAGMA legacy_alter_table=OFF')

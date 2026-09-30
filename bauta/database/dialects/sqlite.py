"""SQLite, from Python's own sqlite3."""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from ..driver import Connection, Cursor, native
from ...configuration import DatabaseConfig, DatabaseType, SQLiteConnection
from .base import ColumnDefinition, ForeignKey, settingsOf, _OnConflictDialect, _renameInThreeSteps, _schemaForeignKeys
from .names import catalogName, catalogTableName, quoteIdentifier


# How long a connection keeps trying to switch a file to WAL while another holds it.
WAL_SWITCH_SECONDS = 30.0


def _useWriteAheadLog(connection: Any) -> None:
    """Switches the file to WAL, unless it already is. The switch takes an
    exclusive lock, and doesn't wait for one as other statements do: two
    processes opening a new file at once -- two jobs, an orchestrator's two
    tasks -- would fail one of them with "database is locked". So a file
    already in WAL is left alone, which is every file after its first
    connection, and the switch is retried while another connection holds it.
    """

    import sqlite3

    deadline = time.monotonic() + WAL_SWITCH_SECONDS
    while connection.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
        try:
            # Whatever mode results is kept: :memory: stays in memory.
            connection.execute('PRAGMA journal_mode=WAL')
            return
        except sqlite3.OperationalError as error:
            if 'locked' not in str(error) or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


class SQLiteDialect(_OnConflictDialect):
    """settings.path is a file path or ":memory:". No columnCategory:
    sqlite3 reports no column types.
    """

    databaseType = DatabaseType.SQLITE

    def openConnection(self, settings: DatabaseConfig) -> Any:

        import sqlite3

        return sqlite3.connect(**self.connectArguments(settings))


    def prepareSession(self, connection: Connection, settings: DatabaseConfig) -> Any:

        # WAL, so a writer can proceed while a stream reads the same file; the
        # default journal fails it with "database is locked". It persists in
        # the file, and needs a local filesystem, not NFS or SMB.
        _useWriteAheadLog(native(connection))
        # Declared foreign keys are enforced, as on every other database.
        # SQLite leaves them off unless each connection asks.
        native(connection).execute('PRAGMA foreign_keys=ON')

        return connection.cursor()


    def _ownConnectArguments(self, settings: DatabaseConfig, password: Optional[str]) -> Dict[str, Any]:

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


    def foreignKeys(self, cursor: Cursor, schema: Optional[str] = None) -> List[ForeignKey]:
        """SQLite keeps foreign keys per table, behind a pragma, so this lists
        the tables and asks each. A reference that omits its columns means the
        referenced table's primary key, which is resolved here.

        A schema is an attached database, and a key never leaves the database
        it is declared in, so both ends are in `schema`, or `main`.
        """

        attached = catalogName(self.databaseType, schema) if schema is not None else 'main'
        tables = self.listTables(cursor, schema)
        rows = []

        for table in tables:
            cursor.execute('SELECT id, "table", "from", "to" FROM pragma_foreign_key_list(?, ?) ORDER BY id, seq', (table, attached))
            references = cursor.fetchall()

            primaryKeys: Dict[str, List[str]] = {}
            positions: Dict[int, int] = {}

            for constraintId, referencedTable, column, referencedColumn in references:
                position = positions.get(constraintId, 0)
                positions[constraintId] = position + 1

                if referencedColumn is None:
                    if referencedTable not in primaryKeys:
                        primaryKeys[referencedTable] = self.primaryKey(cursor, '{}.{}'.format(
                            quoteIdentifier(self.databaseType, attached), quoteIdentifier(self.databaseType, referencedTable)))
                    referencedColumn = primaryKeys[referencedTable][position]

                rows.append((attached, table, column, attached, referencedTable, referencedColumn, '{}_fk{}'.format(table, constraintId), 'main'))

        return _schemaForeignKeys(rows, attached if schema is not None else None)


    def foreignKeyCounts(self, cursor: Cursor) -> Tuple[Dict[str, int], Optional[str]]:
        """Per attached database, `temp` aside, read the only way SQLite
        offers: a table at a time.
        """

        cursor.execute('SELECT name FROM pragma_database_list WHERE name <> ?', ('temp',))
        counts = {}

        for (attached,) in cursor.fetchall():
            keys = self.foreignKeys(cursor, None if attached == 'main' else attached)
            if keys:
                counts[attached] = len(keys)

        return counts, 'main'


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
        what a swap means; see "How a swap works" in docs/concepts/how-it-works.md.
        """

        cursor.execute('PRAGMA legacy_alter_table=ON')
        try:
            super().swap(cursor, targetTable, stageTable, tempTable)
        finally:
            cursor.execute('PRAGMA legacy_alter_table=OFF')

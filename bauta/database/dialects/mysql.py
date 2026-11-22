"""MySQL, and MariaDB, which speaks its protocol."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..driver import Connection, Cursor, native
from ...configuration import ConfigurationError, DatabaseConfig, DatabaseType, MariaDBConnection, MySQLConnection
from .base import ColumnCategory, DatabaseDialect


# UNIQUE_KEY_CLASH: what the upsert guard evaluates to refuse a row: a scalar
#   subquery of two rows, which MySQL and MariaDB reject in every sql_mode. Of
#   the key column itself, so both of IF's branches are its type: `SELECT 1`
#   beside MariaDB's UUID type was refused as mixing types, failing every
#   upsert into a table keyed by one. See MySQLDialect._onDuplicateKey.
# NEW_ROW: what a MySQL upsert names the row it would insert, for its update to
#   read: `AS bauta_new`, where MySQL deprecated VALUES(column) in 8.0.20.
# MYSQL_ROW_ALIAS_VERSION: the oldest MySQL that names that row so.
UNIQUE_KEY_CLASH = '(SELECT {0} UNION ALL SELECT {0})'
NEW_ROW = 'bauta_new'
MYSQL_ROW_ALIAS_VERSION = (8, 0, 19)

class MySQLDialect(DatabaseDialect):
    """MySQL, through mysql-connector. MariaDBDialect below differs only where
    the two servers' SQL does.
    """

    databaseType = DatabaseType.MYSQL

    REFUSED_FLOATS = frozenset({'nan', 'inf'})

    _NUMBER_TYPES = {'INT', 'BIGINT'}
    _DATE_TYPES = {'DATETIME', 'TIMESTAMP', 'DATE'}
    _TEXT_TYPES = {'TEXT', 'VARCHAR', 'CHAR'}

    def _ownConnectArguments(self, settings: DatabaseConfig, password: Optional[str]) -> Dict[str, Any]:
        """The login and database, for mysql-connector. Raises TypeError for
        any other type's settings.
        """

        if not isinstance(settings, (MySQLConnection, MariaDBConnection)):
            raise TypeError("{} settings reached the dialect for MySQL and MariaDB".format(settings.type.value))

        return {'user': settings.user, 'password': password, 'host': settings.host, 'database': settings.database,
                'port': settings.port}


    def openConnection(self, settings: DatabaseConfig) -> Any:
        """A mysql-connector connection."""

        import mysql.connector

        return mysql.connector.connect(**self.connectArguments(settings))


    def prepareSession(self, connection: Connection, settings: DatabaseConfig) -> Any:
        """Refuses MySQL older than 8.0.19, whose upserts couldn't name the
        row they insert: 8.0 itself reached the end of its support in 2026.
        """

        cursor = native(connection).cursor(buffered=True)
        if self.databaseType == DatabaseType.MYSQL:
            # Asked of the server: the driver's own answer is a method its
            # newest versions deprecate and a property its oldest lack.
            cursor.execute('SELECT VERSION()')
            found = re.match(r'(\d+)\.(\d+)\.(\d+)', str(cursor.fetchone()[0]))
            version = tuple(int(part) for part in found.groups()) if found else None
            if version is not None and version < MYSQL_ROW_ALIAS_VERSION:
                raise ConfigurationError('{} runs MySQL {}, and bauta needs {} or later'.format(
                    settings.describeTarget(), '.'.join(map(str, version)), '.'.join(map(str, MYSQL_ROW_ALIAS_VERSION))))

        return cursor


    def streamingCursor(self, connection: Connection, chunkSize: int) -> Any:
        """Unbuffered, unlike connect()'s cursor. It holds the connection until
        drained: no other statement may run on it while a stream is open.
        """

        return native(connection).cursor(buffered=False)


    def discardRemaining(self, connection: Connection, cursor: Cursor) -> None:
        """mysql.connector queues unread rows on the connection, where they fail
        the next statement ("Unread result found"). consume_results() reads and
        discards them -- bounded in memory, but it transfers every unread row.
        """

        native(connection).consume_results()


    def limitStatements(self, connection: Connection, cursor: Cursor, seconds: float) -> None:
        """SELECTs only, for their whole run, the rows a streamed one sends
        included.
        """

        cursor.execute('SET SESSION max_execution_time = {:d}'.format(max(1, round(seconds * 1000))))


    def readOnlySessionStatement(self) -> Optional[str]:
        """MySQL's: `SET SESSION TRANSACTION READ ONLY`."""

        return 'SET SESSION TRANSACTION READ ONLY'


    def placeholders(self, count: int) -> List[str]:
        """mysql-connector's format style: `%s`."""

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


    def isEncrypted(self, cursor: Cursor) -> Optional[bool]:
        """MySQL's: whether the session has a TLS cipher."""

        cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
        row = cursor.fetchone()

        return None if row is None else bool(row[1])


    def foreignKeysQuery(self) -> str:
        """MySQL's, from information_schema.key_column_usage."""

        return ("SELECT table_schema, table_name, column_name, referenced_table_schema, referenced_table_name, "
                "referenced_column_name, constraint_name, DATABASE() "
                "FROM information_schema.key_column_usage "
                "WHERE table_schema = COALESCE({}, DATABASE()) AND referenced_table_name IS NOT NULL "
                "ORDER BY table_name, constraint_name, ordinal_position")


    def foreignKeyCountsQuery(self) -> str:
        """MySQL's, leaving out the server's own schemas."""

        return ("SELECT constraint_schema, count(*), DATABASE() FROM information_schema.referential_constraints "
                "WHERE constraint_schema NOT IN ('mysql', 'sys', 'performance_schema', 'information_schema') GROUP BY constraint_schema")


    def columnsQuery(self) -> str:
        """column_type rather than data_type, since only the first says
        `unsigned` -- an `INT UNSIGNED` column holds values no target's `INT`
        can. It carries the declared size too (`varchar(20)`,
        `enum('x','y')`), which portableType drops.
        """

        return ("SELECT column_name, column_type, character_maximum_length, numeric_precision, numeric_scale, is_nullable "
                "FROM information_schema.columns WHERE table_schema = COALESCE({}, DATABASE()) AND table_name = {} ORDER BY ordinal_position")


    def primaryKeyQuery(self) -> str:
        """MySQL's: the columns of the constraint named PRIMARY."""

        return ("SELECT column_name FROM information_schema.key_column_usage "
                "WHERE table_schema = COALESCE({}, DATABASE()) AND table_name = {} AND constraint_name = 'PRIMARY' ORDER BY ordinal_position")


    def checkConstraintsQuery(self) -> Optional[str]:
        """MySQL 8.0.16 and MariaDB both; older servers never enforced a CHECK."""

        return ("SELECT cc.check_clause FROM information_schema.check_constraints cc "
                "JOIN information_schema.table_constraints tc ON tc.constraint_schema = cc.constraint_schema "
                "AND tc.constraint_name = cc.constraint_name "
                "WHERE tc.constraint_type = 'CHECK' AND tc.table_schema = COALESCE({}, DATABASE()) AND tc.table_name = {} "
                "ORDER BY cc.constraint_name")


    def plainIndexesQuery(self) -> Optional[str]:
        """MySQL's: the indexes that are neither unique nor the primary key."""

        return self._indexesQuery(unique=False)


    def tableGrantsQuery(self) -> Optional[str]:
        """Grants on the table itself; a grant on its database, the usual
        kind, covers the stage table already.
        """

        return ("SELECT privilege_type, grantee, is_grantable FROM information_schema.table_privileges "
                "WHERE table_schema = COALESCE({}, DATABASE()) AND table_name = {} ORDER BY grantee, privilege_type")


    def granteeName(self, grantee: str) -> str:
        """The catalog's own spelling, `'user'@'host'`, which GRANT takes."""

        return grantee


    def uniqueKeysQuery(self) -> str:
        """A column indexed by a prefix only -- which a TEXT column's index
        must be -- comes back NULL, as a functional index's already does: a
        column list alone can't recreate either.
        """

        return self._indexesQuery(unique=True)


    @staticmethod
    def _indexesQuery(unique: bool) -> str:
        """One table's unique or plain B-tree indexes, the primary key aside,
        as rows of (name, column)."""

        return ("SELECT index_name, CASE WHEN sub_part IS NULL THEN column_name END FROM information_schema.statistics "
                "WHERE table_schema = COALESCE({{}}, DATABASE()) AND table_name = {{}} AND non_unique = {} AND index_name <> 'PRIMARY' {}"
                "ORDER BY index_name, seq_in_index").format(0 if unique else 1, '' if unique else "AND index_type = 'BTREE' ")


    def tableExistsQuery(self) -> str:
        """MySQL's, from information_schema."""

        return "SELECT count(*) FROM information_schema.tables WHERE table_schema = COALESCE({}, DATABASE()) AND table_name = {}"


    def listTablesQuery(self) -> str:
        """MySQL's, from information_schema."""

        return ("SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = COALESCE({}, DATABASE()) AND table_type = 'BASE TABLE' ORDER BY table_name")


    def _inserted(self, column: str) -> str:
        """The value of `column` in the row an upsert would insert, as its
        ON DUPLICATE KEY UPDATE reads it.
        """

        return '{}.{}'.format(NEW_ROW, column)


    def _onDuplicateKey(self, table: str, primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:
        """The update for a row that matched one already there, guarded first.

        ON DUPLICATE KEY fires on any unique key, not only the primary key: a
        new row whose email another row already had updated that other row
        with the new one's values, and the new row was never written -- two
        people merged into one without an error, where every other database
        refuses the row. The guard keeps the matched row's key where it is
        the incoming row's, and otherwise asks for a subquery returning two
        rows, which fails the statement in every sql_mode (1242, which
        isUniqueKeyClash recognises). MySQL evaluates the subquery only when
        the keys differ, and assignments left to right, so nothing is
        updated before the guard. It also stands in for the no-op assignment
        a key-only table needs, since an empty SET is invalid. Not INSERT
        IGNORE, which also silences truncation, NOT NULL and foreign-key errors.

        The matched row's columns are qualified with the table, since a load
        from a stage table names the same columns in its SELECT.
        """

        same = ' AND '.join('{0}.{1} <=> {2}'.format(table, column, self._inserted(column)) for column in primaryKeyColumns)
        keyColumn = '{}.{}'.format(table, primaryKeyColumns[0])
        guard = '{0} = IF({1}, {0}, {2})'.format(keyColumn, same, UNIQUE_KEY_CLASH.format(keyColumn))

        return 'ON DUPLICATE KEY UPDATE {}'.format(', '.join([guard] + ['{}={}'.format(column, self._inserted(column)) for column in nonPrimaryKeyColumns]))


    def isUniqueKeyClash(self, error: BaseException) -> bool:
        """Whether `error` is _onDuplicateKey's guard refusing a row."""

        return getattr(error, 'errno', None) == 1242 and 'more than 1 row' in str(error)


    def upsertQuery(self, table: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:
        """MySQL's: INSERT ... AS NEW_ROW ON DUPLICATE KEY UPDATE, guarded
        against a clash on another unique key.
        """

        return 'INSERT INTO {} ({}) VALUES ({}) AS {} {}'.format(table, ', '.join(allColumns), ', '.join(self.placeholders(len(allColumns))), NEW_ROW,
                                                                self._onDuplicateKey(table, primaryKeyColumns, nonPrimaryKeyColumns))


    def upsertFromStageQuery(self, targetTable: str, stageTable: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:
        """The stage's rows through a derived table, which names the row the
        update reads, as the row alias does for one insert.
        """

        return 'INSERT INTO {} ({}) SELECT * FROM (SELECT {} FROM {}) AS {} {}'.format(
            targetTable, ', '.join(allColumns), ', '.join(allColumns), stageTable, NEW_ROW,
            self._onDuplicateKey(targetTable, primaryKeyColumns, nonPrimaryKeyColumns))


    def swapQueries(self, targetTable: str, stageTable: str, tempTable: str) -> List[str]:
        """One atomic statement; RENAME TABLE takes qualified names on both sides."""

        return ['RENAME TABLE {} TO {}, {} TO {}, {} TO {}'.format(stageTable, tempTable, targetTable, stageTable, tempTable, targetTable)]


class MariaDBDialect(MySQLDialect):
    """MySQL's dialect and driver: MariaDB is compatible with everything this
    uses but the statement timeout, which it names otherwise, and an upsert's
    inserted row, which it reads with VALUES(column) -- not deprecated there,
    and the only way it has.
    """

    databaseType = DatabaseType.MARIADB


    def _inserted(self, column: str) -> str:
        """MariaDB's name for a column of the row an upsert would insert:
        VALUES(column), since it has no row alias.
        """

        return 'VALUES({})'.format(column)


    def upsertQuery(self, table: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:
        """MariaDB's: as MySQL's, without the row alias."""

        return 'INSERT INTO {} ({}) VALUES ({}) {}'.format(table, ', '.join(allColumns), ', '.join(self.placeholders(len(allColumns))),
                                                          self._onDuplicateKey(table, primaryKeyColumns, nonPrimaryKeyColumns))


    def upsertFromStageQuery(self, targetTable: str, stageTable: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:
        """MariaDB's: INSERT ... SELECT from the stage with ON DUPLICATE KEY UPDATE."""

        return 'INSERT INTO {} ({}) SELECT {} FROM {} {}'.format(targetTable, ', '.join(allColumns), ', '.join(allColumns), stageTable,
                                                                self._onDuplicateKey(targetTable, primaryKeyColumns, nonPrimaryKeyColumns))


    def limitStatements(self, connection: Connection, cursor: Cursor, seconds: float) -> None:
        """MariaDB's own variable, in seconds; it has no max_execution_time.
        Every statement, a streamed SELECT for its whole run.
        """

        cursor.execute('SET SESSION max_statement_time = {:g}'.format(seconds))


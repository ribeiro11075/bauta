"""MySQL, and MariaDB, which speaks its protocol."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...configuration import DatabaseConnectionConfig, DatabaseType
from .base import ColumnCategory, DatabaseDialect



class MySQLDialect(DatabaseDialect):

    databaseType = DatabaseType.MYSQL

    _NUMBER_TYPES = {'INT', 'BIGINT'}
    _DATE_TYPES = {'DATETIME', 'TIMESTAMP', 'DATE'}
    _TEXT_TYPES = {'TEXT', 'VARCHAR', 'CHAR'}

    def _ownConnectArguments(self, settings: DatabaseConnectionConfig, password: Optional[str]) -> Dict[str, Any]:

        return {'user': settings.user, 'password': password, 'host': settings.host, 'database': settings.database,
                'port': settings.port}


    def openConnection(self, settings: DatabaseConnectionConfig) -> Any:

        import mysql.connector

        return mysql.connector.connect(**self.connectArguments(settings))


    def prepareSession(self, connection: Any, settings: DatabaseConnectionConfig) -> Any:

        return connection.cursor(buffered=True)


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


    def listTablesQuery(self) -> str:

        return ("SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = COALESCE({}, DATABASE()) AND table_type = 'BASE TABLE' ORDER BY table_name")


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


class MariaDBDialect(MySQLDialect):
    """MySQL's dialect and driver, unchanged: MariaDB is compatible with
    everything this uses.
    """

    databaseType = DatabaseType.MARIADB

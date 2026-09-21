"""Oracle, in python-oracledb's thin mode."""
from __future__ import annotations

import datetime
import decimal
import logging
from typing import Any, Dict, List, Optional, Tuple

from ...configuration import DatabaseConnectionConfig, DatabaseType
from ...log import LOGGER_NAME
from .base import ColumnCategory, DatabaseDialect, _mergeUpdateInsertClause, _renameInThreeSteps, _renameSteps, _renameStatement

logger = logging.getLogger(LOGGER_NAME)



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

    def openConnection(self, settings: DatabaseConnectionConfig) -> Any:

        import oracledb

        return oracledb.connect(**self.connectArguments(settings))


    def prepareSession(self, connection: Any, settings: DatabaseConnectionConfig) -> Any:

        connection.outputtypehandler = _oracleValues
        connection.inputtypehandler = _oracleDatetimesAsTimestamps
        cursor = connection.cursor()
        cursor.execute(self.SESSION_FORMATS)

        if settings.currentSchema:
            cursor.execute('ALTER SESSION SET CURRENT_SCHEMA = {}'.format(settings.currentSchema))

        return cursor


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


    def listTablesQuery(self) -> str:
        """all_tables, not dba_tables, which needs a privilege a copy job has no
        other use for; all_tables is what this login may already read.

        A nested table, an index-organized table's overflow segment and a
        secondary table belong to another table rather than standing on their
        own, and a BIN$ name is a dropped table still in the recycle bin.
        """

        return ("SELECT table_name FROM all_tables WHERE owner = " + self.OWNER + " "
                "AND nested = 'NO' AND secondary = 'N' AND (iot_type IS NULL OR iot_type = 'IOT') "
                "AND table_name NOT LIKE 'BIN$%' ORDER BY table_name")


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

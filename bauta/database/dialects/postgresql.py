"""PostgreSQL, loaded through COPY."""
from __future__ import annotations

import datetime
import decimal
import hashlib
import weakref
import uuid
from typing import Any, Dict, List, Optional, Sequence, Set

from ..driver import Connection, Cursor, native
from ...configuration import DatabaseConfig, DatabaseType, PostgreSQLConnection
from .base import ColumnCategory, settingsOf, _OnConflictDialect, _holdsOnly
from .names import unqualifiedName


# The types COPY is trusted with: psycopg's text dumpers spell each exactly as
# PostgreSQL parses it back, whatever the column. datetime.datetime is a date.
# Anything else -- a list, which may be an array or JSON, a Jsonb, a duration --
# sends the chunk through the driver's own adapters, a statement a row.
_COPYABLE = (type(None), bool, int, float, decimal.Decimal, str, datetime.date, datetime.time, uuid.UUID, bytes, bytearray, memoryview)


def _copyIn(cursor: Cursor, statement: str, rows: Sequence[Sequence[Any]]) -> bool:
    """COPY `rows` in with psycopg's own encoders, which encode in C, or return
    False without sending anything if a value isn't of a type COPY is trusted
    with.
    """

    if not _holdsOnly(rows, _COPYABLE):
        return False

    with native(cursor).copy(statement) as copy:
        for row in rows:
            copy.write_row(row)

    return True


class PostgreSQLDialect(_OnConflictDialect):

    databaseType = DatabaseType.POSTGRESQL

    _NUMBER_OIDS = {20, 21, 23}
    _DATE_OIDS = {1114, 1018}
    _TEXT_OIDS = {1043, 18, 25}

    def openConnection(self, settings: DatabaseConfig) -> Any:

        import psycopg
        from psycopg.types.string import TextLoader

        # ClientCursor writes parameters into the statement, as the other
        # drivers do, rather than binding them server-side: a server-side
        # parameter takes one fixed type, which a value can't always fit.
        connection = psycopg.connect(**self.connectArguments(settings), cursor_factory=psycopg.ClientCursor)

        # json and jsonb as their text, as MySQL, SQLite and DuckDB return
        # them, rather than parsed into Python. Parsed, a document lost what
        # it was: the string "123" was loaded back as the number 123, a
        # number went to a jsonb column as an integer and failed the job,
        # and every number with a fraction passed through a float, so
        # 12345678901234567890.123 arrived as 12345678901234567000. Arrays of
        # either hold their elements as text the same way.
        for name in ('json', 'jsonb'):
            connection.adapters.register_loader(name, TextLoader)

        return connection


    def prepareSession(self, connection: Connection, settings: DatabaseConfig) -> Any:

        cursor = connection.cursor()

        # Only this schema, with no fallback such as `public` that catalog
        # lookups wouldn't see. Committed, or a later rollback would undo it.
        currentSchema = settingsOf(settings, PostgreSQLConnection).currentSchema
        if currentSchema:
            cursor.execute('SET search_path TO {}'.format(currentSchema))
            connection.commit()

        return cursor


    def _ownConnectArguments(self, settings: DatabaseConfig, password: Optional[str]) -> Dict[str, Any]:

        settings = settingsOf(settings, PostgreSQLConnection)

        return {'user': settings.user, 'password': password, 'host': settings.host, 'dbname': settings.database,
                'port': settings.port}


    def streamingCursor(self, connection: Connection, chunkSize: int) -> Any:
        """A named, server-side cursor: psycopg buffers everything through an
        unnamed one. A commit on the connection invalidates it, so the extract
        side never commits.
        """

        cursor = native(connection).cursor(name='bauta_{}'.format(uuid.uuid4().hex))
        cursor.itersize = chunkSize

        return cursor


    def limitStatements(self, connection: Connection, cursor: Cursor, seconds: float) -> None:
        """Each statement, which for a streamed query is each FETCH: a long
        extract is not stopped for its length, but one fetch whose sort or
        join runs away is.
        """

        cursor.execute('SET statement_timeout = {:d}'.format(max(1, round(seconds * 1000))))


    def readOnlySessionStatement(self) -> Optional[str]:

        return 'SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY'


    def placeholders(self, count: int) -> List[str]:

        return count * ['%s']


    def supportsMaterializedSelections(self) -> bool:
        """PostgreSQL 12 and later. Without it, PostgreSQL copies a selection
        used once into each query that uses it, which makes planning a subset
        of many tables slow.
        """

        return True


    def isEncrypted(self, cursor: Cursor) -> Optional[bool]:

        cursor.execute('SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()')
        row = cursor.fetchone()

        return None if row is None else bool(row[0])


    def bulkInsert(self, cursor: Cursor, table: str, columns: List[str], rows: Sequence[Sequence[Any]]) -> bool:
        """COPY FROM STDIN: one round trip per chunk, where executemany sends
        one statement per row.
        """

        return _copyIn(cursor, 'COPY {} ({}) FROM STDIN'.format(table, ', '.join(columns)), rows)


    def __init__(self) -> None:
        # The upsert staging tables each connection has created. One dialect
        # serves every connection, so this is keyed by connection, weakly, so
        # a closed one is forgotten with it.
        self._stagingTables: 'weakref.WeakKeyDictionary[Any, Set[str]]' = weakref.WeakKeyDictionary()


    def bulkUpsert(self, cursor: Cursor, table: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str],
                   rows: Sequence[Sequence[Any]]) -> bool:
        """COPY into a temporary table, then one INSERT ... ON CONFLICT from it.

        The table lives as long as the connection and empties at every commit,
        so the first chunk creates it and the rest reuse it, sparing a round
        trip per chunk.
        """

        if not _holdsOnly(rows, _COPYABLE):
            return False

        columns = ', '.join(allColumns)
        staging = 'bauta_upsert_{}'.format(hashlib.sha1('{}|{}'.format(table, columns).encode('utf-8')).hexdigest()[:12])
        created = self._stagingTables.setdefault(native(cursor).connection, set())
        creating = staging not in created

        try:
            if creating:
                cursor.execute('CREATE TEMPORARY TABLE IF NOT EXISTS {} ON COMMIT DELETE ROWS AS SELECT {} FROM {} WITH NO DATA'.format(
                    staging, columns, table))
                created.add(staging)
            _copyIn(cursor, 'COPY {} ({}) FROM STDIN'.format(staging, columns), rows)
            cursor.execute(self.upsertFromStageQuery(table, staging, allColumns, primaryKeyColumns, nonPrimaryKeyColumns))
        except BaseException:
            # The rollback that follows undoes a CREATE made in this chunk.
            if creating:
                created.discard(staging)
            raise

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

        return ("SELECT ns.nspname, cl.relname, att.attname, rns.nspname, rcl.relname, ratt.attname, con.conname, current_schema() "
                "FROM pg_constraint con "
                "JOIN pg_class cl ON cl.oid = con.conrelid "
                "JOIN pg_namespace ns ON ns.oid = cl.relnamespace "
                "JOIN pg_class rcl ON rcl.oid = con.confrelid "
                "JOIN pg_namespace rns ON rns.oid = rcl.relnamespace "
                "CROSS JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS k(attnum, refattnum, position) "
                "JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum "
                "JOIN pg_attribute ratt ON ratt.attrelid = con.confrelid AND ratt.attnum = k.refattnum "
                "WHERE con.contype = 'f' AND ns.nspname = COALESCE({}::text, current_schema()) "
                "ORDER BY cl.relname, con.conname, k.position")


    def foreignKeyCountsQuery(self) -> str:

        return ("SELECT ns.nspname, count(*), current_schema() FROM pg_constraint con "
                "JOIN pg_class cl ON cl.oid = con.conrelid JOIN pg_namespace ns ON ns.oid = cl.relnamespace "
                "WHERE con.contype = 'f' AND ns.nspname NOT IN ('pg_catalog', 'information_schema') GROUP BY ns.nspname")


    # The bound names arrive folded, so the lookups compare them as they are;
    # a name quoted in a job keeps the case it was quoted with. ::text gives a
    # NULL schema a type COALESCE can use.

    def columnsQuery(self) -> str:

        # An array as its element type and size, `character varying(20)[]`,
        # which information_schema reports only as ARRAY.
        return ("SELECT column_name, CASE WHEN data_type = 'ARRAY' THEN (SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                "WHERE a.attrelid = (quote_ident(table_schema) || '.' || quote_ident(table_name))::regclass AND a.attname = column_name) "
                "ELSE data_type END, character_maximum_length, numeric_precision, numeric_scale, is_nullable "
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


    def checkConstraintsQuery(self) -> Optional[str]:

        return ("SELECT pg_get_constraintdef(con.oid) FROM pg_constraint con JOIN pg_class cl ON cl.oid = con.conrelid "
                "JOIN pg_namespace ns ON ns.oid = cl.relnamespace "
                "WHERE con.contype = 'c' AND ns.nspname = COALESCE({}::text, current_schema()) AND cl.relname = {}::text ORDER BY con.conname")


    def plainIndexesQuery(self) -> Optional[str]:
        """B-tree indexes only, which a column list recreates: a GIN or GiST
        index needs its operator class, which this doesn't read.
        """

        return self._indexesQuery(unique=False)


    def tableGrantsQuery(self) -> Optional[str]:
        """From the table's access list, which holds every grant on it, where
        information_schema shows only those the current role is party to.
        """

        return ("SELECT acl.privilege_type, CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END, acl.is_grantable "
                "FROM pg_class cl JOIN pg_namespace ns ON ns.oid = cl.relnamespace "
                "CROSS JOIN LATERAL aclexplode(cl.relacl) AS acl "
                "WHERE acl.grantee <> cl.relowner AND ns.nspname = COALESCE({}::text, current_schema()) AND cl.relname = {}::text "
                "ORDER BY 2, 1")


    def uniqueKeysQuery(self) -> str:
        """From pg_index, which holds a unique constraint's index and a plain
        unique index alike. Only an index's key columns, not those it merely
        INCLUDEs; an index on an expression or with a WHERE is left out.
        """

        return self._indexesQuery(unique=True)


    @staticmethod
    def _indexesQuery(unique: bool) -> str:
        """One table's unique or plain indexes, the primary key's aside, as
        rows of (name, column); see uniqueKeysQuery and plainIndexesQuery.
        """

        return ("SELECT ic.relname, att.attname FROM pg_index idx "
                "JOIN pg_class cl ON cl.oid = idx.indrelid "
                "JOIN pg_namespace ns ON ns.oid = cl.relnamespace "
                "JOIN pg_class ic ON ic.oid = idx.indexrelid "
                "JOIN pg_am am ON am.oid = ic.relam "
                "CROSS JOIN LATERAL unnest(idx.indkey) WITH ORDINALITY AS k(attnum, position) "
                "LEFT JOIN pg_attribute att ON att.attrelid = cl.oid AND att.attnum = k.attnum AND k.attnum > 0 "
                "WHERE {}idx.indisunique AND NOT idx.indisprimary AND idx.indexprs IS NULL AND idx.indpred IS NULL {}"
                "AND k.position <= idx.indnkeyatts "
                "AND ns.nspname = COALESCE({{}}::text, current_schema()) AND cl.relname = {{}}::text "
                "ORDER BY ic.relname, k.position").format('' if unique else 'NOT ', '' if unique else "AND am.amname = 'btree' ")


    def tableExistsQuery(self) -> str:

        return ("SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = COALESCE({}::text, current_schema()) AND table_name = {}::text")


    def listTablesQuery(self) -> str:
        """information_schema shows only what this login may read, which is the
        right set for a copy: a table it cannot select from is not one it can
        copy either.
        """

        return ("SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = COALESCE({}::text, current_schema()) AND table_type = 'BASE TABLE' ORDER BY table_name")


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

    def swap(self, cursor: Cursor, targetTable: str, stageTable: str, tempTable: str) -> None:
        """Renames, then recreates each view on the target from its definition
        captured beforehand, since a PostgreSQL view follows the table, not the
        name. CREATE OR REPLACE keeps grants and views built on it. See "How a
        swap works" in docs/concepts/how-it-works.md.
        """

        cursor.execute(self.DEPENDENT_VIEWS_QUERY, (targetTable,))
        views = cursor.fetchall()

        super().swap(cursor, targetTable, stageTable, tempTable)

        for name, definition in views:
            cursor.execute('CREATE OR REPLACE VIEW {} AS {}'.format(name, definition))

"""What every dialect shares: the DatabaseDialect contract, the catalog
records it returns, and the helpers a load uses on each chunk.
"""
from __future__ import annotations

import contextlib
import datetime
import itertools
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from ...configuration import ConfigurationError, DatabaseConnectionConfig, DatabaseType
from .names import catalogName, catalogTableName, unqualifiedName



class ForeignKey(NamedTuple):
    """One foreign-key constraint. Composite keys list their columns in order."""

    table: str
    columns: Tuple[str, ...]
    referencedTable: str
    referencedColumns: Tuple[str, ...]
    name: str


def _groupForeignKeys(rows: Sequence[Sequence[Any]]) -> List[ForeignKey]:
    """Folds (table, column, referencedTable, referencedColumn, constraint) rows,
    already ordered by position within each constraint, into ForeignKeys.
    """

    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for table, column, referencedTable, referencedColumn, name in rows:
        entry = grouped.setdefault((table, name), {'referencedTable': referencedTable, 'columns': [], 'referencedColumns': []})
        entry['columns'].append(column)
        entry['referencedColumns'].append(referencedColumn)

    return [
        ForeignKey(table=table, columns=tuple(entry['columns']), referencedTable=entry['referencedTable'],
                   referencedColumns=tuple(entry['referencedColumns']), name=name)
        for (table, name), entry in grouped.items()
        ]


class ColumnDefinition(NamedTuple):
    """One column as the database's catalog describes it, for generating DDL.

    `dataType` is the catalog's own type name (`character varying`, `VARCHAR2`,
    `nvarchar`, ...); schema.py maps it to a portable type. `length` is in
    characters, and is None for unbounded text or where it doesn't apply.
    """

    name: str
    dataType: str
    length: Optional[int]
    precision: Optional[int]
    scale: Optional[int]
    nullable: bool


def _columnDefinitions(rows: Sequence[Sequence[Any]]) -> List[ColumnDefinition]:
    """Rows of (name, type, length, precision, scale, nullable) -> ColumnDefinitions.

    Catalogs disagree on how they say "nullable" (YES, Y, 1) and sometimes
    return numbers as Decimal, so both are normalized here.
    """

    def number(value: Any) -> Optional[int]:
        return None if value is None else int(value)

    return [
        ColumnDefinition(name=name, dataType=str(dataType), length=number(length), precision=number(precision), scale=number(scale),
                         nullable=str(nullable).upper() in ('YES', 'Y', '1', 'TRUE'))
        for name, dataType, length, precision, scale, nullable in rows
        ]


class ColumnCategory(str, Enum):
    NUMBER = 'number'
    DATE = 'date'
    TEXT = 'text'


def _mergeUpdateInsertClause(targetAlias: str, sourceAlias: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:
    """The ON/WHEN MATCHED/WHEN NOT MATCHED tail of an Oracle or SQL Server
    MERGE. A key-only table gets no WHEN MATCHED, since an empty SET is invalid.
    """

    onClause = ' AND '.join('{}.{} = {}.{}'.format(targetAlias, column, sourceAlias, column) for column in primaryKeyColumns)
    insertColumns = ', '.join(allColumns)
    insertValues = ', '.join('{}.{}'.format(sourceAlias, column) for column in allColumns)

    whenMatched = ''
    if nonPrimaryKeyColumns:
        updateClause = ', '.join('{}.{} = {}.{}'.format(targetAlias, column, sourceAlias, column) for column in nonPrimaryKeyColumns)
        whenMatched = 'WHEN MATCHED THEN UPDATE SET {} '.format(updateClause)

    return 'ON ({}) {}WHEN NOT MATCHED THEN INSERT ({}) VALUES ({})'.format(onClause, whenMatched, insertColumns, insertValues)


def durationText(value: datetime.timedelta) -> str:
    """A duration as `[-]HH:MM:SS[.ffffff]`, the way MySQL writes a TIME.

    MySQL's TIME is a duration, from -838:59:59 to 838:59:59, and its driver
    returns a timedelta. Nothing else takes one: SQL Server's and SQLite's
    drivers refuse it outright, psycopg writes it as an interval -- which
    PostgreSQL then squeezed into a TIME column as a wrong time of day, without
    a word -- and Oracle stored Python's own `-35 days, 1:00:01`. Every
    database parses this spelling back into whatever the column is.
    """

    sign = '-' if value < datetime.timedelta(0) else ''
    magnitude = abs(value)
    hours, rest = divmod(int(magnitude.total_seconds()), 3600)
    minutes, seconds = divmod(rest, 60)
    fraction = '.{:06d}'.format(magnitude.microseconds) if magnitude.microseconds else ''

    return '{}{:02d}:{:02d}:{:02d}{}'.format(sign, hours, minutes, seconds, fraction)


def _holdsAny(rows: Sequence[Sequence[Any]], kinds: Tuple[type, ...]) -> bool:
    """Whether any value in `rows` is one of `kinds`, a subclass included.

    Every chunk of every load is asked this at least once. The distinct types
    are gathered in C and only those few are checked, rather than calling
    isinstance on every value of every row.
    """

    return any(issubclass(kind, kinds) for kind in set(map(type, itertools.chain.from_iterable(rows))))


def _holdsOnly(rows: Sequence[Sequence[Any]], kinds: Tuple[type, ...]) -> bool:
    """Whether every value in `rows` is one of `kinds`; see _holdsAny."""

    return all(issubclass(kind, kinds) for kind in set(map(type, itertools.chain.from_iterable(rows))))


def _renameSteps(targetTable: str, stageTable: str, tempTable: str) -> List[Tuple[str, str]]:
    """The three renames a swap is, as (from, to) pairs: the stage out of the
    way, the target into its place, and the stage into the target's name.
    """

    return [(stageTable, tempTable), (targetTable, stageTable), (tempTable, targetTable)]


def _renameStatement(fromTable: str, toTable: str) -> str:
    """RENAME TO takes the new name unqualified; the table stays in its schema."""

    return 'ALTER TABLE {} RENAME TO {}'.format(fromTable, unqualifiedName(toTable))


def _renameInThreeSteps(targetTable: str, stageTable: str, tempTable: str) -> List[str]:

    return [_renameStatement(*step) for step in _renameSteps(targetTable, stageTable, tempTable)]


class DatabaseDialect(ABC):
    """Everything that differs between database types lives here, not in Database.

    `databaseType` lets a dialect read a name the way its own database writes
    one, for the catalog lookups and the statements it builds.
    """

    databaseType: DatabaseType

    def connect(self, settings: DatabaseConnectionConfig) -> Tuple[Any, Any]:
        """Returns (connection, cursor), the session prepared. A connection
        whose preparation fails is closed before the error goes on, or every
        retry of a misconfigured job would leave one open.
        """

        connection = self.openConnection(settings)

        try:
            cursor = self.prepareSession(connection, settings)
        except BaseException:
            with contextlib.suppress(Exception):
                connection.close()
            raise

        return connection, cursor

    @abstractmethod
    def openConnection(self, settings: DatabaseConnectionConfig) -> Any:
        """A new connection. Drivers are imported here, so only the one in use
        needs installing.
        """

    def prepareSession(self, connection: Any, settings: DatabaseConnectionConfig) -> Any:
        """Sets the session up the way every statement expects, returning the
        cursor to run them on.
        """

        return connection.cursor()

    @abstractmethod
    def _ownConnectArguments(self, settings: DatabaseConnectionConfig, password: Optional[str]) -> Dict[str, Any]:
        """The driver keyword arguments the connection fields map to."""

    def connectArguments(self, settings: DatabaseConnectionConfig, resolvePassword: bool = True) -> Dict[str, Any]:
        """The fields' driver arguments plus settings.options, refusing an
        option that duplicates a field. resolvePassword=False lets `validate`
        check this without running a passwordCommand.
        """

        own = self._ownConnectArguments(settings, settings.plainPassword() if resolvePassword else None)
        clashes = sorted(set(own) & set(settings.options))

        if clashes:
            raise ConfigurationError('options {} duplicate what the connection fields already set for {}; use the fields instead'.format(
                ', '.join(clashes), settings.type.value))

        return {**own, **settings.options}

    def streamingCursor(self, connection: Any, chunkSize: int) -> Any:
        """A cursor that doesn't buffer the whole result set client-side, which
        fetchmany() alone doesn't prevent. A plain cursor already streams on
        sqlite3 and pymssql.
        """

        return connection.cursor()


    def prepareValues(self, rows: List[Tuple[Any, ...]]) -> List[Tuple[Any, ...]]:
        """A batch as this driver must receive it. Every dialect writes a
        duration as text; PostgreSQL and SQL Server have more to do.
        """

        if not _holdsAny(rows, (datetime.timedelta,)):
            return rows

        return [tuple(durationText(value) if isinstance(value, datetime.timedelta) else value for value in row) for row in rows]


    def discardRemaining(self, connection: Any, cursor: Any) -> None:
        """Release rows left unread by an abandoned stream, so `connection` stays
        usable. Closing the cursor is enough everywhere but MySQL.
        """


    @abstractmethod
    def placeholders(self, count: int) -> List[str]:
        """Parameter placeholder markers, one per bound value, in this dialect's paramstyle."""

    def supportsMaterializedSelections(self) -> bool:
        """Whether `WITH name AS MATERIALIZED (...)` is accepted, and worth
        using: planSubset's queries then compute each selection once.
        """

        return False

    def isEncrypted(self, cursor: Any) -> Optional[bool]:
        """Whether the server reports this connection as encrypted in transit;
        None where there's no network or no way to tell.
        """

        return None

    def bulkInsert(self, cursor: Any, table: str, columns: List[str], rows: Sequence[Sequence[Any]]) -> bool:
        """Loads `rows` in fewer round trips than executemany, for drivers whose
        executemany sends a statement per row. False means nothing was sent.
        """

        return False

    def bulkUpsert(self, cursor: Any, table: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str],
                   rows: Sequence[Sequence[Any]]) -> bool:
        """bulkInsert for an upsert: the same contract. `rows` hold no two rows
        with the same key.
        """

        return False

    def truncateQuery(self, table: str) -> str:
        """TRUNCATE TABLE, which every dialect but SQLite has."""

        return 'TRUNCATE TABLE {}'.format(table)

    def columnCategory(self, dataType: Any) -> Optional[ColumnCategory]:
        """The category of a driver-specific cursor.description type_code, for
        discovery. None means unrecognized, and discovery samples values instead.
        """

        return None

    # The three catalog queries below each bind two parameters, the schema and
    # the table, from catalogTableName: unquoted, and folded as this database
    # folds a name written without quotes. A NULL schema means the current one.

    def primaryKeyQuery(self) -> str:
        """One table's declared primary-key columns, in key order. Not UNIQUE
        constraints, which would make an upsert treat a changed row as new.
        """

        raise NotImplementedError('{} cannot describe primary keys'.format(type(self).__name__))

    def columnsQuery(self) -> str:
        """One table's columns, in order, as rows of (name, type, length,
        precision, scale, nullable).
        """

        raise NotImplementedError('{} cannot describe columns'.format(type(self).__name__))

    def tableExistsQuery(self) -> str:
        """A count of tables with the bound name in the bound schema."""

        raise NotImplementedError('{} cannot check for tables'.format(type(self).__name__))

    def _catalog(self, cursor: Any, query: str, table: str) -> List[Any]:

        cursor.execute(query.format(*self.placeholders(2)), catalogTableName(self.databaseType, table))

        return cursor.fetchall()

    def primaryKey(self, cursor: Any, table: str) -> List[str]:

        return [row[0] for row in self._catalog(cursor, self.primaryKeyQuery(), table)]

    def columnDefinitions(self, cursor: Any, table: str) -> List[ColumnDefinition]:

        return _columnDefinitions(self._catalog(cursor, self.columnsQuery(), table))

    def tableExists(self, cursor: Any, table: str) -> bool:

        return bool(self._catalog(cursor, self.tableExistsQuery(), table)[0][0])

    def listTablesQuery(self) -> str:
        """Every base table of the bound schema, or of the connection's own
        where the bound value is NULL, as rows of (name), ordered by name.

        Base tables only: a view is derived from them, and a system or catalog
        table is the server's own, so neither is something a job would copy.
        """

        raise NotImplementedError('{} cannot list tables'.format(type(self).__name__))

    def listTables(self, cursor: Any, schema: Optional[str] = None) -> List[str]:
        """The schema's base tables, named as the catalog holds them.

        `schema` is bound the way every other catalog lookup binds one --
        unquoted, and folded as this database folds a name written without
        quotes -- so it means the same schema a job's `schema.table` would.
        """

        cursor.execute(self.listTablesQuery().format(*self.placeholders(1)),
                       (catalogName(self.databaseType, schema) if schema is not None else None,))

        return [row[0] for row in cursor.fetchall()]

    def foreignKeysQuery(self) -> str:
        """Every foreign key in the connection's current schema, as rows of
        (table, column, referencedTable, referencedColumn, constraintName),
        ordered by table, constraint and position.
        """

        raise NotImplementedError('{} cannot list foreign keys'.format(type(self).__name__))

    def foreignKeys(self, cursor: Any) -> List[ForeignKey]:
        """For planning subsets. SQLite, which can't do it in one query,
        overrides this.
        """

        cursor.execute(self.foreignKeysQuery())

        return _groupForeignKeys(cursor.fetchall())

    @abstractmethod
    def upsertQuery(self, table: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:
        ...

    @abstractmethod
    def upsertFromStageQuery(self, targetTable: str, stageTable: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:
        ...

    @abstractmethod
    def swapQueries(self, targetTable: str, stageTable: str, tempTable: str) -> List[str]:
        """One or more statements to execute in order, then commit once.

        `tempTable` is in the stage table's schema, and `stageTable` must share
        the target's (configuration checks that), since a rename never moves a
        table between schemas. Renames take the new name unqualified.
        """

    def swap(self, cursor: Any, targetTable: str, stageTable: str, tempTable: str) -> None:
        """Runs the swap on `cursor`; the caller commits. A dialect with more to
        do around the renames overrides this.
        """

        for query in self.swapQueries(targetTable=targetTable, stageTable=stageTable, tempTable=tempTable):
            cursor.execute(query)


class _OnConflictDialect(DatabaseDialect):
    """PostgreSQL and SQLite share `INSERT ... ON CONFLICT` word for word. A
    key-only table gets DO NOTHING, since an empty SET is invalid.

    The stage form's `WHERE true` is SQLite's documented workaround for reading
    ON as the start of a join constraint.
    """

    @staticmethod
    def _onConflict(primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:

        if not nonPrimaryKeyColumns:
            return 'ON CONFLICT({}) DO NOTHING'.format(','.join(primaryKeyColumns))

        return 'ON CONFLICT({}) DO UPDATE SET {}'.format(
            ','.join(primaryKeyColumns), ', '.join('{0}=excluded.{0}'.format(column) for column in nonPrimaryKeyColumns))

    def upsertQuery(self, table: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:

        return 'INSERT INTO {} ({}) VALUES ({}) {}'.format(table, ', '.join(allColumns), ', '.join(self.placeholders(len(allColumns))),
                                                          self._onConflict(primaryKeyColumns, nonPrimaryKeyColumns))

    def upsertFromStageQuery(self, targetTable: str, stageTable: str, allColumns: List[str], primaryKeyColumns: List[str], nonPrimaryKeyColumns: List[str]) -> str:

        return 'INSERT INTO {} ({}) SELECT {} FROM {} WHERE true {}'.format(targetTable, ', '.join(allColumns), ', '.join(allColumns), stageTable,
                                                                          self._onConflict(primaryKeyColumns, nonPrimaryKeyColumns))

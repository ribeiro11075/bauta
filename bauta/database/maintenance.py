"""What a swap does to the tables beyond loading them, as Database methods:
the swap itself, putting right one stopped part-way on a database whose
renames commit alone, and giving the stage table the target's keys, plain
indexes and grants first, so the target keeps them whichever table holds its
name. See "How a swap works" in docs/concepts/how-it-works.md.
"""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from ..configuration import ConfigurationError
from ..log.scrubbing import describeError
from .dialects import DatabaseDialect, quoteIdentifier, splitTableName, suffixedName
from .driver import Connection, Cursor


def _stageIndexName(name: str) -> str:
    """The name a stage table's copy of index `name` takes: the original's
    with `_s`, since PostgreSQL and Oracle name indexes per schema and the
    original keeps its own. Shortened, with a digest of the whole, past the
    30 characters every database here allows.
    """

    stageName = '{}_s'.format(name)
    if len(stageName) <= 30:
        return stageName

    return '{}_{}'.format(name[:21], hashlib.sha256(name.encode('utf-8')).hexdigest()[:8])


class TableMaintenance:
    """Database's swap and stage-table operations, kept apart from reading
    and loading; Database inherits them.
    """

    if TYPE_CHECKING:
        dialect: DatabaseDialect
        cursor: Cursor
        connection: Connection
        type: Any
        primaryKeyCache: Dict[str, List[str]]
        columnNameCache: Dict[str, List[str]]

        def _refuseWrite(self, what: str) -> None: ...
        def statementName(self, table: str) -> str: ...
        def catalogColumns(self, table: str, columns: Optional[Sequence[str]] = None) -> List[str]: ...
        def quoted(self, columns: Sequence[str]) -> List[str]: ...
        def tableExists(self, table: str) -> bool: ...

    def copyKeys(self, fromTable: str, toTable: str) -> List[str]:
        """Gives `toTable` the primary key and unique keys of `fromTable` that it
        lacks, and says what it gave, as `primary key (id)` or `unique (email)`.
        For a swap's stage table, which becomes the target, so the target keeps
        its keys whichever table holds its name. See "How a swap works" in
        docs/concepts/how-it-works.md.

        Nothing is taken away, and a key `toTable` already has over the same
        columns, in any order, is left as it is. Its columns are named as
        `toTable` spells them, which must hold every one. Foreign keys are not
        copied, for the reason `bauta schema` gives its stage tables none.
        """

        self._refuseWrite('add keys to {}'.format(toTable))

        def folded(columns: Sequence[str]) -> frozenset:
            return frozenset(column.upper() for column in columns)

        primaryKey = self.dialect.primaryKey(self.cursor, fromTable)
        uniqueKeys = self.dialect.uniqueKeys(self.cursor, fromTable)
        ownPrimaryKey = self.dialect.primaryKey(self.cursor, toTable)
        held = {folded(columns) for columns in self.dialect.uniqueKeys(self.cursor, toTable)}
        if ownPrimaryKey:
            held.add(folded(ownPrimaryKey))

        addPrimaryKey = primaryKey if primaryKey and not ownPrimaryKey else []
        if addPrimaryKey:
            held.add(folded(addPrimaryKey))
        addUnique = []
        for columns in uniqueKeys:
            if folded(columns) not in held:
                held.add(folded(columns))
                addUnique.append(columns)

        if not addPrimaryKey and not addUnique:
            return []

        spelled = [self.catalogColumns(table=toTable, columns=list(columns)) for columns in [addPrimaryKey] + addUnique]
        try:
            self.dialect.addKeys(self.cursor, self.statementName(toTable), toTable, self.quoted(spelled[0]),
                                 [self.quoted(columns) for columns in spelled[1:]])
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            self.primaryKeyCache.pop(toTable, None)
            self.columnNameCache.pop(toTable, None)

        return ((['primary key ({})'.format(', '.join(spelled[0]))] if addPrimaryKey else [])
                + ['unique ({})'.format(', '.join(columns)) for columns in spelled[1:]])


    def copyAccess(self, fromTable: str, toTable: str) -> Tuple[List[str], List[str]]:
        """Gives `toTable` the plain indexes and table grants of `fromTable` it
        lacks, and says what it gave and what it couldn't. For a swap's stage
        table, as copyKeys gives it the keys: the two tables trade names every
        run, so what only the target had was there every other run -- an
        application's role could read the copy after one run and not after
        the next, and its queries lost their indexes.

        An index over columns `toTable` already indexes, in that order, by
        any index or key, is left out, as is a grant it already has. Each is
        committed on its own, so one that fails -- no privilege to grant it,
        say -- leaves the rest given.
        """

        self._refuseWrite('add indexes and grants to {}'.format(toTable))

        def folded(columns: Sequence[str]) -> Tuple[str, ...]:
            return tuple(column.upper() for column in columns)

        given: List[str] = []
        problems: List[str] = []

        def attempt(what: str, statement: str) -> None:
            try:
                self.cursor.execute(statement)
                self.connection.commit()
                given.append(what)
            except Exception as error:
                self.connection.rollback()
                problems.append('{} -- {}'.format(what, describeError(error)))

        held = {folded(columns) for _, columns in self.dialect.plainIndexes(self.cursor, toTable)}
        held |= {folded(columns) for columns in self.dialect.uniqueKeys(self.cursor, toTable)} if self.dialect.plainIndexesQuery() else set()
        statementTable = self.statementName(toTable)
        for name, columns in self.dialect.plainIndexes(self.cursor, fromTable):
            if folded(columns) in held:
                continue
            held.add(folded(columns))
            try:
                spelled = self.catalogColumns(table=toTable, columns=list(columns))
            except ConfigurationError as error:
                # A column the stage lacks: this index can't be given, the rest still can.
                problems.append('index ({}) -- {}'.format(', '.join(columns), error))
                continue
            attempt('index ({})'.format(', '.join(spelled)), 'CREATE INDEX {} ON {} ({})'.format(
                quoteIdentifier(self.type, _stageIndexName(name)), statementTable, ', '.join(self.quoted(spelled))))

        grants = {(privilege.upper(), grantee) for privilege, grantee, _ in self.dialect.tableGrants(self.cursor, toTable)}
        for privilege, grantee, grantable in self.dialect.tableGrants(self.cursor, fromTable):
            if (privilege.upper(), grantee) in grants:
                continue
            attempt('{} to {}'.format(privilege, grantee), self.dialect.grantStatement(statementTable, privilege, grantee, grantable))

        self.columnNameCache.pop(toTable, None)

        return given, problems


    def _swapTempTable(self, targetTable: str, stageTable: str) -> str:
        """The temporary name a swap moves the stage table through: the
        target's name with `_tmp`, in the stage table's schema.
        """

        stageSchema, _ = splitTableName(stageTable)
        _, targetName = splitTableName(targetTable)
        # Suffixed as written and quoted afterwards, so the temporary name is
        # spelled like the target it stands in for. The suffix goes inside the
        # quotes of a quoted name: `[group]_tmp` is not a name SQL Server's
        # sp_rename can parse.
        tempName = suffixedName(self.type, targetName, '_tmp')

        return '{}.{}'.format(stageSchema, tempName) if stageSchema else tempName


    def swap(self, targetTable: str, stageTable: str) -> None:
        """Exchanges the two tables by renaming, atomically everywhere but
        Oracle, through a temporary name in the stage table's schema.
        """

        self._refuseWrite('swap {}'.format(targetTable))

        tempTable = self._swapTempTable(targetTable, stageTable)
        self.dialect.swap(self.cursor, targetTable=self.statementName(targetTable), stageTable=self.statementName(stageTable),
                          tempTable=self.statementName(tempTable))
        self.connection.commit()


    def recoverInterruptedSwap(self, targetTable: str, stageTable: str) -> Optional[str]:
        """Puts right a swap whose process was killed between renames, on a
        database where each rename commits alone (Oracle), and says what it
        did, or None where there was nothing to do.

        The renames are stage to temporary, target to stage, temporary to
        target. Killed after the second, the target was missing, and every
        later run failed looking for it while the loaded rows sat under the
        temporary name; killed after the first, the stage was missing. The
        rows under the temporary name are a load that completed, so a missing
        target is given them, finishing the swap, and a missing stage gets
        its table back, undoing it. All three there is no state a swap leaves,
        so it is refused rather than guessed at.
        """

        self._refuseWrite('put right a swap of {}'.format(targetTable))

        if self.dialect.ATOMIC_SWAP:
            return None

        tempTable = self._swapTempTable(targetTable, stageTable)
        if not self.tableExists(tempTable):
            return None

        if not self.tableExists(targetTable):
            renamed, how = targetTable, 'finished it: {} holds the rows that swap loaded'.format(targetTable)
        elif not self.tableExists(stageTable):
            renamed, how = stageTable, 'undid it: {} is the stage table again'.format(stageTable)
        else:
            raise ConfigurationError('{0}, {1} and {2} all exist, which no swap of {0} leaves behind; drop or rename {2} once you have '
                                     'seen what it holds'.format(targetTable, stageTable, tempTable))

        self.cursor.execute(self.dialect.renameStatement(self.statementName(tempTable), self.statementName(renamed)))
        self.connection.commit()

        return 'a swap of {} was stopped part-way, leaving its rows in {}; {}'.format(targetTable, tempTable, how)

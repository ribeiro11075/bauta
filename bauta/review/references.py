"""Counts rows in a copy whose foreign key points at nothing, for `bauta verify-references`.

A copy can hold such rows whenever nothing stopped them: keys the target never
declared, SQLite before it enforced them, MySQL with FOREIGN_KEY_CHECKS off,
and constraints disabled or not validated on the other servers. So every key
is counted, declared or not: the target's own, and the sources' matched to the
copy by table name, as `audit` matches them.

Only counts are reported, never values: some keys are copied unmasked.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from ..database.dialects import ForeignKey, quoteIdentifier, quoteTableName, splitTableName, unqualifiedName
from ..log.scrubbing import describeError


class ReferenceResult(NamedTuple):
    """One foreign key in one target database. `orphans` is None when the key
    couldn't be counted, and `problem` then says why.
    """

    database: str
    table: str
    columns: Tuple[str, ...]
    referencedTable: str
    referencedColumns: Tuple[str, ...]
    declared: bool
    orphans: Optional[int]
    problem: Optional[str]


class _Check(NamedTuple):
    """A key with its tables and columns as the target spells them."""

    key: ForeignKey
    declared: bool
    table: Optional[str]
    columns: Optional[List[str]]
    referencedTable: Optional[str]
    referencedColumns: Optional[List[str]]
    problem: Optional[str]


def orphanQuery(databaseType: Any, table: str, columns: Sequence[str], referencedTable: str, referencedColumns: Sequence[str]) -> str:
    """Counts the rows of `table` whose key matches no row of `referencedTable`.

    A key with a NULL column is skipped, as databases skip it when enforcing.
    Tables are used as given and columns are quoted, so both must be spelled as
    the target spells them. NOT EXISTS runs on all six databases, as `subset`'s
    queries do.
    """

    def quoted(alias: str, column: str) -> str:
        return '{}.{}'.format(alias, quoteIdentifier(databaseType, column))

    present = ' AND '.join('{} IS NOT NULL'.format(quoted('c', column)) for column in columns)
    matches = ' AND '.join('{} = {}'.format(quoted('p', referenced), quoted('c', column)) for column, referenced in zip(columns, referencedColumns))

    return 'SELECT COUNT(*) FROM {} c WHERE {} AND NOT EXISTS (SELECT 1 FROM {} p WHERE {})'.format(table, present, referencedTable, matches)


def _folded(foreignKey: ForeignKey) -> Tuple[Any, ...]:

    return (foreignKey.table.upper(), tuple(column.upper() for column in foreignKey.columns),
            foreignKey.referencedTable.upper(), tuple(column.upper() for column in foreignKey.referencedColumns))


def _plan(database: Any, loaded: Mapping[str, str], sourceKeys: Sequence[ForeignKey]) -> List[_Check]:
    """The keys to count: every key the target declares on a table a job loads,
    then each source key the target doesn't declare, with its tables named as
    the jobs name them and its columns as the target spells them.
    """

    checks = []
    # A catalog key describes the connection's current schema. It applies to a
    # job's table only where the job names that same table: a job loading
    # `other.orders` means another table, whose keys this connection can't read,
    # so its keys are counted against the table the job loads, not the catalog's.
    declared: List[ForeignKey] = []
    qualified: List[ForeignKey] = []
    for foreignKey in database.getForeignKeys():
        table = loaded.get(foreignKey.table.upper())
        if table is None:
            continue
        (declared if splitTableName(table)[0] is None else qualified).append(foreignKey)
    declaredFolded = {_folded(foreignKey) for foreignKey in declared}

    for foreignKey in declared:
        checks.append(_Check(foreignKey, True, quoteTableName(database.type, foreignKey.table), list(foreignKey.columns),
                             quoteTableName(database.type, foreignKey.referencedTable), list(foreignKey.referencedColumns), None))

    columnsByTable: Dict[str, Optional[Dict[str, str]]] = {}

    def spelled(table: str, columns: Sequence[str]) -> Tuple[Optional[List[str]], Optional[str]]:
        if table not in columnsByTable:
            columnsByTable[table] = {column.upper(): column for column in database.getAllColumnNames(table)} if database.tableExists(table) else None
        known = columnsByTable[table]
        if known is None:
            return None, '{} is not in the target'.format(unqualifiedName(table))
        missing = [column for column in columns if column.upper() not in known]
        if missing:
            return None, '{} has no column {}'.format(unqualifiedName(table), ', '.join(missing))
        return [known[column.upper()] for column in columns], None

    seen = set()
    for foreignKey in list(qualified) + list(sourceKeys):
        folded = _folded(foreignKey)
        if foreignKey.table.upper() not in loaded or folded in declaredFolded or folded in seen:
            continue
        seen.add(folded)
        table = loaded[foreignKey.table.upper()]
        # An unloaded parent is looked for beside the child, not in the
        # connection's schema, where a same-named table would be another table.
        schema = splitTableName(table)[0]
        referencedTable = loaded.get(foreignKey.referencedTable.upper())
        if referencedTable is None:
            referencedTable = '{}.{}'.format(schema, foreignKey.referencedTable) if schema else foreignKey.referencedTable
        columns, problem = spelled(table, foreignKey.columns)
        referencedColumns, referencedProblem = spelled(referencedTable, foreignKey.referencedColumns)
        checks.append(_Check(foreignKey, False, table, columns, referencedTable, referencedColumns, problem or referencedProblem))

    return checks


def verifyReferences(database: Any, alias: str, loaded: Mapping[str, str], sourceKeys: Sequence[ForeignKey] = ()) -> List[ReferenceResult]:
    """Counts orphaned rows for each foreign key on a table the jobs load.

    `loaded` maps each loaded table's unqualified, upper-cased name to the
    table as its job names it (targetTableFinal). `sourceKeys` are the foreign
    keys of the databases copied from, for a target that declares fewer. A key
    that can't be counted -- a table or column the target lacks, a query the
    database refuses -- is reported with its problem, not raised.
    """

    results = []

    for check in _plan(database, loaded, sourceKeys):
        key = check.key
        orphans, problem = None, check.problem
        if problem is None:
            assert check.table and check.columns and check.referencedTable and check.referencedColumns
            try:
                orphans = int(database.query(orphanQuery(database.type, check.table, check.columns, check.referencedTable,
                                                         check.referencedColumns))[0][0])
            except Exception as error:
                # A failed statement aborts PostgreSQL's transaction until rolled back.
                database.connection.rollback()
                problem = describeError(error)
        if check.declared:
            names = key.table, key.columns, key.referencedTable, key.referencedColumns
        else:
            # As the copy spells them where it could be read, else as the source does.
            names = (check.table or key.table, tuple(check.columns or key.columns), check.referencedTable or key.referencedTable,
                     tuple(check.referencedColumns or key.referencedColumns))
        results.append(ReferenceResult(alias, *names, check.declared, orphans, problem))

    return results


def renderReferences(results: Sequence[ReferenceResult]) -> str:
    """The results for a terminal: one line a key, then a summary."""

    lines = []

    for result in results:
        key = '{}: {} ({}) -> {} ({}){}'.format(result.database, result.table, ', '.join(result.columns), result.referencedTable,
                                                ', '.join(result.referencedColumns), '' if result.declared else ' [not declared]')
        outcome = result.problem if result.orphans is None else '{} orphaned row(s)'.format(result.orphans)
        lines.append('{:<8} {}: {}'.format(_status(result), key, outcome))

    lines.append(summarize(results))

    return '\n'.join(lines) + '\n'


def _status(result: ReferenceResult) -> str:

    if result.orphans is None:
        return 'ERROR'

    return 'ORPHANS' if result.orphans else 'OK'


def summarize(results: Sequence[ReferenceResult]) -> str:

    orphaned = sum(1 for result in results if result.orphans)
    unchecked = sum(1 for result in results if result.orphans is None)

    return 'Checked {} foreign key(s): {} with orphaned rows, {} not checked'.format(len(results), orphaned, unchecked)


def referencesReport(results: Sequence[ReferenceResult], generatedAt: str) -> Dict[str, Any]:
    """The results as a JSON-ready dict."""

    return {
        'generatedAt': generatedAt,
        'references': [dict(result._asdict(), status=_status(result).lower()) for result in results],
        'summary': {
            'checked': len(results),
            'orphaned': sum(1 for result in results if result.orphans),
            'notChecked': sum(1 for result in results if result.orphans is None),
            },
        }

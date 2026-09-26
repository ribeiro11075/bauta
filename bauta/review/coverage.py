"""What of a source database the jobs actually cover.

`audit` answers "is this job safe?" for each job that exists. This answers the
question a reviewer asks instead: **what in production is not covered at all?**
A table nobody wrote a job for is invisible to every other command -- it has no
job to audit -- and that is exactly the table a new release adds.

A table counts as covered when some job reading that database names it in its
sourceQuery, which is as far as a check that doesn't parse SQL can see, the same
approximation audit._mentions makes. A table deliberately left out is declared
in `acknowledged`, so "we don't copy this" is a recorded decision rather than an
omission.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .audit import _mentions, _tableName
from ..configuration import DataJobConfig
from ..generate.discovery import BUILTIN_RULES, DiscoveryRules, personalDataHint

# What a table can be. Only UNCOVERED fails: the others are all decisions
# somebody made and can be shown to have made.
MASKED = 'masked'
COPIED = 'copied'
ACKNOWLEDGED = 'acknowledged'
UNCOVERED = 'uncovered'

# In the order a report reads best: what is wrong first.
STATE_ORDER = (UNCOVERED, COPIED, MASKED, ACKNOWLEDGED)

STATE_LABELS = {
    MASKED: 'copied and masked',
    COPIED: 'copied as it stands',
    ACKNOWLEDGED: 'not copied, declared',
    UNCOVERED: 'NOT COVERED',
    }


def jobsReading(table: str, alias: str, jobs: Mapping[str, DataJobConfig]) -> List[str]:
    """The jobs that read `table` from `alias`, by naming it in sourceQuery."""

    return sorted(name for name, job in jobs.items()
                  if job.sourceConnection == alias and _mentions(job.sourceQuery, table))


def coverageReport(alias: str, tables: Sequence[str], jobs: Mapping[str, DataJobConfig],
                   acknowledged: Optional[Mapping[str, str]] = None,
                   columns: Optional[Mapping[str, Sequence[str]]] = None,
                   rules: DiscoveryRules = BUILTIN_RULES) -> Dict[str, Any]:
    """Every table in `tables`, and what the jobs do with it.

    `acknowledged` maps a table left out on purpose to why. `columns` maps a
    table to its columns, where they could be read, so an uncovered table
    holding what looks like personal data can say so.
    """

    acknowledged = acknowledged or {}
    columns = columns or {}
    declared = {_tableName(table).upper(): reason for table, reason in acknowledged.items()}

    entries = []
    for table in tables:
        folded = _tableName(table).upper()
        readers = jobsReading(table, alias, jobs)
        masked = [name for name in readers if jobs[name].masking is not None]

        if readers:
            state = MASKED if masked else COPIED
            reason = None
        elif folded in declared:
            state, reason = ACKNOWLEDGED, declared[folded]
        else:
            state, reason = UNCOVERED, None

        entry: Dict[str, Any] = {'table': table, 'state': state, 'jobs': readers}
        if reason is not None:
            entry['reason'] = reason
        if state == UNCOVERED:
            personal = [column for column in columns.get(table, ()) if personalDataHint(column, rules)]
            if personal:
                entry['personalDataColumns'] = personal
        entries.append(entry)

    entries.sort(key=lambda entry: (STATE_ORDER.index(entry['state']), entry['table'].upper()))
    summary = {state: sum(1 for entry in entries if entry['state'] == state) for state in STATE_ORDER}
    unknown = sorted(set(declared) - {_tableName(table).upper() for table in tables})

    return {'database': alias, 'tables': entries, 'summary': summary, 'acknowledgedButAbsent': unknown}


def renderCoverage(report: Mapping[str, Any]) -> str:
    """The report for a terminal, worst first."""

    lines = ['{}: {} table(s)'.format(report['database'], len(report['tables'])), '']

    for entry in report['tables']:
        line = '  {:<40} {}'.format(entry['table'], STATE_LABELS[entry['state']])
        if entry['jobs']:
            line += ' by {}'.format(', '.join(entry['jobs']))
        if entry.get('reason'):
            line += ' -- {}'.format(entry['reason'])
        lines.append(line)
        for column in entry.get('personalDataColumns', ()):
            lines.append('    {:<38} looks like personal data'.format(column))

    summary = report['summary']
    lines.extend(['', 'Covered: {} masked, {} copied as they stand, {} declared not copied. NOT COVERED: {}.'.format(
        summary[MASKED], summary[COPIED], summary[ACKNOWLEDGED], summary[UNCOVERED])])

    if report['acknowledgedButAbsent']:
        lines.append('Declared in `acknowledged` but not in this database, so the declaration is stale: {}.'.format(
            ', '.join(report['acknowledgedButAbsent'])))

    if summary[UNCOVERED]:
        lines.append('Add a job for each table above, or declare it in `acknowledged` with the reason it is not copied.')

    return '\n'.join(lines) + '\n'

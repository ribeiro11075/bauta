"""`discover --update`: what has changed under a masked job's policy since it
was written -- columns its sourceQuery now returns that the policy doesn't
name, and columns the policy names that it no longer returns -- with a
proposal for each new one, and the edit that brings the job's file up to date.

Either kind of drift stops the job, which is the point of covering every
column. This turns the stop into a change to review rather than YAML to
write by hand.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, NamedTuple, Optional, Sequence

import yaml

from ..configuration import DataJobConfig, parseYaml
from ..database.dialects import catalogTable
from .discovery import BUILTIN_RULES, DEFAULT_SAMPLE_SIZE, DiscoveryRules, Suggestion, _flow, _scalar, proposeTable, suggestColumn

# A sourceQuery that reads one table whole, as discover writes them: its
# columns are proposed from the table, keys and foreign keys included. The
# name may be qualified, each part quoted or not, as discover writes a table
# of another schema: `"app"."orders"`.
_NAME_PART = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[\w$#]+)'
_WHOLE_TABLE = re.compile(r'^\s*select\s+\*\s+from\s+({0}(?:\.{0})*)\s*;?\s*$'.format(_NAME_PART), re.IGNORECASE)

# Marks a line --apply wrote, so a reviewer sees which lines are new.
PROPOSED = 'proposed by discover --update'


class DriftError(Exception):
    """A job's file couldn't be edited safely; the proposal stands, to apply by hand."""


class JobDrift(NamedTuple):
    """A masked job's policy against what its sourceQuery returns now."""

    job: str
    added: List[Suggestion]
    removed: List[str]

    def any(self) -> bool:

        return bool(self.added or self.removed)


def _wholeTable(query: str) -> Optional[str]:

    match = _WHOLE_TABLE.match(query)

    return match.group(1) if match else None


def findDrift(database: Any, name: str, job: DataJobConfig, rules: DiscoveryRules = BUILTIN_RULES, sampleSize: int = DEFAULT_SAMPLE_SIZE,
              maskKeys: bool = False, foreignKeys: Optional[List[Any]] = None) -> JobDrift:
    """How `job`'s policy differs from what its sourceQuery returns from
    `database`, its source, with a suggestion for each column it doesn't
    name. Sampled values stay in memory, as discover's do.

    A job with a `defaultStrategy` covers every column already, so nothing
    it returns is new to it.
    """

    assert job.masking is not None
    query, parameters = job.sourceQuery, None
    if job.watermarkColumn:
        query = database.substituteWatermarkPlaceholder(query)
        parameters = (job.watermarkInitial,)

    columns, chunks = database.stream(query=query, chunkSize=sampleSize, parameters=parameters)
    with chunks:
        rows = list(next(chunks, []))

    named = {column.upper(): column for column in job.masking.columns}
    returned = {column.upper() for column in columns}
    removed = [column for folded, column in named.items() if folded not in returned]
    new = [column for column in columns if column.upper() not in named] if job.masking.defaultStrategy is None else []

    if not new:
        return JobDrift(job=name, added=[], removed=removed)

    table = _wholeTable(job.sourceQuery)
    if table is not None:
        # The table's own proposal, so a new key column gets the domain its
        # foreign key shares, as it would have from discover.
        proposal = proposeTable(database, catalogTable(database.type, table), sampleSize=sampleSize, foreignKeys=foreignKeys, rules=rules,
                                maskKeys=maskKeys)
        byColumn = {suggestion.column.upper(): suggestion for suggestion in proposal.columns}
        added = [byColumn.get(column.upper()) or suggestColumn(table, column, None, [], rules=rules, maskKeys=maskKeys) for column in new]
    else:
        added = [suggestColumn(name, column, None, [row[columns.index(column)] for row in rows], rules=rules, maskKeys=maskKeys)
                 for column in new]

    return JobDrift(job=name, added=added, removed=removed)


def renderDrift(drifts: Sequence[JobDrift], origins: Dict[str, Any]) -> str:
    """A report of each job's drift, with the lines to add under its
    `columns:` and the ones to remove, for review or to apply by hand.
    """

    lines = []
    for drift in drifts:
        lines.append('{} ({}):'.format(drift.job, origins.get(drift.job, '?')))
        for suggestion in drift.added:
            lines.append('  + {}: {}  # {}'.format(_scalar(suggestion.column), _flow(suggestion.policy), suggestion.reason))
        for column in drift.removed:
            lines.append('  - {}  # no longer returned by sourceQuery'.format(_scalar(column)))

    return '\n'.join(lines) + '\n' if lines else ''


def _indent(line: str) -> int:

    return len(line) - len(line.lstrip(' '))


def _isContent(line: str) -> bool:
    """Neither blank nor a comment alone, so it says where a block ends."""

    stripped = line.strip()

    return bool(stripped) and not stripped.startswith('#')


def _key(line: str) -> Optional[str]:
    """The mapping key a block-style line starts with, unquoted, or None."""

    match = re.match(r'''^\s*(?:'((?:[^']|'')*)'|"((?:[^"\\]|\\.)*)"|([^\s:#'"][^:#]*?))\s*:(?:\s|$)''', line)
    if not match:
        return None
    single, double, plain = match.groups()

    return single.replace("''", "'") if single is not None else double if double is not None else plain


def _block(lines: List[str], start: int) -> int:
    """The index just past the block whose header is lines[start]: the first
    content line indented no deeper than the header.
    """

    indent = _indent(lines[start])
    end = start + 1
    last = start + 1
    while end < len(lines):
        if _isContent(lines[end]):
            if _indent(lines[end]) <= indent:
                break
            last = end + 1
        end += 1

    return last


def _child(lines: List[str], start: int, end: int, key: str) -> int:
    """The line in lines[start+1:end] that opens `key` one level under
    lines[start], in block style.
    """

    children = [index for index in range(start + 1, end) if _isContent(lines[index])]
    if not children:
        raise DriftError('{} holds nothing in block style'.format(lines[start].strip()))
    level = _indent(lines[children[0]])
    for index in children:
        if _indent(lines[index]) == level and _key(lines[index]) == key:
            if lines[index].split(':', 1)[1].split('#', 1)[0].strip():
                raise DriftError('{} is written in flow style ({}); edit it by hand'.format(key, lines[index].strip()))
            return index

    raise DriftError('no block-style {}: found under {}'.format(key, lines[start].strip()))


def applyDrift(text: str, drift: JobDrift) -> str:
    """`text`, a jobs file, with `drift` applied to its job's `columns:`: new
    columns appended with their reasons, stale ones removed with any lines
    they continue onto. Everything else -- comments, order, quoting -- is left
    as written. Only block style is edited; anything else is a DriftError,
    and the report says what to change.
    """

    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'

    jobsLine = next((index for index, line in enumerate(lines) if _indent(line) == 0 and _key(line) == 'jobs'), None)
    if jobsLine is None:
        raise DriftError('no jobs: in block style')
    jobLine = _child(lines, jobsLine, _block(lines, jobsLine), drift.job)
    maskingLine = _child(lines, jobLine, _block(lines, jobLine), 'masking')
    columnsLine = _child(lines, maskingLine, _block(lines, maskingLine), 'columns')
    columnsEnd = _block(lines, columnsLine)

    entries = [index for index in range(columnsLine + 1, columnsEnd) if _isContent(lines[index])]
    level = _indent(lines[entries[0]]) if entries else _indent(lines[columnsLine]) + 2
    removed = {column.upper() for column in drift.removed}
    dropped = set()
    for index in entries:
        if _indent(lines[index]) == level and (_key(lines[index]) or '').upper() in removed:
            dropped.add(index)
            # A policy written over several lines continues, indented deeper.
            following = index + 1
            while following < columnsEnd and _isContent(lines[following]) and _indent(lines[following]) > level:
                dropped.add(following)
                following += 1

    added = ['{}{}: {}  # {}; {}\n'.format(' ' * level, _scalar(suggestion.column), _flow(suggestion.policy), suggestion.reason, PROPOSED)
             for suggestion in drift.added]
    edited = [line for index, line in enumerate(lines[:columnsEnd]) if index not in dropped] + added + lines[columnsEnd:]
    result = ''.join(edited)

    _checkApplied(result, drift)

    return result


def _checkApplied(text: str, drift: JobDrift) -> None:
    """That the edited file still parses, and its job's policy now names
    exactly what it should: an edit the text-level reading got wrong is
    refused rather than written.
    """

    try:
        document = parseYaml(text)
        columns = {str(column).upper() for column in document['jobs'][drift.job]['masking']['columns']}
    except (yaml.YAMLError, KeyError, TypeError) as error:
        raise DriftError('the edited file would not read back ({}); apply the change by hand'.format(error)) from error

    missing = [suggestion.column for suggestion in drift.added if suggestion.column.upper() not in columns]
    remaining = [column for column in drift.removed if column.upper() in columns]
    if missing or remaining:
        raise DriftError('the edit would not have added {} or removed {}; apply the change by hand'.format(
            ', '.join(missing) or 'nothing', ', '.join(remaining) or 'nothing'))

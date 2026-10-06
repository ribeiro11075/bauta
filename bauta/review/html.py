"""`audit` and `coverage` as one HTML file, for a privacy or compliance
reviewer who reads the report rather than running the command.

Rendered from the same report the text and JSON formats are, so it holds
what they hold and nothing more: names of jobs, tables, columns and
connections, strategies, key fingerprints and findings -- never a value
from a table, and never a key. Every one of those is escaped, since a
table or job name is whatever the database or jobs.yaml says it is.

One file, with nothing fetched: no script, font, stylesheet or image from
anywhere else, so it reads the same attached to a ticket, opened offline or
archived beside a release. Light or dark as the reader's system is.
"""
from __future__ import annotations

import html
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .coverage import ACKNOWLEDGED, COPIED, MASKED, STATE_ORDER, UNCOVERED

# Colours as tokens, redefined for a dark system. Severity and state colours
# carry a word beside them, never meaning alone.
_STYLE = '''
:root { color-scheme: light dark; --page: #ffffff; --text: #1b1f24; --muted: #57606a; --line: #d8dee4; --panel: #f6f8fa;
  --error: #b42318; --error-bg: #fef3f2; --warning: #9a6700; --warning-bg: #fff8e5; --info: #0b62c4; --info-bg: #eef6ff;
  --good: #1a7f37; --good-bg: #ecfdf3; }
@media (prefers-color-scheme: dark) {
  :root { --page: #0d1117; --text: #e6edf3; --muted: #9da7b3; --line: #30363d; --panel: #161b22;
    --error: #ff8f86; --error-bg: #3a1714; --warning: #e3b341; --warning-bg: #3a2a07; --info: #79c0ff; --info-bg: #0c2d4a;
    --good: #56d364; --good-bg: #0f2e18; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--text); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1040px; margin: 0 auto; padding: 32px 16px 48px; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 19px; margin: 36px 0 12px; padding-bottom: 6px; border-bottom: 1px solid var(--line); }
h3 { font-size: 16px; margin: 0 0 4px; }
.meta, .note { color: var(--muted); }
.meta { margin: 0; }
code, td.name { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; overflow-wrap: anywhere; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-top: 24px; }
.tile { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }
.tile strong { display: block; font-size: 26px; line-height: 1.2; }
.tile span { color: var(--muted); font-size: 13px; }
.tile.error strong { color: var(--error); } .tile.warning strong { color: var(--warning); } .tile.good strong { color: var(--good); }
.verdict { margin-top: 20px; padding: 12px 14px; border-radius: 8px; border: 1px solid var(--line); }
.verdict.error { background: var(--error-bg); } .verdict.warning { background: var(--warning-bg); } .verdict.good { background: var(--good-bg); }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; vertical-align: top; padding: 7px 10px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; white-space: nowrap; }
.badge.error { color: var(--error); background: var(--error-bg); } .badge.warning { color: var(--warning); background: var(--warning-bg); }
.badge.info { color: var(--info); background: var(--info-bg); } .badge.good { color: var(--good); background: var(--good-bg); }
.badge.plain { color: var(--muted); background: var(--panel); border: 1px solid var(--line); }
ul.findings { list-style: none; margin: 0; padding: 0; }
ul.findings li { padding: 8px 0; border-bottom: 1px solid var(--line); }
ul.findings li .badge { margin-right: 8px; }
article { border: 1px solid var(--line); border-radius: 8px; padding: 16px; margin: 0 0 16px; }
article p { margin: 4px 0 12px; }
footer { margin-top: 40px; color: var(--muted); font-size: 13px; }
'''

_SEVERITY_LABELS = {'error': 'Error', 'warning': 'Warning', 'info': 'Note'}

_STATE_BADGES = {UNCOVERED: 'error', COPIED: 'warning', MASKED: 'good', ACKNOWLEDGED: 'plain'}

# Section headings, in sentence case where the text report shouts.
_STATE_HEADINGS = {UNCOVERED: 'Not covered', COPIED: 'Copied as it stands', MASKED: 'Copied and masked', ACKNOWLEDGED: 'Not copied, declared'}

_FOOTER = ('This report holds the names of jobs, connections, tables and columns, how each column is masked, key fingerprints and '
           'findings. It holds no value from any table, and no masking key.')


def _text(value: Any) -> str:

    return html.escape('' if value is None else str(value), quote=True)


def _page(title: str, heading: str, meta: str, body: Sequence[str]) -> str:

    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="color-scheme" content="light dark">\n'
            '<title>{}</title>\n<style>{}</style>\n</head>\n<body>\n<main>\n<header>\n<h1>{}</h1>\n<p class="meta">{}</p>\n</header>\n'
            '{}\n<footer>{}</footer>\n</main>\n</body>\n</html>\n').format(_text(title), _STYLE, _text(heading), meta, '\n'.join(body),
                                                                         _text(_FOOTER))


def _tiles(tiles: Iterable[Sequence[Any]]) -> str:

    return '<section class="tiles">{}</section>'.format(''.join(
        '<div class="tile {}"><strong>{}</strong><span>{}</span></div>'.format(kind, _text(count), _text(label)) for count, label, kind in tiles))


def _badge(label: str, kind: str) -> str:

    return '<span class="badge {}">{}</span>'.format(kind, _text(label))


def _table(headings: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    """A table whose cells are already HTML."""

    return '<div class="scroll"><table><thead><tr>{}</tr></thead><tbody>{}</tbody></table></div>'.format(
        ''.join('<th scope="col">{}</th>'.format(_text(heading)) for heading in headings),
        ''.join('<tr>{}</tr>'.format(''.join(row)) for row in rows))


def _findings(findings: Sequence[Mapping[str, Any]], withJob: bool = True) -> str:

    items = []
    for finding in findings:
        job = '<code>{}</code>: '.format(_text(finding['job'])) if withJob and finding['job'] else ''
        items.append('<li>{}{}{}</li>'.format(_badge(_SEVERITY_LABELS[finding['severity']], finding['severity']), job, _text(finding['message'])))

    return '<ul class="findings">{}</ul>'.format(''.join(items))


def _plural(count: int, noun: str) -> str:

    return '{} {}{}'.format(count, noun, '' if count == 1 else 's')


def renderAuditHtml(report: Mapping[str, Any], version: Optional[str] = None) -> str:
    """The audit report as one HTML page: a summary and every finding first,
    then a section per job with how it masks each column and what was found
    about it, then the connections.
    """

    summary = report['summary']
    jobs = report['jobs']
    findings = report['findings']
    masked = sum(1 for job in jobs if job['masked'])
    byJob: Dict[Optional[str], List[Mapping[str, Any]]] = {}
    for finding in findings:
        byJob.setdefault(finding['job'], []).append(finding)

    if summary['error']:
        verdict = ('error', 'Not ready: {} must be resolved before this copy can be relied on.'.format(_plural(summary['error'], 'error')))
    elif summary['warning']:
        verdict = ('warning', 'No errors. {} for a reviewer to accept or resolve.'.format(_plural(summary['warning'], 'warning')))
    else:
        verdict = ('good', 'No errors or warnings.')

    body = [_tiles([(summary['error'], 'errors', 'error' if summary['error'] else 'good'),
                    (summary['warning'], 'warnings', 'warning' if summary['warning'] else 'good'),
                    (summary['info'], 'notes', ''),
                    (len(jobs), 'jobs', ''),
                    (masked, 'jobs masking', ''),
                    (len(jobs) - masked, 'jobs not masking', 'warning' if len(jobs) - masked else '')]),
            '<p class="verdict {}">{}</p>'.format(verdict[0], _text(verdict[1]))]

    body.append('<h2>Findings</h2>')
    body.append(_findings(findings) if findings else '<p class="note">Nothing to report.</p>')

    body.append('<h2>Jobs</h2>')
    for job in jobs:
        body.append(_auditJob(job, byJob.get(job['job'], [])))

    if report['connections']:
        body.append('<h2>Connections</h2>')
        rows = []
        for alias, connection in report['connections'].items():
            encrypted = {True: ('encrypted', 'good'), False: ('NOT encrypted', 'error'), None: ('unknown', 'plain')}[connection['encrypted']]
            rows.append(['<td class="name">{}</td>'.format(_text(alias)), '<td>{}</td>'.format(_badge(*encrypted))])
        body.append(_table(['Connection', 'In transit'], rows))

    meta = 'Generated {}{} &middot; {} &middot; {}'.format(
        _text(report['generatedAt']), ' by bauta {}'.format(_text(version)) if version else '', _plural(len(jobs), 'job'),
        'columns resolved by running each query' if any(job.get('columnsResolved') for job in jobs) else 'policies as declared')

    return _page('bauta audit', 'Masking audit', meta, body)


def _auditJob(job: Mapping[str, Any], findings: Sequence[Mapping[str, Any]]) -> str:

    status = _badge('masked', 'good') if job['masked'] else _badge('not masked, declared', 'warning') if job.get('unmasked') \
        else _badge('not masked', 'error')
    parts = ['<article id="job-{}">'.format(_text(job['job'])),
             '<h3><code>{}</code> {}{}</h3>'.format(_text(job['job']), status, '' if job['active'] else ' ' + _badge('inactive', 'plain')),
             '<p>From <code>{}</code> into <code>{}</code>, table <code>{}</code>.{}</p>'.format(
                 _text(job['sourceConnection']), _text(job['targetConnection']), _text(job['targetTable']),
                 ' Masked under key <code>{}</code>.'.format(_text(job['keyFingerprint'])) if job['masked'] else '')]

    if job['masked']:
        rows = []
        for column in job['columns']:
            origin = 'defaultStrategy' if column['source'] == 'defaultStrategy' else 'policy'
            hint = column.get('personalDataHint')
            rows.append(['<td class="name">{}</td>'.format(_text(column['column'])), '<td><code>{}</code></td>'.format(_text(column['strategy'])),
                         '<td class="name">{}</td>'.format(_text(column['domain'] or '')), '<td>{}</td>'.format(_text(origin)),
                         '<td>{}</td>'.format(_text(hint or ''))])
        if job.get('defaultStrategy') and not job.get('columnsResolved'):
            rows.append(['<td class="name">any other column</td>', '<td><code>{}</code></td>'.format(_text(job['defaultStrategy']['strategy'])),
                         '<td></td>', '<td>defaultStrategy</td>', '<td></td>'])
        parts.append(_table(['Column', 'Strategy', 'Domain', 'From', 'Looks like'], rows))
        if not job.get('columnsResolved'):
            parts.append('<p class="note">Columns as the policy declares them; run with --connect to see what the query returns.</p>')

    if findings:
        parts.append(_findings(findings, withJob=False))

    parts.append('</article>')

    return ''.join(parts)


def renderCoverageHtml(report: Mapping[str, Any], generatedAt: Optional[str] = None, version: Optional[str] = None) -> str:
    """The coverage report as one HTML page: a count of tables in each state,
    then a section per state, what is not covered first, with each table and
    the jobs or reason that cover it.
    """

    summary = report['summary']
    tables = report['tables']

    if summary[UNCOVERED]:
        verdict = ('error', '{} that no job copies and nothing declares left out.'.format(_plural(summary[UNCOVERED], 'table')))
    else:
        verdict = ('good', 'Every table is either copied by a job or declared as not copied.')

    body = [_tiles([(summary[UNCOVERED], 'not covered', 'error' if summary[UNCOVERED] else 'good'),
                    (summary[MASKED], 'copied and masked', ''),
                    (summary[COPIED], 'copied as they stand', 'warning' if summary[COPIED] else ''),
                    (summary[ACKNOWLEDGED], 'not copied, declared', ''),
                    (len(tables), 'tables', '')]),
            '<p class="verdict {}">{}</p>'.format(verdict[0], _text(verdict[1]))]

    if report['acknowledgedButAbsent']:
        body.append('<p class="verdict warning">Declared in <code>acknowledged</code> but not in this database, so the declaration is stale: '
                    '{}.</p>'.format(', '.join('<code>{}</code>'.format(_text(table)) for table in report['acknowledgedButAbsent'])))

    for state in STATE_ORDER:
        entries = [entry for entry in tables if entry['state'] == state]
        if not entries:
            continue
        body.append('<h2>{} {}</h2>'.format(_text(_STATE_HEADINGS[state]), _badge(str(len(entries)), _STATE_BADGES[state])))
        rows = []
        for entry in entries:
            if state == UNCOVERED:
                detail = ', '.join('<code>{}</code>'.format(_text(column)) for column in entry.get('personalDataColumns', ()))
                detail = 'Columns that look like personal data: {}'.format(detail) if detail else ''
            elif state == ACKNOWLEDGED:
                detail = _text(entry.get('reason'))
            else:
                detail = ', '.join('<code>{}</code>'.format(_text(job)) for job in entry['jobs'])
            rows.append(['<td class="name">{}</td>'.format(_text(entry['table'])), '<td>{}</td>'.format(detail)])
        heading = {UNCOVERED: 'What it holds', ACKNOWLEDGED: 'Why it is not copied'}.get(state, 'By')
        body.append(_table(['Table', heading], rows))

    if summary[UNCOVERED]:
        body.append('<p class="note">Add a job for each table not covered, or declare it in <code>acknowledged</code> with the reason it '
                    'is not copied.</p>')

    meta = '{}{}{}'.format('Database <code>{}</code>'.format(_text(report['database'])),
                           ' &middot; generated {}'.format(_text(generatedAt)) if generatedAt else '',
                           ' by bauta {}'.format(_text(version)) if version else '')

    return _page('bauta coverage', 'Coverage of {}'.format(report['database']), meta, body)

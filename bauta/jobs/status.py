"""What is running now: one small JSON file per live cycle, in a directory
beside the run-state lock, rewritten as each job starts or is decided and
removed when the cycle ends. `bauta status` reads them, as can anything else
on the machine -- a console, a health check.

A file whose process is gone is a run that ended without removing it -- a
`kill -9`, a machine that lost power -- and readers say so rather than show
it as running. History, not this, is the record of what ran.
"""
from __future__ import annotations

import datetime
import json
import os
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional

from .runner import RunProgress

RUNNING = 'running'
WAITING = 'waiting'
# The suffix of a live cycle's file, and of the file being written in its place.
STATUS_SUFFIX = '.json'
PARTIAL_SUFFIX = '.partial'


def _timestamp(seconds: Optional[float]) -> Optional[str]:

    if not seconds:
        return None

    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat(timespec='seconds')


def statusRecord(progress: RunProgress, pid: Optional[int] = None, host: Optional[str] = None) -> Dict[str, Any]:
    """A cycle's progress as its file holds it: the run, the process running
    it, and each job's state -- waiting, running, or its outcome's status --
    in the order the cycle holds them. Never an error longer than history
    keeps, and never a value.
    """

    from .reporting import ERROR_TEXT_LIMIT

    outcomes = {outcome.job: outcome for outcome in progress.outcomes}
    jobs = []
    for job in progress.jobs:
        outcome = outcomes.get(job)
        if outcome is not None:
            jobs.append({'job': job, 'status': outcome.status.value, 'startedAt': _timestamp(outcome.startedAt),
                         'finishedAt': _timestamp(outcome.finishedAt), 'rowCount': outcome.rowCount,
                         'error': (outcome.error or None) and outcome.error[:ERROR_TEXT_LIMIT]})
        elif job in progress.running:
            jobs.append({'job': job, 'status': RUNNING, 'startedAt': _timestamp(progress.running[job]), 'finishedAt': None,
                         'rowCount': (progress.rows or {}).get(job, 0), 'error': None})
        else:
            jobs.append({'job': job, 'status': WAITING, 'startedAt': None, 'finishedAt': None, 'rowCount': None, 'error': None})

    return {'runId': progress.runId, 'startedAt': _timestamp(progress.startedAt), 'pid': os.getpid() if pid is None else pid,
            'host': host or socket.gethostname(), 'jobs': jobs}


class RunStatus:
    """Keeps a live cycle's file in `directory`: a RunProgress callback for
    runDataJobs' `onProgress`.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)


    def __call__(self, progress: RunProgress) -> None:

        path = self.directory / (progress.runId + STATUS_SUFFIX)
        if progress.finished:
            path.unlink(missing_ok=True)
            return

        self.directory.mkdir(parents=True, exist_ok=True)
        # Written beside it and moved into place, so a reader never sees half.
        partial = path.with_name(path.name + PARTIAL_SUFFIX)
        partial.write_text(json.dumps(statusRecord(progress), indent=2) + '\n')
        os.replace(partial, path)


def _processAlive(pid: int) -> bool:

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process, which exists.
        return True

    return True


def readRunStatuses(directory: Path) -> List[Dict[str, Any]]:
    """Each live cycle's record in `directory`, the latest started first, with
    `alive`: True or False where its process is on this machine, which can be
    asked, and None where it is on another sharing the directory. Empty if
    nothing is running. A file being replaced as it is read is skipped; the
    next read finds it.
    """

    if not Path(directory).is_dir():
        return []

    host = socket.gethostname()
    records = []
    for path in Path(directory).glob('*' + STATUS_SUFFIX):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        record['alive'] = _processAlive(int(record['pid'])) if record.get('host') == host else None
        records.append(record)

    return sorted(records, key=lambda record: record.get('startedAt') or '', reverse=True)


def renderRunStatuses(records: List[Dict[str, Any]]) -> str:
    """Each live run and its jobs, as `bauta status` prints them. Times are
    UTC, as recorded, and labelled so.
    """

    if not records:
        return 'Nothing is running.\n'

    lines = []
    for record in records:
        if record['alive'] is False:
            state = 'ended without finishing: process {} is gone'.format(record['pid'])
        elif record['alive'] is None:
            state = 'running on {}, process {}'.format(record['host'], record['pid'])
        else:
            state = 'running, process {}'.format(record['pid'])
        counts: Dict[str, int] = {}
        for job in record['jobs']:
            counts[job['status']] = counts.get(job['status'], 0) + 1
        lines.append('Run {}, started {} UTC: {}'.format(record['runId'], (record['startedAt'] or '-')[:19].replace('T', ' '), state))
        lines.append('  ' + ', '.join('{} {}'.format(count, status) for status, count in sorted(counts.items())))
        lines.append('  {:<28} {:<10} {:<20} {:>10}  {}'.format('JOB', 'STATUS', 'STARTED (UTC)', 'ROWS', 'ERROR'))
        for job in record['jobs']:
            lines.append('  {:<28} {:<10} {:<20} {:>10}  {}'.format(job['job'], job['status'], (job['startedAt'] or '-')[:19].replace('T', ' '),
                                                                   '-' if job['rowCount'] is None else job['rowCount'], (job['error'] or '')[:80]))
        lines.append('')

    return '\n'.join(lines)

"""What happened, for people and for monitoring: run history and
notifications.

Each takes a cycle's RunResult -- runDataJobs hands one to its `onCycle`
callback as every cycle ends -- and none of them can stop a load: the runner
logs a reporting failure and carries on.
"""
from __future__ import annotations

import datetime
import json
import os
import socket
import urllib.request
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from ..configuration import ConnectionConfig
from ..database import Database
from .memory import exclusiveLock
from .runner import RunResult

HTTP_TIMEOUT_SECONDS = 10

ERROR_TEXT_LIMIT = 2000


def _timestamp(seconds: float) -> Optional[str]:

    if not seconds:
        return None

    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat(timespec='seconds')


def historyRecords(result: RunResult, runId: str) -> List[Dict[str, Any]]:
    """One record per job in the cycle."""

    return [{
        'runId': runId,
        'job': outcome.job,
        'status': outcome.status.value,
        'rowCount': outcome.rowCount,
        'attempts': outcome.attempts,
        'startedAt': _timestamp(outcome.startedAt),
        'finishedAt': _timestamp(outcome.finishedAt),
        'durationSeconds': round(outcome.durationSeconds, 3),
        'error': (outcome.error or None) and outcome.error[:ERROR_TEXT_LIMIT],
        } for outcome in result.outcomes]


# How much of the history file is read at a time when reading it backwards.
TAIL_BLOCK_BYTES = 64 * 1024


def _linesBackwards(path: Path, blockSize: int = TAIL_BLOCK_BYTES) -> Iterator[bytes]:
    """The file's lines, last one first, reading a block at a time from the end
    and never more of the file than the caller asks for.

    A line may straddle a block boundary, so the first piece of each block is
    held back and joined to the block before it.
    """

    with open(path, 'rb') as file:
        file.seek(0, os.SEEK_END)
        position = file.tell()
        remainder = b''

        while position > 0:
            size = min(blockSize, position)
            position -= size
            file.seek(position)
            pieces = (file.read(size) + remainder).split(b'\n')
            remainder = pieces[0]
            for piece in reversed(pieces[1:]):
                yield piece

        yield remainder


class RunHistory(ABC):
    """An append-only record of every job's outcome, for people rather than
    the scheduler.
    """

    @abstractmethod
    def append(self, result: RunResult, runId: str) -> None:
        """Records one cycle's outcomes."""

    @abstractmethod
    def read(self, limit: int = 20, job: Optional[str] = None) -> List[Dict[str, Any]]:
        """The most recent records, newest first."""


class FileHistory(RunHistory):
    """History as JSON lines, one per job outcome, appended under a lock."""

    def __init__(self, historyFile: Union[str, Path]) -> None:
        self.historyFile = Path(historyFile)
        self._lockFile = self.historyFile.with_name(self.historyFile.name + '.lock')


    def append(self, result: RunResult, runId: str) -> None:

        lines = ''.join(json.dumps(record, default=str) + '\n' for record in historyRecords(result, runId))
        if not lines:
            return

        self.historyFile.parent.mkdir(parents=True, exist_ok=True)
        with exclusiveLock(self._lockFile), open(self.historyFile, 'a') as file:
            file.write(lines)
            file.flush()
            os.fsync(file.fileno())


    def read(self, limit: int = 20, job: Optional[str] = None) -> List[Dict[str, Any]]:
        """The newest records first, reading back from the end of the file until
        it has `limit` of them. History is append-only and never rotated, so
        the file only grows, and reading `limit` records costs the same
        whatever its size.
        """

        if not self.historyFile.exists():
            return []

        records: List[Dict[str, Any]] = []

        with exclusiveLock(self._lockFile):
            for line in _linesBackwards(self.historyFile):
                if not line.strip():
                    continue
                record = json.loads(line)
                if job is None or record['job'] == job:
                    records.append(record)
                    if len(records) >= limit:
                        break

        return records


DATABASE_HISTORY_SCHEMA = """CREATE TABLE bauta_history (
    run_id VARCHAR(36) NOT NULL,
    job VARCHAR(255) NOT NULL,
    status VARCHAR(16) NOT NULL,
    row_count NUMERIC(19),
    attempts INT,
    started_at DOUBLE PRECISION,
    finished_at DOUBLE PRECISION,
    error VARCHAR(2000),
    PRIMARY KEY (run_id, job)
    )"""

_HISTORY_COLUMNS = ['run_id', 'job', 'status', 'row_count', 'attempts', 'started_at', 'finished_at', 'error']


class DatabaseHistory(RunHistory):
    """History in a table, which must exist, shaped like
    DATABASE_HISTORY_SCHEMA. Times are epoch seconds, which every dialect
    stores alike.
    """

    def __init__(self, connectionSettings: ConnectionConfig, table: str = 'bauta_history') -> None:
        self.connectionSettings = connectionSettings
        self.table = table


    def append(self, result: RunResult, runId: str) -> None:

        rows = [(runId, outcome.job, outcome.status.value, outcome.rowCount, outcome.attempts, outcome.startedAt or None,
                 outcome.finishedAt or None, (outcome.error or None) and outcome.error[:ERROR_TEXT_LIMIT])
                for outcome in result.outcomes]
        if not rows:
            return

        with Database(connectionSettings=self.connectionSettings) as database:
            database.insert(table=self.table, data=rows, columns=_HISTORY_COLUMNS)


    def read(self, limit: int = 20, job: Optional[str] = None) -> List[Dict[str, Any]]:

        with Database(connectionSettings=self.connectionSettings) as database:
            # The table quoted as a load already writes it; the columns bare,
            # since the catalog's own spelling of them is what a server folds
            # an unquoted name to.
            query = 'SELECT {} FROM {}'.format(', '.join(_HISTORY_COLUMNS), database.statementName(self.table))
            parameters = None
            if job is not None:
                query += ' WHERE job = {}'.format(database.dialect.placeholders(1)[0])
                parameters = (job,)
            query += ' ORDER BY finished_at DESC, job'

            _, chunks = database.stream(query=query, chunkSize=limit, parameters=parameters)
            with chunks:
                rows = next(chunks, [])

        records = []
        for runId, name, status, rowCount, attempts, startedAt, finishedAt, error in rows:
            started, finished = float(startedAt or 0), float(finishedAt or 0)
            # NUMERIC comes back as Decimal on some drivers, which JSON can't write.
            records.append({'runId': runId, 'job': name, 'status': status, 'rowCount': int(rowCount or 0), 'attempts': int(attempts or 0),
                            'startedAt': _timestamp(started), 'finishedAt': _timestamp(finished),
                            'durationSeconds': round(max(0.0, finished - started), 3) if started and finished else 0.0, 'error': error})

        return records


def renderHistory(records: Sequence[Mapping[str, Any]]) -> str:

    lines = ['{:<20} {:<28} {:<10} {:>10} {:>9}  {}'.format('FINISHED', 'JOB', 'STATUS', 'ROWS', 'SECONDS', 'ERROR')]
    for record in records:
        lines.append('{:<20} {:<28} {:<10} {:>10} {:>9.1f}  {}'.format(
            (record['finishedAt'] or '-')[:19].replace('T', ' '), record['job'], record['status'], record['rowCount'] or 0,
            record['durationSeconds'] or 0.0, (record['error'] or '')[:80]))

    return '\n'.join(lines) + '\n'


def newRunId() -> str:

    return str(uuid.uuid4())


# Manifests --------------------------------------------------------------------

# One row per piece of a manifest's JSON, which is ASCII (json.dumps escapes
# the rest), so VARCHAR holds it on every dialect where a single large-text
# column would need a different type on each.
DATABASE_MANIFEST_SCHEMA = """CREATE TABLE bauta_manifest (
    run_id VARCHAR(36) NOT NULL,
    part INT NOT NULL,
    written_at DOUBLE PRECISION NOT NULL,
    content VARCHAR(2000) NOT NULL,
    PRIMARY KEY (run_id, part)
    )"""

MANIFEST_PART_LENGTH = 2000


class DatabaseManifests:
    """Sealed manifests in a table, which must exist, shaped like
    DATABASE_MANIFEST_SCHEMA. Each is stored as written, so its digest and
    signature verify as they would from a file. The table protects nothing
    by itself: whoever can write it can replace a manifest, and only a
    signature shows that one was.
    """

    def __init__(self, connectionSettings: ConnectionConfig, table: str = 'bauta_manifest') -> None:
        self.connectionSettings = connectionSettings
        self.table = table


    def write(self, manifest: Mapping[str, Any], runId: str) -> None:

        text = json.dumps(manifest, indent=2)
        writtenAt = datetime.datetime.now(datetime.timezone.utc).timestamp()
        rows = [(runId, number, writtenAt, text[start:start + MANIFEST_PART_LENGTH])
                for number, start in enumerate(range(0, len(text), MANIFEST_PART_LENGTH))]

        with Database(connectionSettings=self.connectionSettings) as database:
            # One chunk, so one transaction: a manifest is stored whole or not at all.
            database.insert(table=self.table, data=rows, chunkSize=len(rows), columns=['run_id', 'part', 'written_at', 'content'])


    def read(self, runId: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
        """A run's manifest, or the latest one's, with its run id. KeyError if
        there is none.
        """

        with Database(connectionSettings=self.connectionSettings) as database:
            placeholder = database.dialect.placeholders(1)[0]

            if runId is None:
                _, chunks = database.stream(query='SELECT run_id FROM {} ORDER BY written_at DESC'.format(database.statementName(self.table)),
                                            chunkSize=1)
                with chunks:
                    latest = next(chunks, [])
                if not latest:
                    raise KeyError('{} holds no manifest'.format(self.table))
                runId = latest[0][0]

            _, chunks = database.stream(query='SELECT content FROM {} WHERE run_id = {} ORDER BY part'.format(self.table, placeholder),
                                        chunkSize=100, parameters=(runId,))
            with chunks:
                text = ''.join(row[0] for chunk in chunks for row in chunk)

        if not text:
            raise KeyError('{} holds no manifest for run {}'.format(self.table, runId))

        return str(runId), json.loads(text)


# Notifications ----------------------------------------------------------------

def notificationPayload(result: RunResult) -> Dict[str, Any]:
    """The JSON posted to a webhook: a `text` line that Slack, Mattermost and
    Teams show as it is, plus the details for anything that parses it.
    """

    host = socket.gethostname()
    status = 'interrupted' if result.interrupted else ('succeeded' if result.succeeded else 'failed')
    headline = 'bauta on {}: {} -- {} completed, {} failed, {} skipped, {} row(s)'.format(
        host, status, len(result.completed), len(result.failed), len(result.skipped), result.rowCount)
    problems = ['- {} {}: {}'.format(outcome.job, outcome.status.value, (outcome.error or '')[:300]) for outcome in result.failed + result.skipped]

    return {
        'text': '\n'.join([headline] + problems),
        'status': status,
        'host': host,
        'summary': {'completed': len(result.completed), 'failed': len(result.failed), 'skipped': len(result.skipped), 'rows': result.rowCount},
        'jobs': [{'job': outcome.job, 'status': outcome.status.value, 'rowCount': outcome.rowCount,
                  'durationSeconds': round(outcome.durationSeconds, 3), 'error': outcome.error} for outcome in result.outcomes],
        }


def notify(url: str, result: RunResult, always: bool = False) -> bool:
    """Posts the cycle to a webhook if it didn't succeed, or always. Returns
    whether anything was sent.
    """

    if result.succeeded and not result.interrupted and not always:
        return False

    request = urllib.request.Request(url, data=json.dumps(notificationPayload(result), default=str).encode('utf-8'), method='POST',
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        response.read()

    return True

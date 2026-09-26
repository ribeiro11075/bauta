"""Run history and webhook notifications."""
import http.server
import json
import sqlite3
import threading

import pytest

from bauta.configuration import connectionConfig
from bauta.jobs.dependencyGraph import JobOutcome, JobStatus
from bauta.jobs.reporting import (DATABASE_HISTORY_SCHEMA, DATABASE_MANIFEST_SCHEMA, DatabaseHistory, DatabaseManifests, FileHistory, historyRecords,
                             notificationPayload, notify, renderHistory)
from bauta.jobs.runner import RunResult


def _result(*outcomes: JobOutcome, interrupted: bool = False) -> RunResult:
    return RunResult(outcomes=list(outcomes), interrupted=interrupted)


COMPLETED = JobOutcome(job='loadOrders', status=JobStatus.COMPLETED, rowCount=42, startedAt=1_790_000_000.0, finishedAt=1_790_000_012.5)
FAILED = JobOutcome(job='loadCustomers', status=JobStatus.FAILED, error='OperationalError: timeout', attempts=3,
                    startedAt=1_790_000_000.0, finishedAt=1_790_000_001.0)
SKIPPED = JobOutcome(job='loadInvoices', status=JobStatus.SKIPPED, error='predecessor(s) did not complete: loadCustomers')


def test_history_records_describe_each_outcome():
    completed, skipped = historyRecords(_result(COMPLETED, SKIPPED), 'run-1')

    assert completed == {'runId': 'run-1', 'job': 'loadOrders', 'status': 'completed', 'rowCount': 42, 'attempts': 1,
                         'startedAt': '2026-09-21T14:13:20+00:00', 'finishedAt': '2026-09-21T14:13:32+00:00', 'durationSeconds': 12.5,
                         'error': None}
    assert skipped['startedAt'] is None and skipped['error'].startswith('predecessor')


def test_file_history_appends_and_reads_newest_first(tmp_path):
    history = FileHistory(tmp_path / 'logs' / 'history.jsonl')

    history.append(_result(COMPLETED, FAILED), 'run-1')
    history.append(_result(COMPLETED), 'run-2')

    assert [(record['runId'], record['job']) for record in history.read()] == [
        ('run-2', 'loadOrders'), ('run-1', 'loadCustomers'), ('run-1', 'loadOrders')]
    assert [record['runId'] for record in history.read(job='loadOrders', limit=1)] == ['run-2']
    assert len((tmp_path / 'logs' / 'history.jsonl').read_text().splitlines()) == 3


def test_reading_history_that_was_never_written_is_empty(tmp_path):
    assert FileHistory(tmp_path / 'history.jsonl').read() == []


def test_database_history_round_trips(tmp_path):
    path = tmp_path / 'history.db'
    connection = sqlite3.connect(path)
    connection.execute(DATABASE_HISTORY_SCHEMA)
    connection.close()
    history = DatabaseHistory(connectionConfig(type='sqlite', path=str(path)))

    history.append(_result(FAILED), 'run-1')
    history.append(_result(COMPLETED, SKIPPED), 'run-2')

    records = history.read(limit=2)
    assert [(record['runId'], record['job']) for record in records] == [('run-2', 'loadOrders'), ('run-1', 'loadCustomers')]
    assert records[0]['durationSeconds'] == 12.5 and records[1]['error'] == 'OperationalError: timeout'
    assert [record['status'] for record in history.read(job='loadInvoices')] == ['skipped']


def test_history_renders_as_a_table():
    text = renderHistory(historyRecords(_result(FAILED), 'run-1'))

    assert text.splitlines()[1].split()[:5] == ['2026-09-21', '14:13:21', 'loadCustomers', 'failed', '0']
    assert 'OperationalError: timeout' in text


@pytest.fixture
def manifestTable(tmp_path):
    path = tmp_path / 'manifests.db'
    connection = sqlite3.connect(path)
    connection.execute(DATABASE_MANIFEST_SCHEMA)
    connection.close()

    return DatabaseManifests(connectionConfig(type='sqlite', path=str(path))), path


def test_a_manifest_longer_than_a_part_is_stored_in_order_and_read_back_whole(manifestTable):
    manifests, path = manifestTable
    manifest = {'jobs': [{'job': 'j{}'.format(index), 'columns': ['c'] * 40} for index in range(30)]}

    manifests.write(manifest, 'run-1')

    connection = sqlite3.connect(path)
    parts = [row[0] for row in connection.execute("SELECT part FROM bauta_manifest WHERE run_id = 'run-1' ORDER BY part")]
    connection.close()
    assert len(parts) > 1 and parts == list(range(len(parts)))
    assert manifests.read('run-1') == ('run-1', manifest)


def test_the_latest_manifest_is_read_unless_a_run_is_named(manifestTable):
    manifests, _ = manifestTable

    manifests.write({'jobs': ['older']}, 'run-1')
    manifests.write({'jobs': ['newer']}, 'run-2')

    assert manifests.read() == ('run-2', {'jobs': ['newer']})
    assert manifests.read('run-1') == ('run-1', {'jobs': ['older']})


def test_reading_a_manifest_that_is_not_there_is_a_key_error(manifestTable):
    manifests, _ = manifestTable

    with pytest.raises(KeyError, match='holds no manifest'):
        manifests.read()

    manifests.write({'jobs': []}, 'run-1')
    with pytest.raises(KeyError, match='no manifest for run run-9'):
        manifests.read('run-9')


class _Recorder(http.server.BaseHTTPRequestHandler):

    def _record(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        self.server.requests.append((self.command, self.path, self.headers['Content-Type'], body.decode('utf-8')))
        self.send_response(200)
        self.end_headers()

    do_POST = _record

    def log_message(self, *arguments):
        pass


@pytest.fixture
def webServer():
    server = http.server.HTTPServer(('127.0.0.1', 0), _Recorder)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield server, 'http://127.0.0.1:{}'.format(server.server_port)

    server.shutdown()
    server.server_close()


def test_a_notification_is_sent_only_when_a_cycle_does_not_succeed(webServer):
    server, url = webServer

    assert notify(url, _result(COMPLETED)) is False
    assert notify(url, _result(COMPLETED, FAILED, SKIPPED)) is True
    assert notify(url, _result(COMPLETED), always=True) is True
    assert notify(url, _result(COMPLETED, interrupted=True)) is True

    assert len(server.requests) == 3
    payload = json.loads(server.requests[0][3])
    assert payload['status'] == 'failed'
    assert payload['summary'] == {'completed': 1, 'failed': 1, 'skipped': 1, 'rows': 42}
    assert '- loadCustomers failed: OperationalError: timeout' in payload['text']
    assert json.loads(server.requests[2][3])['status'] == 'interrupted'


def test_the_notification_text_leads_with_the_outcome():
    payload = notificationPayload(_result(COMPLETED))

    assert payload['text'].startswith('bauta on ')
    assert ': succeeded -- 1 completed, 0 failed, 0 skipped, 42 row(s)' in payload['text']


# Reading history back from the end ---------------------------------------------

def _writeHistory(path, count, job='loadOrders'):
    import json

    with open(path, 'w') as file:
        for index in range(count):
            file.write(json.dumps({'runId': 'run{}'.format(index), 'job': job, 'status': 'completed',
                                   'rowCount': index, 'attempts': 1, 'startedAt': None, 'finishedAt': None,
                                   'durationSeconds': 0.0, 'error': None}) + '\n')


def test_history_reads_the_newest_records_first(tmp_path):
    from bauta.jobs.reporting import FileHistory

    path = tmp_path / 'history.jsonl'
    _writeHistory(path, 50)

    records = FileHistory(path).read(limit=3)

    assert [record['runId'] for record in records] == ['run49', 'run48', 'run47']


def test_history_reads_only_the_tail_of_a_long_file(tmp_path):
    """Regression test: reading twenty records used to parse every line ever
    written, so `bauta history` got slower every day a run recorded to it.
    """
    from bauta.jobs.reporting import TAIL_BLOCK_BYTES, FileHistory

    path = tmp_path / 'history.jsonl'
    _writeHistory(path, 20000)
    assert path.stat().st_size > TAIL_BLOCK_BYTES * 4

    records = FileHistory(path).read(limit=5)

    assert [record['runId'] for record in records] == ['run19999', 'run19998', 'run19997', 'run19996', 'run19995']


def test_history_finds_records_straddling_a_block_boundary(tmp_path):
    """A record is only whole once the block before it has been read too."""
    from bauta.jobs.reporting import _linesBackwards

    path = tmp_path / 'history.jsonl'
    _writeHistory(path, 2000)

    lines = [line for line in _linesBackwards(path, blockSize=64) if line.strip()]

    assert len(lines) == 2000
    assert all(line.startswith(b'{') and line.endswith(b'}') for line in lines)
    # Reading a block at a time must give the same records as one big read.
    assert lines == [line for line in _linesBackwards(path, blockSize=1024 * 1024) if line.strip()]


def test_history_filters_by_job_across_blocks(tmp_path):
    import json

    from bauta.jobs.reporting import FileHistory

    path = tmp_path / 'history.jsonl'
    with open(path, 'w') as file:
        for index in range(4000):
            job = 'wanted' if index % 500 == 0 else 'other'
            file.write(json.dumps({'runId': 'run{}'.format(index), 'job': job, 'status': 'completed', 'rowCount': 0,
                                   'attempts': 1, 'startedAt': None, 'finishedAt': None, 'durationSeconds': 0.0,
                                   'error': None}) + '\n')

    records = FileHistory(path).read(limit=3, job='wanted')

    assert [record['runId'] for record in records] == ['run3500', 'run3000', 'run2500']


def test_history_reads_a_file_whose_last_line_has_no_newline(tmp_path):
    from bauta.jobs.reporting import FileHistory

    path = tmp_path / 'history.jsonl'
    _writeHistory(path, 3)
    path.write_text(path.read_text().rstrip('\n'))

    assert [record['runId'] for record in FileHistory(path).read(limit=5)] == ['run2', 'run1', 'run0']


def test_history_of_an_empty_or_missing_file_is_empty(tmp_path):
    from bauta.jobs.reporting import FileHistory

    missing = tmp_path / 'nothing.jsonl'
    empty = tmp_path / 'empty.jsonl'
    empty.write_text('')

    assert FileHistory(missing).read() == []
    assert FileHistory(empty).read() == []


def test_history_round_trips_what_it_appended(tmp_path):
    """append() and read() have to agree about order and shape."""
    from bauta.jobs.dependencyGraph import JobOutcome, JobStatus
    from bauta.jobs.reporting import FileHistory
    from bauta.jobs.runner import RunResult

    path = tmp_path / 'history.jsonl'
    history = FileHistory(path)

    history.append(RunResult(outcomes=[JobOutcome(job='first', status=JobStatus.COMPLETED, rowCount=1)]), 'runA')
    history.append(RunResult(outcomes=[JobOutcome(job='second', status=JobStatus.FAILED, error='boom')]), 'runB')

    records = history.read(limit=10)

    assert [record['job'] for record in records] == ['second', 'first']
    assert records[0]['status'] == 'failed' and records[0]['error'] == 'boom'

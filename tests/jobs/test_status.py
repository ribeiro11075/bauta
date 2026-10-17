"""What is running now: the file each live cycle keeps, what readers make of
one whose process is gone, and the progress runDataJobs reports as it goes.
"""
import json
import sqlite3

import pytest

from bauta import Configuration, DataJobsFile, FileMemory, RunProgress, connectionConfig, runDataJobs
from bauta.cli import EXIT_SUCCESS, main
from bauta.jobs.dependencyGraph import JobOutcome, JobStatus
from bauta.jobs.status import RunStatus, readRunStatuses, renderRunStatuses, statusRecord

GONE_PID = 2 ** 22 + 12345


def _progress(**changes):
    fields = dict(runId='run-1', startedAt=1_800_000_000.0, jobs=['first', 'second', 'third'], running={'second': 1_800_000_010.0},
                  outcomes=[JobOutcome(job='first', status=JobStatus.FAILED, error='OperationalError: ' + 'x' * 5000, startedAt=1_800_000_000.0,
                                       finishedAt=1_800_000_005.0)])
    fields.update(changes)

    return RunProgress(**fields)


def test_a_record_says_where_each_job_stands_in_the_cycles_order():
    record = statusRecord(_progress(), pid=42, host='etl-1')

    assert record['runId'] == 'run-1' and record['pid'] == 42 and record['host'] == 'etl-1'
    assert [(job['job'], job['status']) for job in record['jobs']] == [('first', 'failed'), ('second', 'running'), ('third', 'waiting')]
    assert record['jobs'][1]['startedAt'] == '2027-01-15T08:00:10+00:00'
    assert len(record['jobs'][0]['error']) == 2000


def test_a_running_jobs_rows_so_far_are_in_its_record():
    record = statusRecord(_progress(rows={'second': 1200}))

    assert record['jobs'][1]['rowCount'] == 1200 and record['jobs'][2]['rowCount'] is None


def test_rows_are_counted_and_passed_on_at_most_once_an_interval(monkeypatch):
    from bauta.jobs import progress

    clock = [100.0]
    monkeypatch.setattr(progress.time, 'monotonic', lambda: clock[0])
    sent = []
    progress.setProgressSink(sent.append)
    try:
        progress.addRows(10)
        progress.addRows(10)
        clock[0] += progress.PROGRESS_INTERVAL_SECONDS
        progress.addRows(5)
    finally:
        progress.setProgressSink(None)

    assert sent == [10, 25]


def test_a_failing_sink_is_dropped_rather_than_failing_the_load():
    from bauta.jobs import progress

    def broken(rows):
        raise BrokenPipeError()

    progress.setProgressSink(broken)
    try:
        progress.addRows(1)
        progress.addRows(1)
    finally:
        progress.setProgressSink(None)


def test_a_cycles_file_is_written_whole_and_removed_as_it_finishes(tmp_path):
    status = RunStatus(tmp_path / 'memory.yaml.running')

    status(_progress())
    (written,) = (tmp_path / 'memory.yaml.running').iterdir()
    assert written.name == 'run-1.json'
    assert json.loads(written.read_text())['jobs'][1]['status'] == 'running'

    status(_progress(finished=True))
    assert list((tmp_path / 'memory.yaml.running').iterdir()) == []


def test_a_reader_says_which_runs_are_alive_gone_or_elsewhere(tmp_path):
    import os
    import socket

    directory = tmp_path / 'running'
    directory.mkdir()
    here = socket.gethostname()
    for runId, pid, host, started in [('alive', os.getpid(), here, '2026-10-16T02:00:00+00:00'), ('gone', GONE_PID, here, '2026-10-16T01:00:00+00:00'),
                                      ('elsewhere', 1, 'another-host', '2026-10-16T03:00:00+00:00')]:
        (directory / (runId + '.json')).write_text(json.dumps({'runId': runId, 'pid': pid, 'host': host, 'startedAt': started, 'jobs': []}))
    (directory / 'torn.json').write_text('{"runId": ')

    records = readRunStatuses(directory)

    assert [(record['runId'], record['alive']) for record in records] == [('elsewhere', None), ('alive', True), ('gone', False)]
    assert 'ended without finishing: process {} is gone'.format(GONE_PID) in renderRunStatuses(records)
    assert readRunStatuses(tmp_path / 'nothing-here') == []
    assert renderRunStatuses([]) == 'Nothing is running.\n'


@pytest.fixture
def database(tmp_path):
    connection = sqlite3.connect(str(tmp_path / 'demo.db'))
    connection.execute('CREATE TABLE src (id INT PRIMARY KEY, name TEXT)')
    connection.execute('CREATE TABLE tgt (id INT PRIMARY KEY, name TEXT)')
    connection.executemany('INSERT INTO src VALUES (?, ?)', [(index, 'name{}'.format(index)) for index in range(5)])
    connection.commit()
    connection.close()

    return tmp_path / 'demo.db'


def test_a_run_reports_each_change_then_its_end_under_the_run_id_it_returns(tmp_path, database):
    job = {'sourceConnection': 'demo', 'sourceQuery': 'SELECT id, name FROM src', 'targetConnection': 'demo', 'targetTableFinal': 'tgt',
           'insertStrategy': 'upsert', 'chunkSize': 2}
    jobsFile = Configuration.validateJobConfiguration({'workers': 1, 'jobs': {'first': job, 'second': dict(job, predecessors=['first'])}}, DataJobsFile)
    seen = []

    result = runDataJobs(jobsFile=jobsFile, connectionConfiguration={'demo': connectionConfig(type='sqlite', path=str(database))},
                         memory=FileMemory(memoryFile=tmp_path / 'memory.yaml'), logFile=tmp_path / 'run.log', onProgress=seen.append)

    assert {progress.runId for progress in seen} == {result.runId}
    states = []
    for progress in seen:
        state = (sorted(progress.running), [outcome.job for outcome in progress.outcomes], progress.finished)
        if not states or states[-1] != state:
            states.append(state)
    assert states == [(['first'], [], False), (['second'], ['first'], False), ([], ['first', 'second'], True)]
    # Each job's first chunk is reported as it lands, and any later report
    # -- a second or more on, which a slow machine can take -- has more.
    reported = {}
    for progress in seen:
        for job, rows in (progress.rows or {}).items():
            reported.setdefault(job, []).append(rows)
    assert sorted(reported) == ['first', 'second']
    assert all(counts[0] == 2 and counts == sorted(counts) and counts[-1] <= 5 for counts in reported.values())


def test_run_leaves_no_live_file_and_status_says_nothing_is_running(tmp_path, database, monkeypatch, capsys):
    configuration = tmp_path / 'configuration'
    configuration.mkdir()
    (configuration / 'connections.yaml').write_text('demo:\n  type: sqlite\n  path: ../demo.db\n')
    (configuration / 'jobs.yaml').write_text('history: history.jsonl\njobs:\n  copy:\n    sourceConnection: demo\n    sourceQuery: SELECT id, name FROM src\n'
                                             '    targetConnection: demo\n    targetTableFinal: tgt\n    insertStrategy: upsert\n')
    monkeypatch.chdir(tmp_path)

    assert main(['run', '--quiet']) == EXIT_SUCCESS
    assert list((configuration / 'memory.yaml.running').iterdir()) == []

    capsys.readouterr()
    assert main(['status', '--quiet']) == EXIT_SUCCESS
    assert capsys.readouterr().out == 'Nothing is running.\n'

    (configuration / 'memory.yaml.running' / 'left.json').write_text(json.dumps(
        {'runId': 'left', 'pid': GONE_PID, 'host': __import__('socket').gethostname(), 'startedAt': '2026-10-16T02:00:00+00:00',
         'jobs': [{'job': 'copy', 'status': 'running', 'startedAt': '2026-10-16T02:00:01+00:00', 'rowCount': None, 'error': None}]}))
    assert main(['status', '--quiet', '--format', 'json']) == EXIT_SUCCESS
    (record,) = json.loads(capsys.readouterr().out)
    assert record['runId'] == 'left' and record['alive'] is False

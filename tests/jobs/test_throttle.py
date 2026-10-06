"""Read limits and stage timings: what a job spends its time on, and how fast
it may read from a connection with maxRowsReadPerSecond.
"""
import sqlite3
import threading

import pytest

from bauta.configuration import Configuration, ConfigurationError, DataJobsFile, connectionConfig
from bauta.jobs import throttle
from bauta.jobs.memory import FileMemory
from bauta.jobs.pipeline import _executeDataJob
from bauta.jobs.runner import runDataJobs
from bauta.jobs.throttle import BURST_SECONDS, ReadLimit, StageTimes, readLimitFor, timedChunks
from tests.jobConfigs import dataJob, dataJobFields


class _Clock:
    """time.monotonic and time.sleep as one clock a test moves, so a limit's
    schedule can be checked exactly rather than by how long a test took.
    """

    def __init__(self, monkeypatch):
        self.now = 1000.0
        self.slept = []
        monkeypatch.setattr(throttle.time, 'monotonic', lambda: self.now)
        monkeypatch.setattr(throttle.time, 'sleep', self.sleep)

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_reading_within_the_burst_does_not_wait(monkeypatch):
    clock = _Clock(monkeypatch)
    limit = ReadLimit(1000)

    assert limit.take(int(1000 * BURST_SECONDS)) == 0.0
    assert clock.slept == []


def test_reading_past_the_limit_waits_for_what_the_rows_owe(monkeypatch):
    clock = _Clock(monkeypatch)
    limit = ReadLimit(1000)

    limit.take(1000)
    assert limit.take(500) == pytest.approx(0.5)
    assert limit.take(1000) == pytest.approx(1.0)
    assert clock.slept == [pytest.approx(0.5), pytest.approx(1.0)]


def test_a_pause_earns_no_more_than_the_burst(monkeypatch):
    """Without the cap, an hour idle would let the next hour's worth of rows
    through at once: exactly the flood the limit exists to stop.
    """
    clock = _Clock(monkeypatch)
    limit = ReadLimit(1000)

    limit.take(1000)
    clock.now += 3600

    assert limit.take(1000) == 0.0
    assert limit.take(1000) == pytest.approx(1.0)


def test_one_limit_is_shared_by_everyone_reading_through_it(monkeypatch):
    """Two partitions, or two jobs, on one connection have one budget between
    them, not one each.
    """
    _Clock(monkeypatch)
    limit = ReadLimit(1000)

    assert limit.take(1000) == 0.0
    # Another reader, the same limit: its rows wait behind the first's.
    assert limit.take(1000) == pytest.approx(1.0)


def test_timed_chunks_times_reading_apart_from_waiting_and_closes_the_stream(monkeypatch):
    clock = _Clock(monkeypatch)
    times = StageTimes()
    closed = []

    class Stream:
        def __init__(self):
            self.chunks = iter([[(1,)] * 1000, [(2,)] * 1000])

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.chunks)

        def close(self):
            closed.append(True)

    chunks = list(timedChunks(Stream(), times, ReadLimit(1000)))

    assert [len(chunk) for chunk in chunks] == [1000, 1000]
    assert clock.slept == [pytest.approx(1.0)]
    stages = times.asDict()
    assert stages['throttled'] == pytest.approx(1.0) and stages['read'] < 0.5
    assert closed == [True]


def test_a_connection_without_a_limit_has_none():
    throttle.setSharedReadLimits({})

    assert readLimitFor('source', connectionConfig(type='sqlite', path='x.db')) is None


def test_a_process_gets_one_limit_per_connection():
    throttle.setSharedReadLimits({})
    settings = connectionConfig(type='sqlite', path='x.db', maxRowsReadPerSecond=500)

    assert readLimitFor('source', settings) is readLimitFor('source', settings)
    assert readLimitFor('source', settings).rowsPerSecond == 500


@pytest.mark.parametrize('value', [0, -1])
def test_a_limit_must_be_above_zero(value):
    with pytest.raises(ConfigurationError, match='greater than 0'):
        connectionConfig(type='sqlite', path='x.db', maxRowsReadPerSecond=value)


def test_a_lake_takes_no_read_limit(tmp_path):
    """Files and Iceberg are targets only: nothing reads from them."""
    with pytest.raises((ValueError, ConfigurationError)):
        connectionConfig(type='files', root=str(tmp_path), format='parquet', maxRowsReadPerSecond=100)


@pytest.fixture
def sqliteJob(tmp_path):
    path = tmp_path / 'demo.db'
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE src (id INT PRIMARY KEY, name TEXT)')
    connection.execute('CREATE TABLE tgt (id INT PRIMARY KEY, name TEXT)')
    connection.executemany('INSERT INTO src VALUES (?, ?)', [(index, 'n{}'.format(index)) for index in range(50)])
    connection.commit()
    connection.close()

    return {'db': connectionConfig(type='sqlite', path=str(path))}


def test_a_completed_job_says_where_its_time_went(sqliteJob, monkeypatch):
    import time

    from bauta.masking.core import BoundMasking

    # Masking 50 values takes too little time to tell from none; each chunk
    # is made to take a known while, which must land in `mask` and nowhere else.
    apply = BoundMasking.apply
    monkeypatch.setattr(BoundMasking, 'apply', lambda self, *args, **kwargs: (time.sleep(0.02), apply(self, *args, **kwargs))[1])
    job = dataJob(sourceConnection='db', targetConnection='db', sourceQuery='SELECT id, name FROM src', targetTableFinal='tgt', chunkSize=7,
                  masking={'key': 'a-throttle-test-masking-key', 'columns': {'id': 'keep', 'name': 'hash'}})

    outcome = _executeDataJob('copy', job, sqliteJob)

    assert outcome.rowCount == 50
    assert set(outcome.stages) == {'read', 'mask', 'write', 'throttled'}
    # 50 rows in chunks of 7 is 8 chunks.
    assert outcome.stages['mask'] >= 8 * 0.02
    assert outcome.stages['read'] < 8 * 0.02 and outcome.stages['write'] < 8 * 0.02
    assert outcome.stages['throttled'] == 0.0


def test_a_job_with_partitions_adds_its_slices_times_together(sqliteJob, monkeypatch):
    import time

    from bauta.masking.core import BoundMasking

    # Each chunk of each slice made to mask for a known while, as reading and
    # writing 50 rows take too little to tell from none.
    apply = BoundMasking.apply
    monkeypatch.setattr(BoundMasking, 'apply', lambda self, *args, **kwargs: (time.sleep(0.02), apply(self, *args, **kwargs))[1])
    job = dataJob(sourceConnection='db', targetConnection='db', sourceQuery='SELECT id, name FROM src', targetTableFinal='tgt', chunkSize=7,
                  insertStrategy='upsert', partitions={'column': 'id', 'count': 3},
                  masking={'key': 'a-throttle-test-masking-key', 'columns': {'id': 'keep', 'name': 'hash'}})

    outcome = _executeDataJob('copy', job, sqliteJob)

    # Three slices of about 17 rows in chunks of 7: at least 3 chunks each,
    # each slice's masking added to the job's.
    assert outcome.rowCount == 50 and outcome.stages['mask'] >= 9 * 0.02


def test_a_job_reading_from_a_limited_connection_waits_for_it(sqliteJob):
    throttle.setSharedReadLimits({})
    settings = {'db': sqliteJob['db'].model_copy(update={'maxRowsReadPerSecond': 100.0})}
    # 100 rows at 100 a second fit within the burst; the next 50 owe half a second.
    job = dataJob(sourceConnection='db', targetConnection='db',
                  sourceQuery='SELECT id, name FROM src UNION ALL SELECT id + 100, name FROM src UNION ALL SELECT id + 200, name FROM src',
                  targetTableFinal='tgt', chunkSize=25, unmasked=True)

    outcome = _executeDataJob('copy', job, settings)

    assert outcome.rowCount == 150
    assert outcome.stages['throttled'] == pytest.approx(0.5, abs=0.2)


def test_a_runs_jobs_share_their_sources_read_limit(tmp_path):
    """Each job runs in a process of its own. A limit each process kept for
    itself would let two jobs read twice what the connection was given.

    Each job alone fits its rows within the burst and waits for nothing; only
    a limit the two share makes either wait.
    """
    source = tmp_path / 'source.db'
    connection = sqlite3.connect(source)
    connection.execute('CREATE TABLE src (id INT PRIMARY KEY, name TEXT)')
    connection.executemany('INSERT INTO src VALUES (?, ?)', [(index, 'n') for index in range(400)])
    connection.commit()
    connection.close()
    connections = {'source': connectionConfig(type='sqlite', path=str(source), maxRowsReadPerSecond=400)}
    for name in ('a', 'b'):
        target = tmp_path / '{}.db'.format(name)
        connection = sqlite3.connect(target)
        connection.execute('CREATE TABLE tgt (id INT PRIMARY KEY, name TEXT)')
        connection.close()
        connections[name] = connectionConfig(type='sqlite', path=str(target))

    jobs = {name: dataJobFields(sourceConnection='source', targetConnection=name, sourceQuery='SELECT id, name FROM src', targetTableFinal='tgt',
                                chunkSize=100, unmasked=True) for name in ('a', 'b')}
    result = runDataJobs(jobsFile=Configuration.validateJobConfiguration({'workers': 2, 'jobs': jobs}, DataJobsFile),
                         connectionConfiguration=connections, memory=FileMemory(tmp_path / 'memory.yaml'))

    assert result.succeeded
    assert sum(outcome.rowCount for outcome in result.outcomes) == 800
    # 800 rows at 400 a second, less the burst, owe a second. The jobs may
    # wait at the same moment, so their waits add up to between one second
    # and two; kept apart, each would fit in the burst and wait for nothing.
    assert 0.7 <= sum(outcome.stages['throttled'] for outcome in result.outcomes) <= 2.4


def test_stage_times_can_be_added_to_from_many_threads():
    times = StageTimes()

    threads = [threading.Thread(target=lambda: [times.add('mask', 0.001) for _ in range(1000)]) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert times.asDict()['mask'] == pytest.approx(8.0)


def test_a_nan_sqlite_would_store_as_null_fails_the_job_once_naming_the_column(sqliteJob):
    """SQLite stores NaN as NULL without a word. The job fails instead, at
    once rather than after its retries, since the same rows fail the same way.
    """
    from bauta.jobs.dependencyGraph import JobStatus
    from bauta.jobs.pipeline import _executeWithRetries

    connection = sqlite3.connect(sqliteJob['db'].path)
    connection.execute('CREATE TABLE readings_copy (id INT PRIMARY KEY, v REAL)')
    connection.commit()
    connection.close()
    # SQLite can't hold a NaN to read either: the source query spells it,
    # and a transform makes it the float a PostgreSQL source would return.
    job = dataJob(sourceConnection='db', targetConnection='db', sourceQuery="SELECT 1 AS id, '1.5' AS v UNION ALL SELECT 2, 'nan'",
                  targetTableFinal='readings_copy', sourceQueryColumnTransforms={'v': ['builtins:float']}, unmasked=True, retries=3,
                  retryDelaySeconds=0)

    outcome = _executeWithRetries(job, 'copy', lambda: _executeDataJob('copy', job, sqliteJob))

    assert outcome.status == JobStatus.FAILED and outcome.attempts == 1
    assert 'readings_copy column v was sent NaN, which SQLite cannot hold' in outcome.error

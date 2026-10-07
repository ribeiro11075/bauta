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

    # On a clock that moves only while a chunk is masked, a second each, so
    # what is asked is where the time is put, not how fast this machine is:
    # with real time, a slow runner took 0.3s to write 50 rows to SQLite.
    # A clock for each thread, since the next chunk is masked while the last
    # is written, and each stage is timed on its own thread.
    clocks = threading.local()
    chunks = []
    apply = BoundMasking.apply

    def maskingForASecond(self, *args, **kwargs):
        clocks.now = getattr(clocks, 'now', 0.0) + 1.0
        chunks.append(1)
        return apply(self, *args, **kwargs)

    monkeypatch.setattr(time, 'perf_counter', lambda: getattr(clocks, 'now', 0.0))
    monkeypatch.setattr(BoundMasking, 'apply', maskingForASecond)
    job = dataJob(sourceConnection='db', targetConnection='db', sourceQuery='SELECT id, name FROM src', targetTableFinal='tgt', chunkSize=7,
                  masking={'key': 'a-throttle-test-masking-key', 'columns': {'id': 'keep', 'name': 'hash'}})

    outcome = _executeDataJob('copy', job, sqliteJob)

    assert outcome.rowCount == 50
    assert set(outcome.stages) == {'read', 'mask', 'write', 'throttled'}
    # 50 rows in chunks of 7 is 8 chunks.
    assert len(chunks) >= 8
    assert outcome.stages['mask'] == pytest.approx(len(chunks))
    assert outcome.stages['read'] == 0.0 and outcome.stages['write'] == 0.0
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


def test_a_job_reading_from_a_limited_connection_waits_for_it(sqliteJob, monkeypatch):
    # On the test's clock, which moves only while the limit sleeps: on a real
    # one, the limit's budget refills while a slow runner writes, and the job
    # waited 0.2s rather than 0.5s, as it should.
    clock = _Clock(monkeypatch)
    throttle.setSharedReadLimits({})
    settings = {'db': sqliteJob['db'].model_copy(update={'maxRowsReadPerSecond': 100.0})}
    # 100 rows at 100 a second fit within the burst; the next 50 owe half a second.
    job = dataJob(sourceConnection='db', targetConnection='db',
                  sourceQuery='SELECT id, name FROM src UNION ALL SELECT id + 100, name FROM src UNION ALL SELECT id + 200, name FROM src',
                  targetTableFinal='tgt', chunkSize=25, unmasked=True)

    outcome = _executeDataJob('copy', job, settings)

    assert outcome.rowCount == 150
    assert sum(clock.slept) == pytest.approx(0.5)
    assert outcome.stages['throttled'] == pytest.approx(0.5)


def _readThrough(shared, start, rows, results):
    """One process reading `rows` through the shared limit once `start` is
    set, a hundred at a time, putting on `results` when it began and when it
    had read them all, by the system's monotonic clock, which every process
    shares.
    """
    import time

    start.wait()
    limit = ReadLimit(400, shared)
    began = time.monotonic()
    for _ in range(rows // 100):
        limit.take(100)
    results.put((began, time.monotonic()))


def test_processes_reading_through_one_shared_limit_share_its_budget():
    """Each job runs in a process of its own. A limit each kept for itself
    would let two read twice what the connection was given.

    What the limit promises is the rate, whenever each process happens to be
    scheduled: 800 rows at 400 a second, a second's worth allowed at once,
    can't all be read sooner than a second after reading began. Each limited
    alone would read its 400 at once. (How long each waited depends on when
    each woke, which a slow machine spreads apart, and proves nothing.)
    """
    from bauta.jobs.workers import PROCESS_CONTEXT

    shared = PROCESS_CONTEXT.Value('d', float('-inf'))
    start, results = PROCESS_CONTEXT.Event(), PROCESS_CONTEXT.Queue()
    processes = [PROCESS_CONTEXT.Process(target=_readThrough, args=(shared, start, 400, results)) for _ in range(2)]
    for process in processes:
        process.start()
    start.set()
    spans = [results.get(timeout=60) for _ in processes]
    for process in processes:
        process.join(60)

    first = min(began for began, _ in spans)
    last = max(finished for _, finished in spans)
    assert last - first >= 0.95


def test_a_run_hands_every_job_the_same_shared_limit_for_its_source(tmp_path, monkeypatch):
    """The limit is shared only if each job's process is handed the run's
    one value for the connection, not a limit of its own.
    """
    from bauta.jobs import runner, workers

    handed = []
    original = workers._JobProcess.__init__

    def recording(self, *args, **kwargs):
        handed.append(args[7] if len(args) > 7 else kwargs.get('readLimits'))
        original(self, *args, **kwargs)

    monkeypatch.setattr(workers._JobProcess, '__init__', recording)
    source = tmp_path / 'source.db'
    connection = sqlite3.connect(source)
    connection.execute('CREATE TABLE src (id INT PRIMARY KEY, name TEXT)')
    connection.execute("INSERT INTO src VALUES (1, 'n')")
    connection.commit()
    connection.close()
    connections = {'source': connectionConfig(type='sqlite', path=str(source), maxRowsReadPerSecond=400),
                   'other': connectionConfig(type='sqlite', path=str(source))}
    for name in ('a', 'b'):
        target = tmp_path / '{}.db'.format(name)
        connection = sqlite3.connect(target)
        connection.execute('CREATE TABLE tgt (id INT PRIMARY KEY, name TEXT)')
        connection.close()
        connections[name] = connectionConfig(type='sqlite', path=str(target))
    jobs = {name: dataJobFields(sourceConnection='source', targetConnection=name, sourceQuery='SELECT id, name FROM src', targetTableFinal='tgt',
                                unmasked=True) for name in ('a', 'b')}

    result = runner.runDataJobs(jobsFile=Configuration.validateJobConfiguration({'workers': 2, 'jobs': jobs}, DataJobsFile),
                                connectionConfiguration=connections, memory=FileMemory(tmp_path / 'memory.yaml'))

    assert result.succeeded and sum(outcome.rowCount for outcome in result.outcomes) == 2
    [first, second] = handed
    # One value for the limited connection, the same object for both jobs; none for the other.
    assert set(first) == {'source'} and first['source'] is second['source']


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

import datetime
import decimal

import pytest

from bauta.jobs.memory import FileMemory


def test_missing_memory_file_reads_as_empty(tmp_path):
    """Regression test: a fresh checkout ships no memory file at all -- this used
    to require the file to already exist and crash with FileNotFoundError.
    """
    memory = FileMemory(memoryFile=tmp_path / 'does_not_exist.yaml')

    assert memory.read() == {}


def test_record_run_persists_and_reloads(tmp_path):
    memoryPath = tmp_path / 'memory.yaml'

    first = FileMemory(memoryFile=memoryPath)
    first.recordRun(job='job1')

    second = FileMemory(memoryFile=memoryPath)

    assert 'job1' in second.read()


def test_record_run_only_touches_its_own_job(tmp_path):
    memory = FileMemory(memoryFile=tmp_path / 'memory.yaml')
    memory.recordRun(job='job1')
    firstTimestamp = memory.read()['job1']

    memory.recordRun(job='job2')

    updated = memory.read()
    assert updated['job1'] == firstTimestamp
    assert 'job2' in updated


def test_empty_memory_file_reads_as_empty_dict(tmp_path):
    memoryPath = tmp_path / 'memory.yaml'
    memoryPath.write_text('')

    memory = FileMemory(memoryFile=memoryPath)

    assert memory.read() == {}


def test_record_run_does_not_clobber_a_concurrent_workers_update(tmp_path):
    """Regression test for a lost-update race: each worker process holds its own
    FileMemory instance. Here, both are constructed against the same (still-empty)
    file before either has written -- simulating two worker processes starting up
    around the same time. Without re-reading the file inside recordRun, the second
    writer's stale empty snapshot would silently overwrite the first writer's entry.
    """
    memoryPath = tmp_path / 'memory.yaml'

    workerA = FileMemory(memoryFile=memoryPath)
    workerB = FileMemory(memoryFile=memoryPath)

    workerB.recordRun(job='jobB')
    workerA.recordRun(job='jobA')

    finalState = FileMemory(memoryFile=memoryPath)
    assert {'jobA', 'jobB'} <= finalState.read().keys()


def test_watermarks_and_run_times_are_stored_independently(tmp_path):
    """They're written by two separate locked read-modify-writes against one
    file -- neither may clobber the other's section.
    """
    memory = FileMemory(memoryFile=tmp_path / 'memory.yaml')

    memory.recordWatermark(job='job1', value='2026-09-15 10:00:00')
    memory.recordRun(job='job1')
    memory.recordWatermark(job='job2', value=4711)

    assert set(memory.read()) == {'job1'}
    assert memory.readWatermarks() == {'job1': '2026-09-15 10:00:00', 'job2': 4711}


def test_the_first_write_creates_the_memory_files_directory(tmp_path):
    memory = FileMemory(memoryFile=tmp_path / 'transaction' / 'memory.yaml')

    memory.recordRun('loadOrders')

    assert 'loadOrders' in memory.read()


def test_a_missing_memory_file_reads_as_no_watermarks(tmp_path):
    assert FileMemory(memoryFile=tmp_path / 'nope.yaml').readWatermarks() == {}


@pytest.mark.parametrize('value', [
    4711,
    3.5,
    'abc',
    datetime.datetime(2026, 9, 15, 10, 30, 0),
    datetime.date(2026, 9, 15),
    ])
def test_watermark_values_round_trip_through_yaml(tmp_path, value):
    """Whatever goes in has to come back as the same type -- it gets bound back
    into a predicate compared against the source column it came from.
    """
    memory = FileMemory(memoryFile=tmp_path / 'memory.yaml')

    memory.recordWatermark(job='job1', value=value)

    assert memory.readWatermarks()['job1'] == value


def test_a_decimal_watermark_is_stored_as_a_number_not_a_python_object(tmp_path):
    """Oracle returns every NUMBER as a Decimal, which PyYAML can only write as a
    python/object tag that FullLoader then refuses to load -- so an Oracle id
    watermark would fail on the way back in.
    """
    memory = FileMemory(memoryFile=tmp_path / 'memory.yaml')

    memory.recordWatermark(job='job1', value=decimal.Decimal('4711'))
    memory.recordWatermark(job='job2', value=decimal.Decimal('3.5'))

    assert 'python/object' not in (tmp_path / 'memory.yaml').read_text()
    assert memory.readWatermarks() == {'job1': 4711, 'job2': 3.5}


def test_an_integral_decimal_keeps_full_precision_beyond_floats_range(tmp_path):
    """int, not float: an id past 2**53 would lose its last digits as a float."""
    memory = FileMemory(memoryFile=tmp_path / 'memory.yaml')
    bigId = 9007199254740993

    memory.recordWatermark(job='job1', value=decimal.Decimal(bigId))

    assert memory.readWatermarks()['job1'] == bigId


def test_a_legacy_flat_memory_file_is_read_as_last_run_times(tmp_path):
    """A memory file written before watermarks existed is a bare job -> timestamp
    mapping. Reading it as anything else would drop every refresh window on
    upgrade and fire every job at once.
    """
    memoryPath = tmp_path / 'memory.yaml'
    memoryPath.write_text('job1: 1726400000.0\njob2: 1726400001.0\n')
    memory = FileMemory(memoryFile=memoryPath)

    assert memory.read() == {'job1': 1726400000.0, 'job2': 1726400001.0}
    assert memory.readWatermarks() == {}

    memory.recordWatermark(job='job1', value=7)

    assert memory.read() == {'job1': 1726400000.0, 'job2': 1726400001.0}
    assert memory.readWatermarks() == {'job1': 7}


def test_a_write_replaces_the_file_rather_than_rewriting_it_in_place(tmp_path, monkeypatch):
    """A process killed mid-write used to leave a truncated file that every
    later run failed to parse. A failed write now leaves the old file intact.
    """
    import yaml

    memoryFile = tmp_path / 'memory.yaml'
    memory = FileMemory(memoryFile=memoryFile)
    memory.recordRun('first')
    before = memoryFile.read_text()

    def dieMidWrite(document, stream, **arguments):
        stream.write('lastRun:\n  fir')
        raise KeyboardInterrupt

    monkeypatch.setattr(yaml, 'dump', dieMidWrite)

    with pytest.raises(KeyboardInterrupt):
        memory.recordRun('second')

    assert memoryFile.read_text() == before


def test_the_run_lock_is_exclusive_and_released_afterwards(tmp_path):
    from bauta.jobs.memory import RunInProgressError, exclusiveRun

    lockFile = tmp_path / 'memory.yaml.run.lock'

    with exclusiveRun(lockFile):
        with pytest.raises(RunInProgressError):
            with exclusiveRun(lockFile):
                pass

    with exclusiveRun(lockFile):
        pass


def test_the_database_memory_schema_uses_a_portable_float_type():
    """DOUBLE alone is MySQL's spelling; PostgreSQL, Oracle and SQL Server reject it."""
    from bauta.jobs.memory import DATABASE_MEMORY_SCHEMA

    assert 'DOUBLE PRECISION' in DATABASE_MEMORY_SCHEMA


def test_file_memory_records_and_forgets_key_fingerprints(tmp_path):
    memory = FileMemory(memoryFile=tmp_path / 'memory.yaml')
    memory.recordRun('maskCustomers')

    memory.recordKeyFingerprint('maskCustomers', 'abc123')
    assert memory.readKeyFingerprints() == {'maskCustomers': 'abc123'}
    assert memory.read().keys() == {'maskCustomers'}

    memory.recordKeyFingerprint('maskCustomers', None)
    assert memory.readKeyFingerprints() == {}


def test_database_memory_keeps_key_fingerprints_out_of_watermarks_and_runs(tmp_path):
    import sqlite3

    from bauta.configuration import connectionConfig
    from bauta.jobs.memory import DATABASE_MEMORY_SCHEMA, DatabaseMemory

    path = tmp_path / 'memory.db'
    connection = sqlite3.connect(path)
    connection.execute(DATABASE_MEMORY_SCHEMA)
    connection.close()
    with DatabaseMemory(connectionConfig(type='sqlite', path=str(path))) as memory:
        memory.recordWatermark('maskCustomers', 7)
        memory.recordKeyFingerprint('maskCustomers', 'abc123')

        assert memory.readKeyFingerprints() == {'maskCustomers': 'abc123'}
        assert memory.readWatermarks() == {'maskCustomers': 7}
        assert memory.read() == {}

        memory.recordKeyFingerprint('maskCustomers', None)
        assert memory.readKeyFingerprints() == {}


def test_a_backend_must_keep_every_kind_of_state():
    """Watermarks and key fingerprints aren't optional: a backend that dropped
    fingerprints would quietly turn off the check that stops two masking keys
    mixing in one target, so one that leaves any method out can't be made.
    """
    from bauta.jobs.memory import MemoryBackend

    class _RunsOnly(MemoryBackend):
        def read(self):
            return {}

        def recordRun(self, job):
            return None

    with pytest.raises(TypeError, match='abstract'):
        _RunsOnly()


@pytest.mark.parametrize('value', [b'\x00\x00\x07\xd1', decimal.Decimal('12.50'), datetime.time(10, 30, 5), datetime.datetime(2026, 9, 17, 8, 0),
                                   datetime.date(2026, 9, 17), 4711, 3.5, 'abc'])
def test_database_memory_gives_a_watermark_back_as_the_type_it_was(tmp_path, value):
    """The next run binds it against the source column, which a string doesn't
    compare with the way a SQL Server rowversion's bytes do.
    """
    import sqlite3

    from bauta.configuration import connectionConfig
    from bauta.jobs.memory import DATABASE_MEMORY_SCHEMA, DatabaseMemory

    path = tmp_path / 'memory.db'
    connection = sqlite3.connect(path)
    connection.execute(DATABASE_MEMORY_SCHEMA)
    connection.close()
    with DatabaseMemory(connectionConfig(type='sqlite', path=str(path))) as memory:
        memory.recordWatermark('job1', value)
        read = memory.readWatermarks()['job1']

        assert type(read) is type(value) and read == value


def test_a_decimal_watermark_round_trips_exactly(tmp_path):
    """Written as a float, 12345678901234567.1 came back as 1.2345678901234568e+16
    -- above the highest row read, so the rows between were skipped for good.
    """
    import decimal

    memory = FileMemory(memoryFile=tmp_path / 'memory.yaml')
    value = decimal.Decimal('12345678901234567.1')
    memory.recordWatermark('loadEvents', value)

    stored = FileMemory(memoryFile=tmp_path / 'memory.yaml').readWatermarks()['loadEvents']

    assert stored == value and isinstance(stored, decimal.Decimal)
    assert "!decimal '12345678901234567.1'" in (tmp_path / 'memory.yaml').read_text()


def test_a_watermark_written_as_a_float_by_an_older_version_still_reads(tmp_path):
    memoryFile = tmp_path / 'memory.yaml'
    memoryFile.write_text('lastRun: {}\nmaskingKeys: {}\nwatermarks:\n  loadEvents: 1.2345678901234568e+16\n')

    assert FileMemory(memoryFile=memoryFile).readWatermarks() == {'loadEvents': 1.2345678901234568e+16}


# DatabaseMemory's held connection ----------------------------------------------

def _databaseMemory(tmp_path, name='memory.db'):
    import sqlite3

    from bauta.configuration import connectionConfig
    from bauta.jobs.memory import DATABASE_MEMORY_SCHEMA, DatabaseMemory

    path = tmp_path / name
    connection = sqlite3.connect(path)
    connection.execute(DATABASE_MEMORY_SCHEMA)
    connection.close()

    return DatabaseMemory(connectionConfig(type='sqlite', path=str(path)))


def test_database_memory_opens_one_connection_and_keeps_it(tmp_path):
    """A completed masked incremental job reads a watermark and records three
    things; each of those used to open its own connection -- and its own
    passwordCommand subprocess wherever one supplies a cloud IAM token.
    """
    with _databaseMemory(tmp_path) as memory:
        memory.recordRun('loadOrders')
        first = memory._database
        memory.recordWatermark('loadOrders', 7)
        memory.readWatermarks()
        memory.read()

        assert first is not None
        assert memory._database is first


def test_database_memory_opens_nothing_until_it_is_used(tmp_path):
    with _databaseMemory(tmp_path) as memory:
        assert memory._database is None


def test_database_memory_does_not_carry_its_connection_across_a_pickle(tmp_path):
    """Every job's process gets a copy of the backend. A connection can't be
    pickled, and must not be shared if it could.
    """
    import pickle

    with _databaseMemory(tmp_path) as memory:
        memory.recordRun('loadOrders')
        assert memory._database is not None

        with pickle.loads(pickle.dumps(memory)) as copy:
            assert copy._database is None and copy._pid is None
            assert copy.read() == memory.read()


def test_database_memory_reopens_a_connection_that_died(tmp_path):
    """Held rather than reopened per call, the backend now meets a server
    restart, an idle timeout or an expired token, which reopening used to hide.
    """
    with _databaseMemory(tmp_path) as memory:
        memory.recordWatermark('loadOrders', 7)
        stale = memory._database
        stale.connection.close()          # as a server hanging up would leave it

        assert memory.readWatermarks() == {'loadOrders': 7}
        assert memory._database is not stale


def test_database_memory_does_not_retry_a_statement_that_was_simply_wrong(tmp_path):
    """Only a reused connection is retried. A failure on one opened in the same
    call is the statement's fault, and running it twice would hide that.
    """
    import sqlite3

    from bauta.configuration import connectionConfig
    from bauta.jobs.memory import DatabaseMemory

    path = tmp_path / 'no-such-table.db'
    sqlite3.connect(path).close()
    with DatabaseMemory(connectionConfig(type='sqlite', path=str(path))) as memory:
        with pytest.raises(Exception):
            memory.read()


def test_database_memory_close_is_idempotent_and_reopens_on_next_use(tmp_path):
    with _databaseMemory(tmp_path) as memory:
        memory.recordRun('loadOrders')
        memory.close()
        memory.close()

        assert memory._database is None
        assert 'loadOrders' in memory.read()
        assert memory._database is not None


def test_database_memory_reads_a_table_whose_name_needs_quoting(tmp_path):
    """The table goes into every statement the way a load already writes it, so
    a name that has to be quoted is read as well as written.
    """
    import sqlite3

    from bauta.configuration import connectionConfig
    from bauta.jobs.memory import DATABASE_MEMORY_SCHEMA, DatabaseMemory

    path = tmp_path / 'reserved.db'
    connection = sqlite3.connect(path)
    connection.execute(DATABASE_MEMORY_SCHEMA.replace('bauta_memory', '"order"'))
    connection.close()
    with DatabaseMemory(connectionConfig(type='sqlite', path=str(path)), table='order') as memory:
        memory.recordWatermark('loadOrders', 7)
        memory.recordKeyFingerprint('maskCustomers', 'abc123')
        memory.recordRun('loadOrders')

        assert memory.readWatermarks() == {'loadOrders': 7}
        assert memory.readKeyFingerprints() == {'maskCustomers': 'abc123'}
        assert 'loadOrders' in memory.read()

"""What every database must do the same way, run against all six: reading a
table's shape, the loads, the swap, streaming, run state in a table, and whole
jobs through runDataJobs. Everything else in this suite proves the SQL each
dialect builds; this proves it runs.

A check written here runs on every database, so none can be left without it.
What only one database does is tested in its own file: test_integration_oracle,
_postgresql, _mssql and _sqlite.

SQLite needs no server, so its runs are part of the default `pytest`; the five
servers' are marked `integration` (see docker-compose.yml). A server that isn't
reachable skips its runs with the reason.
"""
import pytest

from bauta.configuration import Configuration, DataJobsFile
from bauta.database import Database
from bauta.jobs.memory import DatabaseMemory, FileMemory
from bauta.jobs.runner import runDataJobs
from tests.integration.servers import SERVERS

DATABASES = [pytest.param('sqlite')] + [pytest.param(name, marks=pytest.mark.integration) for name in sorted(SERVERS)]


@pytest.fixture(params=DATABASES)
def databaseName(request):

    return request.param


def _folded(databaseName, names):
    """`names` as this database stores a name written without quotes: Oracle
    folds them to upper case, which is Oracle's behaviour being checked, not
    something the package decides.
    """

    return [name.upper() for name in names] if databaseName == 'oracle' else names


def _fromNothing(databaseName):
    """What a SELECT of literals alone needs after it: Oracle has no FROM-less
    SELECT, and reads them from `dual`.
    """

    return ' FROM dual' if databaseName == 'oracle' else ''


def _createLike(database, table, suffix):

    other = table + suffix
    database.alter('CREATE TABLE {} (id INT PRIMARY KEY, name VARCHAR(50), amount INT)'.format(other))

    return other


def _runOneJob(settings, tmp_path, memory, **job):

    raw = {'workers': 1, 'jobs': {'job1': dict({'active': True, 'sourceDatabase': 'db', 'targetDatabase': 'db', 'insertStrategy': 'upsert',
                                                'chunkSize': 100}, **job)}}

    return runDataJobs(jobsFile=Configuration.validateJobConfiguration(raw, DataJobsFile), databaseConfiguration={'db': settings},
                       logFile=tmp_path / 'runner.log', memory=memory, runForever=False)


# Reading a table's shape -----------------------------------------------------

def test_schema_introspection_against_a_real_table(liveDatabase, peopleTable, databaseName):
    assert liveDatabase.getAllColumnNames(table=peopleTable) == _folded(databaseName, ['id', 'name', 'amount'])
    assert liveDatabase.getPrimaryColumnNames(table=peopleTable) == _folded(databaseName, ['id'])


# Loads -----------------------------------------------------------------------

def test_insert_and_query_round_trip(liveDatabase, peopleTable):
    liveDatabase.insert(table=peopleTable, data=[(1, 'alice', 100), (2, 'bob', 200)])

    rows = liveDatabase.query('SELECT id, name, amount FROM {} ORDER BY id'.format(peopleTable))

    assert rows == [(1, 'alice', 100), (2, 'bob', 200)]


def test_insert_chunking_against_a_real_table(liveDatabase, peopleTable):
    data = [(i, 'name{}'.format(i), i * 10) for i in range(1, 11)]

    liveDatabase.insert(table=peopleTable, data=data, chunkSize=3)

    rows = liveDatabase.query('SELECT COUNT(*) FROM {}'.format(peopleTable))
    assert rows == [(10,)]


def test_upsert_inserts_new_rows_and_updates_existing_ones(liveDatabase, peopleTable):
    """Five syntaxes reach the same result: MySQL and MariaDB's ON DUPLICATE
    KEY, PostgreSQL and SQLite's ON CONFLICT, and a MERGE on Oracle (bind
    variables selected from dual) and SQL Server (a VALUES constructor).
    """
    liveDatabase.insert(table=peopleTable, data=[(1, 'alice', 100)])

    # id 1 already exists (update expected), id 2 is new (insert expected)
    liveDatabase.upsert(table=peopleTable, data=[(1, 'alice-updated', 999), (2, 'bob', 200)])

    rows = liveDatabase.query('SELECT id, name, amount FROM {} ORDER BY id'.format(peopleTable))
    assert rows == [(1, 'alice-updated', 999), (2, 'bob', 200)]


def test_upsert_from_stage(liveDatabase, peopleTable):
    stageTable = _createLike(liveDatabase, peopleTable, '_stage')
    try:
        liveDatabase.insert(table=peopleTable, data=[(1, 'alice', 100)])
        liveDatabase.insert(table=stageTable, data=[(1, 'alice-updated', 999), (2, 'bob', 200)])

        liveDatabase.upsertFromStage(targetTable=peopleTable, stageTable=stageTable)

        rows = liveDatabase.query('SELECT id, name, amount FROM {} ORDER BY id'.format(peopleTable))
        assert rows == [(1, 'alice-updated', 999), (2, 'bob', 200)]
    finally:
        liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(stageTable))


def test_swap_replaces_target_with_stage_contents(liveDatabase, peopleTable):
    """Each database renames its own way, and each is proved here: MySQL and
    MariaDB in one multi-target RENAME TABLE; PostgreSQL in three ALTER TABLEs
    chained in one execute(), which only its simple-query protocol runs in
    full; Oracle in three separate execute() calls, since it runs one statement
    at a time; SQL Server in three chained EXEC sp_rename calls, a procedure
    rather than DDL; SQLite in a transaction.
    """
    stageTable = _createLike(liveDatabase, peopleTable, '_stage')

    liveDatabase.insert(table=peopleTable, data=[(1, 'old', 1)])
    liveDatabase.insert(table=stageTable, data=[(2, 'new', 2)])

    liveDatabase.swap(targetTable=peopleTable, stageTable=stageTable)

    rows = liveDatabase.query('SELECT id, name, amount FROM {}'.format(peopleTable))
    assert rows == [(2, 'new', 2)]
    # The peopleTable fixture drops the target, and after the swap the stage's
    # name holds the old target's rows.
    liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(stageTable))


def test_truncate_removes_all_rows_but_keeps_the_table(liveDatabase, peopleTable, databaseName):
    """SQLite has no TRUNCATE, so this also proves its DELETE FROM fallback."""
    liveDatabase.insert(table=peopleTable, data=[(1, 'alice', 100)])

    liveDatabase.truncate(table=peopleTable)

    assert liveDatabase.query('SELECT * FROM {}'.format(peopleTable)) == []
    assert liveDatabase.getAllColumnNames(table=peopleTable) == _folded(databaseName, ['id', 'name', 'amount'])


def test_context_manager_against_a_real_connection(connectionSettings, peopleTable, databaseName):
    """Confirms the connection is actually closed once the with-block exits, not
    just that __exit__ was called.
    """
    with Database(connectionSettings=connectionSettings) as database:
        database.insert(table=peopleTable, data=[(1, 'alice', 100)])
        assert database.query('SELECT COUNT(*) FROM {}'.format(peopleTable)) == [(1,)]

    with pytest.raises(Exception):
        database.query('SELECT 1' + _fromNothing(databaseName))


# Streaming -------------------------------------------------------------------

def test_stream_returns_real_columns_and_bounded_chunks(liveDatabase, peopleTable):
    """Database.stream against this dialect's real driver and cursor.

    This is the check that matters most per dialect, because streaming is the
    one thing DatabaseDialect cannot fake: a plain fetchmany() bounds how many
    rows Python builds objects for, but says nothing about how many the driver
    already pulled off the socket. Only a real server proves streamingCursor()
    got a non-buffering cursor -- psycopg needs a *named* (server-side) cursor,
    and mysql.connector needs buffered=False, the inverse of what connect() uses.

    The chunk sizes prove fetchmany is bounding the walk; the reassembled rows
    prove nothing is dropped at a chunk boundary.
    """
    rows = [(index, 'name{}'.format(index), index * 10) for index in range(250)]
    liveDatabase.insert(table=peopleTable, data=rows, chunkSize=100)

    columns, chunks = liveDatabase.stream(query='SELECT id, name, amount FROM {} ORDER BY id'.format(peopleTable), chunkSize=100)
    chunkList = list(chunks)

    assert [column.lower() for column in columns] == ['id', 'name', 'amount']
    assert [len(chunk) for chunk in chunkList] == [100, 100, 50]
    assert [tuple(row) for chunk in chunkList for row in chunk] == rows


def test_stream_of_an_empty_table_yields_no_chunks_but_still_reports_columns(liveDatabase, peopleTable):
    """cursor.description has to be populated before any row is fetched -- the
    reason stream() pulls its first chunk eagerly rather than describing off a
    bare execute(), which psycopg's server-side cursors in particular do not
    reliably support. Transform.validate() checks against these columns before
    any load runs, so they must be right even with zero rows.
    """
    columns, chunks = liveDatabase.stream(query='SELECT id, name, amount FROM {}'.format(peopleTable), chunkSize=100)

    assert [column.lower() for column in columns] == ['id', 'name', 'amount']
    assert list(chunks) == []


def test_stream_closes_its_cursor_when_abandoned_part_way_through(liveDatabase, peopleTable):
    """Abandoning the iterator (a transform raising mid-stream) must not leak
    the cursor -- on PostgreSQL that is a server-side cursor otherwise held for
    the life of the connection, and on MySQL unread rows block the connection.
    """
    liveDatabase.insert(table=peopleTable, data=[(index, 'n', 0) for index in range(250)], chunkSize=100)

    _, chunks = liveDatabase.stream(query='SELECT id, name, amount FROM {}'.format(peopleTable), chunkSize=10)
    next(chunks)
    chunks.close()

    assert liveDatabase.query('SELECT count(*) FROM {}'.format(peopleTable)) == [(250,)]


# Run state in a table --------------------------------------------------------

def test_database_memory_records_and_reads_back_a_run(connectionSettings, memoryTable):
    with DatabaseMemory(connectionSettings=connectionSettings, table=memoryTable) as memory:
        memory.recordRun(job='job1')

        assert 'job1' in memory.read()


def test_database_memory_upserts_rather_than_duplicating(liveDatabase, connectionSettings, memoryTable):
    """read() returning one entry per job wouldn't actually prove there's no
    duplicate row (a dict comprehension would just keep the last one) -- check the
    row count directly instead.
    """
    with DatabaseMemory(connectionSettings=connectionSettings, table=memoryTable) as memory:
        memory.recordRun(job='job1')
        firstRun = memory.read()['job1']
        memory.recordRun(job='job1')
        secondRun = memory.read()['job1']

        assert liveDatabase.query('SELECT COUNT(*) FROM {}'.format(memoryTable)) == [(1,)]
        assert secondRun >= firstRun


# Whole jobs ------------------------------------------------------------------

def test_run_data_jobs_end_to_end(liveDatabase, peopleTable, connectionSettings, databaseName, tmp_path):
    """The full runDataJobs path, nothing mocked: real configuration
    validation, a real DependencyGraph, the job in a worker *process* of its
    own, and FileMemory pickled into it, reconstructed there, and writing back
    a real run timestamp -- the closest thing in the suite to a configured
    deployment running.
    """
    liveDatabase.insert(table=peopleTable, data=[(1, 'old', 1)])
    memoryPath = tmp_path / 'memory.yaml'

    _runOneJob(connectionSettings, tmp_path, FileMemory(memoryFile=memoryPath), targetTableFinal=peopleTable,
               sourceQuery="select 2, 'new', 2" + _fromNothing(databaseName))

    rows = liveDatabase.query('SELECT id, name, amount FROM {} ORDER BY id'.format(peopleTable))
    assert rows == [(1, 'old', 1), (2, 'new', 2)]
    assert 'job1' in FileMemory(memoryFile=memoryPath).read()


def test_run_data_jobs_with_database_backed_memory(liveDatabase, peopleTable, memoryTable, connectionSettings, databaseName, tmp_path):
    """As above, with run state in a table: DatabaseMemory, which has no locking
    of its own, only Database.upsert's atomicity, through the same worker
    process round trip.
    """
    liveDatabase.insert(table=peopleTable, data=[(1, 'old', 1)])

    with DatabaseMemory(connectionSettings=connectionSettings, table=memoryTable) as memory:
        _runOneJob(connectionSettings, tmp_path, memory, targetTableFinal=peopleTable,
                   sourceQuery="select 2, 'new', 2" + _fromNothing(databaseName))

        rows = liveDatabase.query('SELECT id, name, amount FROM {} ORDER BY id'.format(peopleTable))
        assert rows == [(1, 'old', 1), (2, 'new', 2)]
        assert 'job1' in memory.read()


def test_run_data_jobs_streams_a_table_larger_than_its_chunk_size(liveDatabase, peopleTable, connectionSettings, tmp_path):
    """A chunkSize well below the row count, so the job genuinely streams, in a
    real worker process through a real driver.
    """
    targetTable = _createLike(liveDatabase, peopleTable, '_target')
    rows = [(index, 'name{}'.format(index), index) for index in range(500)]
    liveDatabase.insert(table=peopleTable, data=rows, chunkSize=100)

    try:
        _runOneJob(connectionSettings, tmp_path, FileMemory(memoryFile=tmp_path / 'jobs.yaml'), chunkSize=37, targetTableFinal=targetTable,
                   sourceQuery='SELECT id, name, amount FROM {} ORDER BY id'.format(peopleTable))

        assert liveDatabase.query('SELECT id, name, amount FROM {} ORDER BY id'.format(targetTable)) == rows
    finally:
        liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(targetTable))


def test_run_data_jobs_with_target_columns_reordered_from_the_tables_own_order(liveDatabase, peopleTable, connectionSettings, databaseName,
                                                                               tmp_path):
    """peopleTable's real column order is (id, name, amount); sourceQuery
    deliberately selects a different order. Without targetColumns, this would
    silently insert id's value into the name column and vice versa -- no error,
    since both are real columns (see targetColumns in docs/configuration.md). Setting
    targetColumns to match the query's actual order is what keeps this correct.
    """
    _runOneJob(connectionSettings, tmp_path, FileMemory(memoryFile=tmp_path / 'memory.yaml'), targetTableFinal=peopleTable,
               targetColumns=['name', 'amount', 'id'], sourceQuery="select 'alice', 100, 1" + _fromNothing(databaseName))

    rows = liveDatabase.query('SELECT id, name, amount FROM {}'.format(peopleTable))
    assert rows == [(1, 'alice', 100)]

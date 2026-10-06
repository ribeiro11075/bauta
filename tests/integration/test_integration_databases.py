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

from bauta.configuration import Configuration, DatabaseType, DataJobsFile
from bauta.database import Database
from bauta.jobs.memory import DatabaseMemory, FileMemory
from bauta.jobs.runner import runDataJobs
from tests.integration.servers import EMBEDDED, SERVERS

DATABASES = [pytest.param(name) for name in EMBEDDED] + [pytest.param(name, marks=pytest.mark.integration) for name in sorted(SERVERS)]


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


def _runOneJob(settings, tmp_path, memory, held=None, **job):
    """Runs `job` against the database `settings` names. `held` is the test's
    own connection to it, let go for the run on DuckDB, which lets one process
    at a time open a file -- and the job runs in a process of its own.
    """

    if held is not None and settings.type == DatabaseType.DUCKDB:
        held.close()
        try:
            return _runOneJob(settings, tmp_path, memory, **job)
        finally:
            held.connect()

    raw = {'workers': 1, 'jobs': {'job1': dict({'active': True, 'sourceConnection': 'db', 'targetConnection': 'db', 'insertStrategy': 'upsert',
                                                'chunkSize': 100}, **job)}}

    return runDataJobs(jobsFile=Configuration.validateJobConfiguration(raw, DataJobsFile), connectionConfiguration={'db': settings},
                       logFile=tmp_path / 'runner.log', memory=memory, runForever=False)


# The driver contract -------------------------------------------------------
# What bauta relies on of every driver; see bauta/database/driver.py. DuckDB's
# broke all four, and nothing said so until a swap failed to commit.

def test_statements_share_one_transaction_until_rolled_back(liveDatabase, peopleTable):
    liveDatabase.execute("INSERT INTO {} (id, name, amount) VALUES (1, 'a', 1)".format(peopleTable))
    liveDatabase.execute("INSERT INTO {} (id, name, amount) VALUES (2, 'b', 2)".format(peopleTable))

    liveDatabase.rollback()

    assert liveDatabase.query('SELECT count(*) FROM {}'.format(peopleTable)) == [(0,)]


def test_what_the_cursor_did_is_committed_by_the_connection(liveDatabase, peopleTable, connectionSettings):
    liveDatabase.execute("INSERT INTO {} (id, name, amount) VALUES (1, 'a', 1)".format(peopleTable))
    liveDatabase.commit()

    with Database(connectionSettings=connectionSettings) as other:
        assert other.query('SELECT count(*) FROM {}'.format(peopleTable)) == [(1,)]


def test_a_statement_reports_the_rows_it_changed(liveDatabase, peopleTable):
    liveDatabase.insert(table=peopleTable, data=[(1, 'a', 1), (2, 'b', 2), (3, 'c', 3)])

    assert liveDatabase.execute('DELETE FROM {} WHERE id > 1'.format(peopleTable)) == 2
    liveDatabase.rollback()


def test_a_rollback_with_nothing_open_does_nothing(liveDatabase):
    """It runs while another error is being handled, which it must not replace."""
    liveDatabase.rollback()
    liveDatabase.rollback()


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

@pytest.fixture
def memoryDatabase(databaseName):
    """Run state is refused in DuckDB, which lets one process at a time open a
    file; see test_integration_duckdb.py.
    """

    if databaseName == 'duckdb':
        pytest.skip('run state is refused in DuckDB')


def test_database_memory_records_and_reads_back_a_run(connectionSettings, memoryTable, memoryDatabase):
    with DatabaseMemory(connectionSettings=connectionSettings, table=memoryTable) as memory:
        memory.recordRun(job='job1')

        assert 'job1' in memory.read()


def test_database_memory_upserts_rather_than_duplicating(liveDatabase, connectionSettings, memoryTable, memoryDatabase):
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

    _runOneJob(connectionSettings, tmp_path, FileMemory(memoryFile=memoryPath), held=liveDatabase, targetTableFinal=peopleTable,
               sourceQuery="select 2, 'new', 2" + _fromNothing(databaseName))

    rows = liveDatabase.query('SELECT id, name, amount FROM {} ORDER BY id'.format(peopleTable))
    assert rows == [(1, 'old', 1), (2, 'new', 2)]
    assert 'job1' in FileMemory(memoryFile=memoryPath).read()


def test_run_data_jobs_with_database_backed_memory(liveDatabase, peopleTable, memoryTable, connectionSettings, databaseName, tmp_path,
                                                   memoryDatabase):
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
        _runOneJob(connectionSettings, tmp_path, FileMemory(memoryFile=tmp_path / 'jobs.yaml'), held=liveDatabase, chunkSize=37,
                   targetTableFinal=targetTable,
                   sourceQuery='SELECT id, name, amount FROM {} ORDER BY id'.format(peopleTable))

        assert liveDatabase.query('SELECT id, name, amount FROM {} ORDER BY id'.format(targetTable)) == rows
    finally:
        liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(targetTable))


def test_run_data_jobs_with_target_columns_reordered_from_the_tables_own_order(liveDatabase, peopleTable, connectionSettings, databaseName,
                                                                               tmp_path):
    """peopleTable's real column order is (id, name, amount); sourceQuery
    deliberately selects a different order. Without targetColumns, this would
    silently insert id's value into the name column and vice versa -- no error,
    since both are real columns (see targetColumns in docs/reference/jobs.md). Setting
    targetColumns to match the query's actual order is what keeps this correct.
    """
    _runOneJob(connectionSettings, tmp_path, FileMemory(memoryFile=tmp_path / 'memory.yaml'), held=liveDatabase, targetTableFinal=peopleTable,
               targetColumns=['name', 'amount', 'id'], sourceQuery="select 'alice', 100, 1" + _fromNothing(databaseName))

    rows = liveDatabase.query('SELECT id, name, amount FROM {}'.format(peopleTable))
    assert rows == [(1, 'alice', 100)]


# Partitions ------------------------------------------------------------------

def test_partition_slices_read_every_row_once(liveDatabase, peopleTable, databaseName):
    """The bounds query and each slice's predicate, through a derived table,
    on every database: together they read each row once, nulls and values
    beyond the bounds included.
    """
    from bauta.jobs.partitions import boundsQuery, integerBound, slicePredicates, splitPoints, wrappedQuery

    rows = [(index, 'name{}'.format(index), None if index % 7 == 0 else index * 3 - 50) for index in range(60)]
    liveDatabase.insert(table=peopleTable, data=rows, chunkSize=100)
    query = 'SELECT id, name, amount FROM {}'.format(peopleTable)
    column = liveDatabase.quoted(_folded(databaseName, ['amount']))[0]

    _, bounds = liveDatabase.stream(boundsQuery(query, column), chunkSize=1)
    with bounds:
        lowest, highest = (integerBound(value, 'amount') for value in next(bounds)[0])

    read = []
    for predicate in slicePredicates(column, splitPoints(lowest, highest, 4)):
        _, chunks = liveDatabase.stream(wrappedQuery(query + ';', predicate), chunkSize=7)
        with chunks:
            read.extend(row for chunk in chunks for row in chunk)

    assert (lowest, highest) == (-47, 127)
    assert sorted(read) == rows


def test_run_data_jobs_with_partitions_swaps_in_every_row(liveDatabase, peopleTable, connectionSettings, databaseName, tmp_path):
    """A partitioned swap through a real run: every partition its own
    connections and thread, all loading one stage table, swapped in once.
    DuckDB is left out: it lets one job at a time open its file, so validation
    refuses it partitions.
    """
    if databaseName == 'duckdb':
        pytest.skip('DuckDB refuses partitions')

    targetTable = _createLike(liveDatabase, peopleTable, '_target')
    stageTable = _createLike(liveDatabase, peopleTable, '_target_stage')
    rows = [(index, 'name{}'.format(index), None if index % 5 == 0 else index) for index in range(300)]
    liveDatabase.insert(table=peopleTable, data=rows, chunkSize=100)
    liveDatabase.insert(table=targetTable, data=[(1000, 'old', 1)])

    try:
        result = _runOneJob(connectionSettings, tmp_path, FileMemory(memoryFile=tmp_path / 'memory.yaml'), chunkSize=23,
                            insertStrategy='swap', targetTableFinal=targetTable, targetTableStage=stageTable,
                            partitions={'column': 'amount', 'count': 3},
                            sourceQuery='SELECT id, name, amount FROM {}'.format(peopleTable))

        assert result.succeeded, result.outcomes
        assert result.rowCount == len(rows)
        assert liveDatabase.query('SELECT id, name, amount FROM {} ORDER BY id'.format(targetTable)) == rows
        assert 'as 3 partition(s)' in (tmp_path / 'runner.log').read_text()
    finally:
        for table in (targetTable, stageTable):
            liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(table))


def test_automatic_partitions_slice_by_the_targets_primary_key(liveDatabase, peopleTable, connectionSettings, databaseName, monkeypatch, tmp_path):
    """`partitions: auto` on every database that can take slices: the column
    is the target's primary key, the count the budget's share, and the masks
    are those of one stream. Run in this process, so the slice size it
    judges by can be made small enough for a test table.
    """
    if databaseName in ('duckdb', 'sqlite'):
        pytest.skip('read as one stream: {} lets one writer in at a time'.format(databaseName))

    import logging
    from bauta.jobs import partitions as partitionsModule
    from bauta.jobs import pipeline
    from bauta.jobs.partitions import CoreBudget
    from tests.jobConfigs import dataJob

    monkeypatch.setattr(partitionsModule, 'MINIMUM_ROWS_PER_SLICE', 50)
    monkeypatch.setattr(pipeline, '_budget', CoreBudget(share=4, maskingThreads=1, automaticThreads=False, places=None))
    rows = [(index, 'name{}'.format(index), index % 9) for index in range(1, 301)]
    liveDatabase.insert(table=peopleTable, data=rows, chunkSize=100)
    masking = {'key': 'an-automatic-partitions-masking-key', 'columns': {'id': 'keep', 'name': {'strategy': 'key'}, 'amount': 'keep'}}

    def copy(suffix, partitions):
        target, stage = _createLike(liveDatabase, peopleTable, suffix), _createLike(liveDatabase, peopleTable, suffix + '_stage')
        job = dataJob(sourceConnection='db', targetConnection='db', sourceQuery='SELECT id, name, amount FROM {}'.format(peopleTable),
                      targetTableFinal=target, targetTableStage=stage, insertStrategy='swap', chunkSize=17, masking=masking, partitions=partitions)
        try:
            outcome = pipeline._executeDataJob('job1', job, {'db': connectionSettings})
            return outcome.rowCount, liveDatabase.query('SELECT id, name, amount FROM {} ORDER BY id'.format(target))
        finally:
            for table in (target, stage):
                liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(table))

    messages = []
    handler = logging.Handler()
    handler.emit = lambda record: messages.append(record.getMessage())
    logging.getLogger('bauta').addHandler(handler)
    monkeypatch.setattr(logging.getLogger('bauta'), 'level', logging.INFO)
    try:
        sliced = copy('_auto', 'auto')
        whole = copy('_whole', None)
    finally:
        logging.getLogger('bauta').removeHandler(handler)

    assert sliced == whole and sliced[0] == len(rows)
    assert any('as 4 slices of' in message for message in messages), messages


# Keys across a swap --------------------------------------------------------

def _keys(database, table):
    """The table's primary key and unique keys, as each database folds them."""

    return ([column.upper() for column in database.dialect.primaryKey(database.cursor, table)],
            sorted(tuple(column.upper() for column in columns) for columns in database.dialect.uniqueKeys(database.cursor, table)))


def test_a_swapped_table_keeps_its_keys_on_every_run(liveDatabase, peopleTable, connectionSettings, tmp_path):
    """The stage is given the target's primary key and unique key before it
    is loaded, so the live table has them after every swap, not every other
    one -- three swaps, so each table has been the target twice, under names
    the database chose without colliding.
    """
    targetTable, stageTable = peopleTable + '_keyed', peopleTable + '_keyed_stage'
    liveDatabase.alter('CREATE TABLE {} (id INT NOT NULL, name VARCHAR(50), amount INT, PRIMARY KEY (id), UNIQUE (name))'.format(targetTable))
    liveDatabase.alter('CREATE TABLE {} (id INT, name VARCHAR(50), amount INT)'.format(stageTable))
    liveDatabase.insert(table=peopleTable, data=[(index, 'name{}'.format(index), index) for index in range(10)])

    try:
        for run in range(3):
            result = _runOneJob(connectionSettings, tmp_path, FileMemory(memoryFile=tmp_path / 'memory.yaml'), held=liveDatabase,
                                insertStrategy='swap', targetTableFinal=targetTable, targetTableStage=stageTable,
                                sourceQuery='SELECT id, name, amount FROM {}'.format(peopleTable))

            assert result.succeeded, (run, result.outcomes)
            assert _keys(liveDatabase, targetTable) == (['ID'], [('NAME',)]), run
            assert liveDatabase.query('SELECT count(*) FROM {}'.format(targetTable)) == [(10,)]
            # Its reads end here, or the next run's renames wait on them.
            liveDatabase.rollback()
        assert _keys(liveDatabase, stageTable) == (['ID'], [('NAME',)])
        liveDatabase.rollback()
        assert 'Gave stage table {} the primary key (id) and unique (name)'.format(stageTable).upper() in \
            (tmp_path / 'runner.log').read_text().upper()
    finally:
        for table in (targetTable, stageTable):
            liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(table))


def test_a_repeated_key_fails_a_swap_before_it_reaches_the_target(liveDatabase, peopleTable, connectionSettings, databaseName, tmp_path):
    """With the target's key on the stage, a source repeating a key fails the
    load, every run, where a keyless stage swapped the repeat in every other.
    """
    stageTable = peopleTable + '_bare_stage'
    liveDatabase.alter('CREATE TABLE {} (id INT, name VARCHAR(50), amount INT)'.format(stageTable))
    liveDatabase.insert(table=peopleTable, data=[(1, 'kept', 1)])

    try:
        result = _runOneJob(connectionSettings, tmp_path, FileMemory(memoryFile=tmp_path / 'memory.yaml'), held=liveDatabase,
                            insertStrategy='swap', targetTableFinal=peopleTable, targetTableStage=stageTable,
                            sourceQuery="SELECT 2, 'twice', 2{0} UNION ALL SELECT 2, 'twice', 2{0}".format(_fromNothing(databaseName)))

        assert result.failed, result.outcomes
        assert liveDatabase.query('SELECT id, name, amount FROM {}'.format(peopleTable)) == [(1, 'kept', 1)]
    finally:
        liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(stageTable))

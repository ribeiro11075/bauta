"""What only SQLite does, against a real file: incremental loads end to end,
foreign keys enforced only when a connection asks, views that follow a renamed
table, and decimals kept as text. What every database does is in
test_integration_databases.py.

SQLite ships with Python and needs no server, so this file isn't marked
`integration` and runs by default. Each test gets a file of its own rather than
":memory:", which is private to the connection that made it, so a job's own
process would never see it.
"""
import uuid

import pytest

from bauta.configuration import Configuration, DatabaseType, DataJobsFile
from bauta.jobs.memory import FileMemory
from bauta.jobs.runner import runDataJobs

DATABASE = 'sqlite'


@pytest.fixture
def ordersTables(liveDatabase):
    suffix = uuid.uuid4().hex[:8]
    source = 'orders_src_{}'.format(suffix)
    target = 'orders_tgt_{}'.format(suffix)

    for table in (source, target):
        liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, name VARCHAR(50), updated_at TEXT)'.format(table))

    yield source, target

    for table in (source, target):
        liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(table))


def _incrementalJobsFile(source, target):
    raw = {'workers': 1, 'jobs': {'loadOrders': {
        'active': True, 'sourceConnection': 'db', 'targetConnection': 'db', 'insertStrategy': 'upsert', 'chunkSize': 2,
        'targetTableFinal': target, 'watermarkColumn': 'updated_at', 'watermarkInitial': '1970-01-01',
        'sourceQuery': 'SELECT id, name, updated_at FROM {} WHERE updated_at > {{{{ watermark }}}} ORDER BY updated_at'.format(source),
        }}}
    return Configuration.validateJobConfiguration(raw, DataJobsFile)


def test_an_incremental_job_loads_only_rows_past_its_watermark(liveDatabase, ordersTables, connectionSettings, tmp_path):
    """End to end through runDataJobs, twice, against a real database.

    The discriminator is row 1: its name changes in the source between runs but
    its updated_at does not. A full re-extract would pick that change up; a
    genuinely incremental one cannot see the row at all, because the predicate
    now starts past it. Row counts alone would prove nothing here -- upsert is
    idempotent, so re-reading everything would produce an identical target.
    """
    source, target = ordersTables
    memoryPath = tmp_path / 'memory.yaml'
    memory = FileMemory(memoryFile=memoryPath)
    jobsFile = _incrementalJobsFile(source, target)

    liveDatabase.insert(table=source, data=[
        (1, 'first', '2026-01-01T00:00:00'),
        (2, 'second', '2026-01-02T00:00:00'),
        (3, 'third', '2026-01-03T00:00:00'),
        ], chunkSize=10)

    runDataJobs(jobsFile=jobsFile, connectionConfiguration={'db': connectionSettings}, logFile=tmp_path / 'jobs.log',
                memory=memory, runForever=False)

    assert liveDatabase.query('SELECT count(*) FROM {}'.format(target)) == [(3,)]
    assert memory.readWatermarks() == {'loadOrders': '2026-01-03T00:00:00'}

    liveDatabase.alter("UPDATE {} SET name = 'CHANGED-BUT-NOT-TOUCHED' WHERE id = 1".format(source))
    liveDatabase.insert(table=source, data=[(4, 'fourth', '2026-01-04T00:00:00')], chunkSize=10)

    runDataJobs(jobsFile=jobsFile, connectionConfiguration={'db': connectionSettings}, logFile=tmp_path / 'jobs.log',
                memory=memory, runForever=False)

    assert liveDatabase.query('SELECT count(*) FROM {}'.format(target)) == [(4,)]
    assert liveDatabase.query('SELECT name FROM {} WHERE id = 1'.format(target)) == [('first',)]
    assert liveDatabase.query('SELECT name FROM {} WHERE id = 4'.format(target)) == [('fourth',)]
    assert memory.readWatermarks() == {'loadOrders': '2026-01-04T00:00:00'}


def test_an_incremental_job_with_nothing_new_loads_nothing_and_keeps_its_watermark(liveDatabase, ordersTables, connectionSettings, tmp_path):
    """A run that extracts no rows must leave the stored watermark where it is --
    overwriting it with the null the job reached would re-extract everything.
    """
    source, target = ordersTables
    memory = FileMemory(memoryFile=tmp_path / 'memory.yaml')
    jobsFile = _incrementalJobsFile(source, target)

    liveDatabase.insert(table=source, data=[(1, 'first', '2026-01-01T00:00:00')], chunkSize=10)

    runDataJobs(jobsFile=jobsFile, connectionConfiguration={'db': connectionSettings}, logFile=tmp_path / 'jobs.log',
                memory=memory, runForever=False)
    runDataJobs(jobsFile=jobsFile, connectionConfiguration={'db': connectionSettings}, logFile=tmp_path / 'jobs.log',
                memory=memory, runForever=False)

    assert liveDatabase.query('SELECT count(*) FROM {}'.format(target)) == [(1,)]
    assert memory.readWatermarks() == {'loadOrders': '2026-01-01T00:00:00'}


def test_declared_foreign_keys_are_enforced(liveDatabase):
    """SQLite ignores them unless each connection asks; every other database enforces them."""
    liveDatabase.alter('CREATE TABLE parents (id INT PRIMARY KEY)')
    liveDatabase.alter('CREATE TABLE children (id INT PRIMARY KEY, parent_id INT REFERENCES parents(id))')
    liveDatabase.insert(table='parents', data=[(1,)])
    liveDatabase.insert(table='children', data=[(10, 1)])

    with pytest.raises(Exception, match='FOREIGN KEY constraint failed'):
        liveDatabase.insert(table='children', data=[(11, 2)])
    liveDatabase.connection.rollback()

    with pytest.raises(Exception, match='FOREIGN KEY constraint failed'):
        liveDatabase.truncate('parents')


def test_a_job_can_turn_enforcement_off_for_its_own_load(liveDatabase, connectionSettings):
    """The documented way out, for a copy that has to load rows its keys refuse."""
    from bauta.jobs.pipeline import _executeDataJob

    liveDatabase.alter('CREATE TABLE parents (id INT PRIMARY KEY)')
    liveDatabase.alter('CREATE TABLE children (id INT PRIMARY KEY, parent_id INT REFERENCES parents(id))')
    liveDatabase.alter('CREATE TABLE incoming (id INT, parent_id INT)')
    liveDatabase.insert(table='incoming', data=[(1, 99)])

    def job(preTargetAdhocQueries):
        return Configuration.validateJobConfiguration({'workers': 1, 'jobs': {'load': {
            'active': True, 'sourceConnection': 'db', 'targetConnection': 'db', 'sourceQuery': 'SELECT * FROM incoming',
            'targetTableFinal': 'children', 'insertStrategy': 'upsert', 'chunkSize': 10,
            'preTargetAdhocQueries': preTargetAdhocQueries}}}, DataJobsFile).jobs['load']

    with pytest.raises(Exception, match='FOREIGN KEY constraint failed'):
        _executeDataJob('load', job([]), {'db': connectionSettings})

    _executeDataJob('load', job(['PRAGMA foreign_keys=OFF']), {'db': connectionSettings})
    assert liveDatabase.query('SELECT * FROM children') == [(1, 99)]


def test_a_swap_leaves_views_reading_the_new_target(liveDatabase):
    """SQLite rewrites the views that name a renamed table, to follow it, so a
    swap used to repoint every view on the target at the stage table -- the old
    rows, emptied by the next run -- and leave it there.
    """
    suffix = uuid.uuid4().hex[:8]
    target, stage = 'orders_{}'.format(suffix), 'orders_{}_stage'.format(suffix)
    view = 'recent_{}'.format(suffix)

    liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, amount INT)'.format(target))
    liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, amount INT)'.format(stage))
    liveDatabase.alter('CREATE VIEW {} AS SELECT id, amount FROM {} WHERE amount > 0'.format(view, target))
    liveDatabase.insert(table=target, data=[(1, 10)])
    liveDatabase.insert(table=stage, data=[(2, 20), (3, 30)])

    liveDatabase.swap(targetTable=target, stageTable=stage)

    assert liveDatabase.query('SELECT id FROM {} ORDER BY id'.format(view)) == [(2,), (3,)]
    assert target in liveDatabase.query("SELECT sql FROM sqlite_master WHERE name = '{}'".format(view))[0][0]


def test_a_decimal_keeps_every_digit_in_a_table_schema_created(liveDatabase, tmp_path):
    """A DECIMAL column has NUMERIC affinity, so SQLite converted the exact text
    to an integer or a float as it stored it: 123456789012345678.1234567890
    came back as 123456789012345680, and 0.1 as the nearest double. `schema`
    gives a decimal a column SQLite keeps as it is written.
    """
    import decimal

    from bauta.database.dialects import ColumnDefinition
    from bauta.generate.schema import TableDefinition, createStatements

    amounts = [decimal.Decimal('123456789012345678.1234567890'), decimal.Decimal('0.1'), decimal.Decimal('-12345678901234567890')]
    definition = TableDefinition(
        name='money', primaryKey=['id'], foreignKeys=[],
        columns=[ColumnDefinition(name='id', dataType='integer', length=None, precision=None, scale=None, nullable=False),
                 ColumnDefinition(name='amount', dataType='numeric', length=None, precision=38, scale=10, nullable=True)])

    (statement,) = createStatements(DatabaseType.POSTGRESQL, DatabaseType.SQLITE, [definition])
    liveDatabase.alter(statement.sql)
    liveDatabase.insert(table='money', data=[(index, amount) for index, amount in enumerate(amounts)])

    stored = [row[0] for row in liveDatabase.query('SELECT amount FROM money ORDER BY id')]

    assert [decimal.Decimal(value) for value in stored] == amounts
    assert any('keeps every digit' in note for note in statement.notes)

"""Proves the actual headline feature works: extracting from one database dialect
and loading into a *different* one within a single job. Every other integration
test uses the same database for both source and target, for simplicity -- this is
the one test that genuinely exercises sourceConnection and targetConnection pointing
at different servers of different types in the same _executeDataJob call, with a
real sourceQueryColumnTransforms entry applied in between.

Requires both a MySQL and a PostgreSQL server reachable at the settings below
(see docker-compose.yml: `docker compose up -d mysql postgresql`) and both
drivers importable. Skipped automatically, with a clear reason, if either isn't
available. Excluded from the default `pytest` run -- run with `pytest -m integration`.
"""
import uuid

import pytest

pytest.importorskip('mysql.connector', reason='mysql-connector-python is not installed (pip install -e ".[mysql]")')
pytest.importorskip('psycopg', reason='psycopg is not installed (pip install -e ".[postgresql]")')

from bauta.configuration import Configuration, connectionConfig, DatabaseType, DataJobsFile
from bauta.database import Database
from bauta.jobs.memory import FileMemory
from bauta.jobs.runner import runDataJobs

pytestmark = pytest.mark.integration

MYSQL_SETTINGS = connectionConfig(
    type=DatabaseType.MYSQL, user='root', password='root', database='bauta_test', host='127.0.0.1', port=3307,
    )
POSTGRESQL_SETTINGS = connectionConfig(
    type=DatabaseType.POSTGRESQL, user='postgres', password='postgres', database='bauta_test', host='127.0.0.1', port=5433,
    )


@pytest.fixture
def mysqlDatabase():
    try:
        database = Database(connectionSettings=MYSQL_SETTINGS)
    except Exception as error:
        pytest.skip(f'no live mysql server reachable at {MYSQL_SETTINGS.host}:{MYSQL_SETTINGS.port} ({error})')

    yield database

    database.close()


@pytest.fixture
def postgresqlDatabase():
    try:
        database = Database(connectionSettings=POSTGRESQL_SETTINGS)
    except Exception as error:
        pytest.skip(f'no live postgresql server reachable at {POSTGRESQL_SETTINGS.host}:{POSTGRESQL_SETTINGS.port} ({error})')

    yield database

    database.close()


@pytest.fixture
def sourceTable(mysqlDatabase):
    tableName = 'source_{}'.format(uuid.uuid4().hex[:8])

    mysqlDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, name VARCHAR(50), amount INT)'.format(tableName))
    mysqlDatabase.insert(table=tableName, data=[(1, 'alice', 100), (2, 'bob', 200)])

    yield tableName

    mysqlDatabase.alter('DROP TABLE IF EXISTS {}'.format(tableName))


@pytest.fixture
def targetTable(postgresqlDatabase):
    tableName = 'target_{}'.format(uuid.uuid4().hex[:8])

    # amount is VARCHAR here, not INT -- the currency transform below turns it
    # into a formatted string ("$100.00") before it's loaded
    postgresqlDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, name VARCHAR(50), amount VARCHAR(20))'.format(tableName))

    yield tableName

    postgresqlDatabase.alter('DROP TABLE IF EXISTS {}'.format(tableName))


def test_data_moves_from_mysql_to_postgresql_with_a_transform_applied(postgresqlDatabase, sourceTable, targetTable, tmp_path):
    raw = {
        'workers': 1,
        'jobs': {
            'job1': {
                'active': True, 'sourceConnection': 'mysql', 'targetConnection': 'postgresql', 'insertStrategy': 'upsert',
                'chunkSize': 100, 'targetTableFinal': targetTable,
                'sourceQueryColumnTransforms': {'amount': ['bauta.transform.builtinTransforms:currency']},
                'sourceQuery': 'select id, name, amount from {} order by id'.format(sourceTable),
                },
            },
        }
    jobsFile = Configuration.validateJobConfiguration(raw, DataJobsFile)
    connectionConfiguration = {'mysql': MYSQL_SETTINGS, 'postgresql': POSTGRESQL_SETTINGS}

    runDataJobs(jobsFile=jobsFile, connectionConfiguration=connectionConfiguration, logFile=tmp_path / 'runner.log',
                memory=FileMemory(memoryFile=tmp_path / 'memory.yaml'), runForever=False)

    rows = postgresqlDatabase.query('SELECT id, name, amount FROM {} ORDER BY id'.format(targetTable))
    assert rows == [(1, 'alice', '$100.00'), (2, 'bob', '$200.00')]


DURATIONS = [(1, '12:34:56.123456'), (2, '-838:59:59'), (3, '838:59:59')]


@pytest.fixture
def mysqlDurations(mysqlDatabase):
    table = 'durations_{}'.format(uuid.uuid4().hex[:8])
    mysqlDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, tm TIME(6))'.format(table))
    mysqlDatabase.alter("INSERT INTO {} VALUES {}".format(table, ', '.join("({}, '{}')".format(*row) for row in DURATIONS)))

    yield table

    mysqlDatabase.alter('DROP TABLE IF EXISTS {}'.format(table))


def test_a_mysql_time_keeps_its_value_in_a_text_column_elsewhere(mysqlDatabase, postgresqlDatabase, mysqlDurations):
    """MySQL's TIME is a duration, and its driver returns a timedelta. Nothing
    else takes one: SQL Server's and SQLite's drivers refuse it, Oracle stored
    Python's own `-35 days, 1:00:01`, and PostgreSQL squeezed a day-long value
    into a TIME column as a wrong time of day without a word.
    """
    rows = mysqlDatabase.query('SELECT id, tm FROM {} ORDER BY id'.format(mysqlDurations))
    table = mysqlDurations

    postgresqlDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, tm VARCHAR(32))'.format(table))
    try:
        postgresqlDatabase.insert(table=table, data=rows)

        assert postgresqlDatabase.query('SELECT id, tm FROM {} ORDER BY id'.format(table)) == DURATIONS
    finally:
        postgresqlDatabase.alter('DROP TABLE IF EXISTS {}'.format(table))


def test_a_day_long_mysql_time_is_refused_by_a_time_column_rather_than_stored_wrong(mysqlDatabase, postgresqlDatabase, mysqlDurations):
    """`schema` maps TIME to TIME, which holds a time of day. -838:59:59 isn't
    one, and used to arrive as 01:00:01.
    """
    rows = mysqlDatabase.query('SELECT id, tm FROM {} WHERE id = 2'.format(mysqlDurations))
    table = mysqlDurations

    postgresqlDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, tm TIME)'.format(table))
    try:
        with pytest.raises(Exception, match='out of range'):
            postgresqlDatabase.insert(table=table, data=rows)
        postgresqlDatabase.connection.rollback()

        assert postgresqlDatabase.query('SELECT count(*) FROM {}'.format(table)) == [(0,)]
    finally:
        postgresqlDatabase.alter('DROP TABLE IF EXISTS {}'.format(table))

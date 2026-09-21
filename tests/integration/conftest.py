"""What the integration suites share: a live connection to one database, and
the tables most of their tests load.

A module names its database with `DATABASE = 'mysql'` (or 'mariadb',
'postgresql', 'oracle', 'mssql', 'sqlite'); test_integration_databases.py
overrides databaseName to run across all six. A server that isn't reachable,
or whose driver isn't installed, skips its tests with the reason.
"""
import importlib
import uuid

import pytest

from bauta.configuration import DatabaseConnectionConfig, DatabaseType
from bauta.database import Database
from tests.integration.servers import SERVERS

# The type of the run-state table's last_run column: no one spelling of a
# double is accepted by all six.
DOUBLE = {'postgresql': 'DOUBLE PRECISION', 'oracle': 'BINARY_DOUBLE', 'mssql': 'FLOAT'}


@pytest.fixture
def databaseName(request):

    return request.module.DATABASE


@pytest.fixture
def connectionSettings(databaseName, tmp_path):
    """SQLite gets a throwaway file of its own; a server is asked first
    whether it is there at all.
    """

    if databaseName == 'sqlite':
        return DatabaseConnectionConfig(type=DatabaseType.SQLITE, database=str(tmp_path / 'test.db'))

    driver, settings = SERVERS[databaseName]
    try:
        importlib.import_module(driver)
        Database(connectionSettings=settings).close()
    except Exception as error:
        pytest.skip('{} is not available at {}:{} ({})'.format(databaseName, settings.host, settings.port, error))

    return settings


@pytest.fixture
def liveDatabase(connectionSettings):

    with Database(connectionSettings=connectionSettings) as database:
        yield database


def _table(database, prefix, columns):

    name = '{}_{}'.format(prefix, uuid.uuid4().hex[:8])
    database.alter('CREATE TABLE {} ({})'.format(name, columns))

    return name


@pytest.fixture
def peopleTable(liveDatabase):

    name = _table(liveDatabase, 'people', 'id INT PRIMARY KEY, name VARCHAR(50), amount INT')

    yield name

    liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(name))


@pytest.fixture
def memoryTable(liveDatabase, databaseName):
    """Shaped like DATABASE_MEMORY_SCHEMA, with this database's double."""

    name = _table(liveDatabase, 'memory', 'job VARCHAR(255) PRIMARY KEY, last_run {}, watermark_value VARCHAR(255), '
                                          'watermark_type VARCHAR(32)'.format(DOUBLE.get(databaseName, 'DOUBLE')))

    yield name

    liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(name))

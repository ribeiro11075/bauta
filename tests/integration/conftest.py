"""What the integration suites share: a live connection to one database, and
the tables most of their tests load.

A module names its database with `DATABASE = 'mysql'` (or 'mariadb',
'postgresql', 'oracle', 'mssql', 'sqlite'); test_integration_databases.py
overrides databaseName to run across all six. A server that isn't reachable,
or whose driver isn't installed, skips its tests with the reason.
"""
import uuid

import pytest

from bauta.database import Database
from tests.integration.servers import serverSettings

# The type of the run-state table's last_run column: no one spelling of a
# double is accepted by all six.
DOUBLE = {'postgresql': 'DOUBLE PRECISION', 'oracle': 'BINARY_DOUBLE', 'mssql': 'FLOAT'}


@pytest.fixture
def databaseName(request):

    return request.module.DATABASE


@pytest.fixture
def connectionSettings(databaseName, tmp_path):
    return serverSettings(databaseName, tmp_path)


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

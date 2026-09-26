"""The database servers docker-compose.yml runs, shared by the integration
suites that are parametrized across all of them. Each entry is the driver
module to import and the connection settings.
"""
import importlib

import pytest

from bauta.configuration import connectionConfig, DatabaseType
from bauta.database import Database

# The databases that run in this process, from a file: no server to start, so
# the suites parametrized across every database run them by default.
EMBEDDED = ['duckdb', 'sqlite']

SERVERS = {
    'mysql': ('mysql.connector', connectionConfig(
        type=DatabaseType.MYSQL, user='root', password='root', database='bauta_test', host='127.0.0.1', port=3307)),
    'mariadb': ('mysql.connector', connectionConfig(
        type=DatabaseType.MARIADB, user='root', password='root', database='bauta_test', host='127.0.0.1', port=3308)),
    'postgresql': ('psycopg', connectionConfig(
        type=DatabaseType.POSTGRESQL, user='postgres', password='postgres', database='bauta_test', host='127.0.0.1', port=5433)),
    'oracle': ('oracledb', connectionConfig(
        type=DatabaseType.ORACLE, user='system', password='oracle', host='127.0.0.1', port=1522,
        serviceName='bauta_test')),
    'mssql': ('pymssql', connectionConfig(
        type=DatabaseType.MSSQL, user='sa', password='YourStr0ng!Passw0rd', database='master', host='127.0.0.1', port=1434)),
    }


def serverSettings(name, tmp_path=None):
    """The settings to reach `name` with -- SQLite and DuckDB a throwaway file
    in `tmp_path` -- or a skip saying why that server can't be reached: it isn't
    running, or its driver isn't installed.
    """

    if name == 'sqlite':
        return connectionConfig(type=DatabaseType.SQLITE, path=str(tmp_path / 'test.db'))
    if name == 'duckdb':
        pytest.importorskip('duckdb')
        return connectionConfig(type=DatabaseType.DUCKDB, path=str(tmp_path / 'test.duckdb'))

    driver, settings = SERVERS[name]
    try:
        importlib.import_module(driver)
        Database(connectionSettings=settings).close()
    except Exception as error:
        pytest.skip('{} is not available at {}:{} ({})'.format(name, settings.host, settings.port, error))

    return settings

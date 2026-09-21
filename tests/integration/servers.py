"""The database servers docker-compose.yml runs, shared by the integration
suites that are parametrized across all of them. Each entry is the driver
module to import and the connection settings.
"""
import importlib

import pytest

from bauta.configuration import DatabaseConnectionConfig, DatabaseType
from bauta.database import Database

SERVERS = {
    'mysql': ('mysql.connector', DatabaseConnectionConfig(
        type=DatabaseType.MYSQL, user='root', password='root', database='bauta_test', host='127.0.0.1', port=3307)),
    'mariadb': ('mysql.connector', DatabaseConnectionConfig(
        type=DatabaseType.MARIADB, user='root', password='root', database='bauta_test', host='127.0.0.1', port=3308)),
    'postgresql': ('psycopg', DatabaseConnectionConfig(
        type=DatabaseType.POSTGRESQL, user='postgres', password='postgres', database='bauta_test', host='127.0.0.1', port=5433)),
    'oracle': ('oracledb', DatabaseConnectionConfig(
        type=DatabaseType.ORACLE, user='system', password='oracle', database='bauta_test', host='127.0.0.1', port=1522,
        serviceName='bauta_test')),
    'mssql': ('pymssql', DatabaseConnectionConfig(
        type=DatabaseType.MSSQL, user='sa', password='YourStr0ng!Passw0rd', database='master', host='127.0.0.1', port=1434)),
    }


def serverSettings(name, tmp_path=None):
    """The settings to reach `name` with -- SQLite a throwaway file in
    `tmp_path` -- or a skip saying why that server can't be reached: it isn't
    running, or its driver isn't installed.
    """

    if name == 'sqlite':
        return DatabaseConnectionConfig(type=DatabaseType.SQLITE, database=str(tmp_path / 'test.db'))

    driver, settings = SERVERS[name]
    try:
        importlib.import_module(driver)
        Database(connectionSettings=settings).close()
    except Exception as error:
        pytest.skip('{} is not available at {}:{} ({})'.format(name, settings.host, settings.port, error))

    return settings

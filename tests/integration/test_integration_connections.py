"""Connection options, encryption and currentSchema against real servers.

What a driver does with an option can only be seen from the server: that it
arrived, that a connection really is encrypted, that a session really resolves
names in the schema it was given. Each test asks the server.

Parametrized over the servers in docker-compose.yml where it applies; any that
isn't reachable, or whose driver isn't installed, is skipped with a reason. Run
with `pytest -m integration`.
"""
import importlib
import os
import subprocess
import sys
import textwrap
import uuid

import pytest

from bauta.database import Database
from tests.integration.servers import SERVERS

pytestmark = pytest.mark.integration


def _connect(name: str, **changes):
    driver, settings = SERVERS[name]
    try:
        importlib.import_module(driver)
        return Database(connectionSettings=settings.model_copy(update=changes))
    except ImportError as error:
        pytest.skip('{} is not available ({})'.format(name, error))


def _requireServer(name: str) -> None:
    try:
        _connect(name).close()
    except Exception as error:
        pytest.skip('{} is not available ({})'.format(name, error))


@pytest.mark.parametrize('name', ['mysql', 'mariadb'])
def test_mysql_tls_is_on_by_default_and_options_can_turn_it_off(name):
    _requireServer(name)

    with _connect(name) as database:
        assert database.isEncrypted() is True

    with _connect(name, options={'ssl_disabled': True}) as database:
        assert database.isEncrypted() is False


def test_postgresql_options_reach_libpq():
    _requireServer('postgresql')

    with _connect('postgresql', options={'application_name': 'bauta-test', 'sslmode': 'disable'}) as database:
        assert database.query("SELECT current_setting('application_name')") == [('bauta-test',)]
        assert database.isEncrypted() is False

    # The compose server has no certificate, so requiring TLS must fail
    # rather than quietly connecting in the clear.
    with pytest.raises(Exception, match='SSL'):
        _connect('postgresql', options={'sslmode': 'require'})


def test_oracle_options_reach_the_driver():
    _requireServer('oracle')

    with _connect('oracle', options={'program': 'bauta-test'}) as database:
        assert database.query("SELECT program FROM v$session WHERE sid = SYS_CONTEXT('USERENV', 'SID')") == [('bauta-test',)]
        assert database.isEncrypted() is False


def test_mssql_encrypts_when_freetds_is_configured_to(tmp_path):
    """pymssql's own `encryption` argument had no effect in testing, so the
    documented route is FreeTDS's configuration file. FreeTDS reads it when
    the process starts using it, hence a separate process.
    """
    _requireServer('mssql')

    (tmp_path / 'freetds.conf').write_text('[global]\n\tencryption = require\n')
    script = textwrap.dedent('''
        from tests.integration.servers import SERVERS
        from bauta.database import Database
        with Database(SERVERS['mssql'][1]) as database:
            print(database.isEncrypted())
        ''')
    repository = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    environment = dict(os.environ, FREETDSCONF=str(tmp_path / 'freetds.conf'),
                       PYTHONPATH=os.pathsep.join([repository, os.path.join(repository, 'tests')]))

    result = subprocess.run([sys.executable, '-c', script], env=environment, capture_output=True, text=True, timeout=120)

    assert result.stdout.strip() == 'True', result.stderr


# How to create and drop another schema, per server that supports currentSchema.
SCHEMAS = {
    'postgresql': ('CREATE SCHEMA {0}', 'DROP SCHEMA {0} CASCADE'),
    'oracle': ('CREATE USER {0} IDENTIFIED BY "Pw{0}" QUOTA UNLIMITED ON USERS', 'DROP USER {0} CASCADE'),
    }


@pytest.mark.parametrize('name', sorted(SCHEMAS))
def test_current_schema_decides_where_unqualified_names_resolve(name):
    _requireServer(name)
    schema = 'current_{}'.format(uuid.uuid4().hex[:6])
    create, drop = SCHEMAS[name]

    with _connect(name) as admin:
        admin.alter(create.format(schema))

        try:
            with _connect(name, currentSchema=schema) as database:
                database.alter('CREATE TABLE people (id INT PRIMARY KEY, name VARCHAR(20))')
                database.upsert(table='people', data=[(1, 'Ann')])
                database.upsert(table='people', data=[(1, 'Bo')])

                assert [column.lower() for column in database.getPrimaryColumnNames('people')] == ['id']
                assert database.tableExists('people')

                # Keys too: Oracle's used to come from the login's own schema.
                database.alter('CREATE TABLE pets (id INT PRIMARY KEY, owner_id INT, CONSTRAINT fk_pets_owner FOREIGN KEY (owner_id) '
                               'REFERENCES people (id))')
                assert [(key.table.lower(), key.referencedTable.lower()) for key in database.getForeignKeys()] == [('pets', 'people')]

            assert admin.query('SELECT id, name FROM {}.people'.format(schema)) == [(1, 'Bo')]
            assert not admin.tableExists('people')
        finally:
            admin.alter(drop.format(schema))


@pytest.mark.parametrize('name', sorted(SERVERS))
def test_require_encryption_connects_only_where_the_server_says_the_session_is_encrypted(name):
    """The containers here encrypt MySQL and MariaDB by default and the rest
    not at all: requireEncryption has to tell them apart by asking each
    server, not by reading the settings.
    """
    _requireServer(name)
    from bauta.configuration import ConfigurationError

    settings = SERVERS[name][1]
    with Database(connectionSettings=settings) as database:
        encrypted = database.isEncrypted()

    required = settings.model_copy(update={'requireEncryption': True})
    if encrypted is True:
        with Database(connectionSettings=required) as database:
            assert database.query('SELECT 1' + (' FROM dual' if name == 'oracle' else '')) == [(1,)]
    else:
        with pytest.raises(ConfigurationError, match='requireEncryption'):
            Database(connectionSettings=required)


@pytest.mark.parametrize('name', sorted(SERVERS))
def test_a_read_only_connection_reads_and_refuses_writes_on_every_server(name):
    """bauta refuses its own writes on a readOnly connection everywhere; where
    the server can make a session read-only (PostgreSQL, MySQL, MariaDB) a
    write made around bauta is refused by the server too.
    """
    _requireServer(name)
    from bauta.configuration import ConfigurationError

    table = 'ro_{}'.format(uuid.uuid4().hex[:8])
    settings = SERVERS[name][1]
    with Database(connectionSettings=settings) as database:
        database.alter('CREATE TABLE {} (id INT PRIMARY KEY)'.format(table))
        database.insert(table, [(1,)])
    try:
        with Database(connectionSettings=settings.model_copy(update={'readOnly': True})) as readOnly:
            assert readOnly.query('SELECT id FROM {}'.format(table)) == [(1,)]
            readOnly.rollback()
            with pytest.raises(ConfigurationError, match='is readOnly'):
                readOnly.insert(table, [(2,)])
            if readOnly.dialect.readOnlySessionStatement() is not None:
                with pytest.raises(Exception):
                    readOnly.cursor.execute('INSERT INTO {} VALUES (3)'.format(table))
                readOnly.rollback()
    finally:
        with Database(connectionSettings=settings) as database:
            assert database.query('SELECT id FROM {}'.format(table)) == [(1,)]
            database.alter('DROP TABLE {}'.format(table))


@pytest.mark.parametrize('name', sorted(SERVERS))
def test_a_watermark_named_twice_is_bound_on_every_server(name):
    """Oracle names its placeholders, :1 for each, and still takes one value
    per placeholder, as the rest do.
    """
    _requireServer(name)

    with Database(connectionSettings=SERVERS[name][1]) as database:
        query, parameters = database.bindWatermark('SELECT 1 AS a{} WHERE 3 > {{{{ watermark }}}} OR 4 > {{{{ watermark }}}}'.format(
            ' FROM dual' if name == 'oracle' else ''), 2)
        _, chunks = database.stream(query, 10, parameters)

        assert [[int(value) for value, in chunk] for chunk in chunks] == [[1]]


# A query each server takes well over a second on, and finishes in a bounded
# time if the limit fails, rather than hanging the suite.
SLOW_QUERIES = {
    'postgresql': 'SELECT pg_sleep(3)',
    'mysql': 'SELECT count(*) FROM information_schema.columns a, information_schema.columns b, (SELECT 1 FROM information_schema.columns LIMIT 60) c',
    'mariadb': 'SELECT count(*) FROM information_schema.columns a, information_schema.columns b, (SELECT 1 FROM information_schema.columns LIMIT 60) c',
    'oracle': 'BEGIN DBMS_SESSION.SLEEP(3); END;',
    'mssql': 'SELECT count_big(*) FROM sys.all_columns a CROSS JOIN sys.all_columns b CROSS JOIN sys.all_columns c',
    }


@pytest.mark.parametrize('name', sorted(SERVERS))
def test_a_statement_past_the_timeout_is_stopped_by_the_server_and_not_retried(name):
    """maxRowsReadPerSecond paces the rows a job fetches, not what its query
    costs the server: a join gone wrong ran on production for as long as it
    took, and was retried.
    """
    _requireServer(name)
    import time

    from bauta.jobs.pipeline import _STATEMENT_TIMEOUT

    settings = SERVERS[name][1].model_copy(update={'statementTimeoutSeconds': 1})
    with Database(connectionSettings=settings) as database:
        assert database.query('SELECT 1' + (' FROM dual' if name == 'oracle' else '')) == [(1,)]
        started = time.monotonic()
        with pytest.raises(Exception) as raised:
            database.cursor.execute(SLOW_QUERIES[name])
            database.cursor.fetchall()

    assert time.monotonic() - started < 2.5
    assert _STATEMENT_TIMEOUT.search(str(raised.value)), str(raised.value)

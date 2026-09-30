"""Foreign keys in a schema other than the connection's own: read where the
jobs' tables are, and a check that found none says where it should have
looked rather than passing.

DuckDB, since it has schemas and needs no server. The same queries run on
the other five in tests/integration/test_integration_masking.py.
"""
import json

import pytest

from bauta.cli import EXIT_JOBS_DID_NOT_SUCCEED, EXIT_SUCCESS, main
from bauta.configuration import connectionConfig
from bauta.database import Database

pytest.importorskip('duckdb')


def _database(path, *statements):
    with Database(connectionSettings=connectionConfig(type='duckdb', path=str(path)), create=True) as database:
        for statement in statements:
            database.alter(statement)


PROD = (
    'CREATE SCHEMA app',
    'CREATE TABLE app.customers (id INT PRIMARY KEY, email TEXT)',
    'CREATE TABLE app.orders (id INT PRIMARY KEY, customer_id INT REFERENCES app.customers(id))',
    "INSERT INTO app.customers VALUES (1, 'a@corp.com')",
    'INSERT INTO app.orders VALUES (10, 1)',
    )

# The copy as `schema --apply` made it before foreign keys were read by
# schema: the tables, no keys, and so nothing to stop a row pointing at nothing.
COPY = (
    'CREATE SCHEMA app',
    'CREATE TABLE app.customers (id INT PRIMARY KEY, email TEXT)',
    'CREATE TABLE app.orders (id INT PRIMARY KEY, customer_id INT)',
    "INSERT INTO app.customers VALUES (2, 'b@corp.com')",
    'INSERT INTO app.orders VALUES (10, 1)',
    )

JOBS = """
jobs:
  copyCustomers:
    sourceConnection: prod
    targetConnection: copy
    sourceQuery: SELECT * FROM {customers}
    targetTableFinal: {customers}
    insertStrategy: upsert
    unmasked: true
  copyOrders:
    sourceConnection: prod
    targetConnection: copy
    sourceQuery: SELECT * FROM {orders}
    targetTableFinal: {orders}
    insertStrategy: upsert
    unmasked: true
"""


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'configuration').mkdir()
    (tmp_path / 'configuration' / 'connections.yaml').write_text(
        'prod:\n  type: duckdb\n  path: ../prod.duckdb\ncopy:\n  type: duckdb\n  path: ../copy.duckdb\n')
    (tmp_path / 'configuration' / 'jobs.yaml').write_text(JOBS.format(customers='app.customers', orders='app.orders'))
    _database(tmp_path / 'prod.duckdb', *PROD)
    _database(tmp_path / 'copy.duckdb', *COPY)

    return tmp_path


def test_foreign_keys_of_a_named_schema_are_qualified_as_list_tables_qualifies_its_tables(workspace):
    with Database(connectionSettings=connectionConfig(type='duckdb', path=str(workspace / 'prod.duckdb'))) as database:
        assert database.getForeignKeys() == []
        ((table, columns, referencedTable, referencedColumns, _),) = database.getForeignKeys('app')
        assert (table, columns, referencedTable, referencedColumns) == ('app.orders', ('customer_id',), 'app.customers', ('id',))
        assert {key.table for key in database.getForeignKeys('app')} <= set(database.listTables(schema='app'))
        assert [key.table for key in database.getForeignKeysFor(['app.orders'])] == ['app.orders']
        assert database.foreignKeysElsewhere([None]) == {'app': 1}
        assert database.foreignKeysElsewhere(['app']) == {}


def test_discover_in_a_named_schema_orders_parents_first_and_shares_the_key_domain(workspace, capsys):
    assert main(['discover', '--connection', 'prod', '--all-tables', '--schema', 'app', '--target', 'copy', '--mask-keys', '--quiet']) == EXIT_SUCCESS

    jobs = capsys.readouterr().out
    assert '- maskApp_customers' in jobs.split('maskApp_orders:')[1]
    assert 'customer_id: {strategy: key, domain: app.customers}' in jobs


def test_schema_in_a_named_schema_creates_its_foreign_keys(workspace, capsys):
    assert main(['schema', '--connection', 'prod', '--target', 'copy', '--all-tables', '--schema', 'app', '--quiet']) == EXIT_SUCCESS

    assert 'REFERENCES' in capsys.readouterr().out


def test_verify_references_counts_orphans_in_a_named_schema(workspace, capsys):
    """Every row of a copy pointing at nothing used to pass as "Checked 0
    foreign key(s)": the keys were read from the connection's own schema.
    """

    assert main(['verify-references', '--quiet']) == EXIT_JOBS_DID_NOT_SUCCEED

    assert 'ORPHANS  copy: app.orders (customer_id) -> app.customers (id) [not declared]: 1 orphaned row(s)' in capsys.readouterr().out


def test_verify_references_fails_when_it_checked_nothing_but_keys_are_declared_elsewhere(workspace, capsys):
    """A job whose query isn't a whole table could read any schema, so the
    connection's own is read: none there, and a key in `app`, is a check
    that looked in the wrong place.
    """

    (workspace / 'configuration' / 'jobs.yaml').write_text(JOBS.replace('SELECT * FROM {orders}', 'SELECT id, customer_id FROM {orders}').replace(
        'SELECT * FROM {customers}', 'SELECT id, email FROM {customers}').format(customers='customers', orders='orders'))
    _database(workspace / 'copy.duckdb', 'CREATE TABLE customers (id INT PRIMARY KEY, email TEXT)', 'CREATE TABLE orders (id INT, customer_id INT)')

    assert main(['verify-references', '--quiet', '--format', 'json']) == EXIT_JOBS_DID_NOT_SUCCEED

    ((note),) = json.loads(capsys.readouterr().out)['unchecked']
    assert (note['database'], note['status'], note['foreignKeysElsewhere']) == ('copy', 'error', {'prod': {'app': 1}})
    assert 'currentSchema' in note['message']


def test_verify_references_with_no_keys_anywhere_passes_but_says_nothing_was_checked(workspace, capsys):
    """Keys the application enforces rather than the database: nothing to
    count is right, but it isn't reported as a check that ran.
    """

    for name in ('prod', 'copy'):
        (workspace / '{}.duckdb'.format(name)).unlink()
        _database(workspace / '{}.duckdb'.format(name), 'CREATE SCHEMA app', 'CREATE TABLE app.customers (id INT PRIMARY KEY)',
                  'CREATE TABLE app.orders (id INT PRIMARY KEY, customer_id INT)')

    assert main(['verify-references', '--quiet']) == EXIT_SUCCESS

    assert capsys.readouterr().out.startswith('NONE     copy: no foreign keys are declared on the tables loaded into it or on their sources')


def test_audit_warns_when_the_jobs_schemas_declare_no_keys_but_another_does(workspace, capsys):
    (workspace / 'configuration' / 'jobs.yaml').write_text(JOBS.replace('SELECT * FROM {orders}', 'SELECT id, customer_id FROM app.orders').replace(
        'SELECT * FROM {customers}', 'SELECT id, email FROM app.customers').format(customers='customers', orders='orders'))
    _database(workspace / 'copy.duckdb', 'CREATE TABLE customers (id INT PRIMARY KEY, email TEXT)', 'CREATE TABLE orders (id INT, customer_id INT)')

    assert main(['audit', '--connect', '--format', 'json', '--quiet']) == EXIT_SUCCESS

    messages = [finding['message'] for finding in json.loads(capsys.readouterr().out)['findings'] if finding['severity'] == 'warning']
    assert any(message.startswith('prod declares no foreign keys in the schema(s) its jobs use, but 1 in app') for message in messages)


def test_audit_notes_a_key_the_source_declares_and_the_copy_does_not(workspace, capsys):
    assert main(['audit', '--connect', '--format', 'json', '--quiet']) == EXIT_SUCCESS

    notes = [finding['message'] for finding in json.loads(capsys.readouterr().out)['findings'] if finding['severity'] == 'info']
    assert any(note.startswith('copy does not declare 1 foreign key(s) its sources do between the tables copied into it: orders(customer_id) -> customers')
               for note in notes)

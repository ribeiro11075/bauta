"""verify-references: orphaned rows counted per foreign key, against real SQLite files."""
import json

import pytest

from bauta.cli import EXIT_JOBS_DID_NOT_SUCCEED, EXIT_SUCCESS, main
from bauta.configuration import DatabaseConnectionConfig, DatabaseType
from bauta.database import Database
from bauta.databaseDialects import ForeignKey
from bauta.references import orphanQuery, verifyReferences

ORDERS_KEY = ForeignKey('ORDERS', ('CUSTOMER_ID',), 'CUSTOMERS', ('ID',), 'FK_ORDERS')


@pytest.fixture
def copy(tmp_path):
    with Database(connectionSettings=DatabaseConnectionConfig(type=DatabaseType.SQLITE, database=str(tmp_path / 'copy.db'))) as database:
        yield database


def _load(database, *statements):
    """Rows loaded as a copy with its keys disabled would load them."""
    database.alter('PRAGMA foreign_keys=OFF')
    for statement in statements:
        database.alter(statement)
    database.alter('PRAGMA foreign_keys=ON')


def _counts(results):
    return [(result.table, result.declared, result.orphans, result.problem) for result in results]


def test_a_declared_key_with_orphaned_rows_is_counted(copy):
    _load(copy, 'CREATE TABLE customers (id INT PRIMARY KEY)',
          'CREATE TABLE orders (id INT PRIMARY KEY, customer_id INT REFERENCES customers(id))',
          'INSERT INTO customers VALUES (1)',
          'INSERT INTO orders VALUES (10, 1), (11, 2), (12, 3), (13, NULL)')

    assert _counts(verifyReferences(copy, 'copy', {'ORDERS': 'orders'})) == [('orders', True, 2, None)]


def test_a_key_only_the_source_declares_is_counted_in_the_target_spelling(copy):
    _load(copy, 'CREATE TABLE customers (id INT PRIMARY KEY)', 'CREATE TABLE orders (id INT PRIMARY KEY, customer_id INT)',
          'INSERT INTO orders VALUES (10, 1)')

    results = verifyReferences(copy, 'copy', {'ORDERS': 'orders', 'CUSTOMERS': 'customers'}, [ORDERS_KEY])

    assert _counts(results) == [('orders', False, 1, None)]
    assert (results[0].columns, results[0].referencedTable, results[0].referencedColumns) == (('customer_id',), 'customers', ('id',))


def test_a_source_key_the_target_also_declares_is_counted_once(copy):
    _load(copy, 'CREATE TABLE customers (id INT PRIMARY KEY)',
          'CREATE TABLE orders (id INT PRIMARY KEY, customer_id INT REFERENCES customers(id))')

    assert _counts(verifyReferences(copy, 'copy', {'ORDERS': 'orders'}, [ORDERS_KEY, ORDERS_KEY])) == [('orders', True, 0, None)]


def test_a_composite_key_skips_rows_with_a_null_column(copy):
    _load(copy, 'CREATE TABLE regions (country TEXT, code TEXT, PRIMARY KEY (country, code))',
          'CREATE TABLE stores (id INT PRIMARY KEY, country TEXT, code TEXT, FOREIGN KEY (country, code) REFERENCES regions (country, code))',
          "INSERT INTO regions VALUES ('pt', 'lx')",
          "INSERT INTO stores VALUES (1, 'pt', 'lx'), (2, 'pt', 'po'), (3, 'es', 'lx'), (4, 'pt', NULL)")

    assert _counts(verifyReferences(copy, 'copy', {'STORES': 'stores'})) == [('stores', True, 2, None)]


def test_only_tables_the_jobs_load_are_checked(copy):
    _load(copy, 'CREATE TABLE customers (id INT PRIMARY KEY)',
          'CREATE TABLE orders (id INT PRIMARY KEY, customer_id INT REFERENCES customers(id))')

    assert verifyReferences(copy, 'copy', {'CUSTOMERS': 'customers'}) == []


def test_a_referenced_table_missing_from_the_target_is_reported(copy):
    _load(copy, 'CREATE TABLE orders (id INT PRIMARY KEY, customer_id INT)')

    assert _counts(verifyReferences(copy, 'copy', {'ORDERS': 'orders'}, [ORDERS_KEY])) == [
        ('orders', False, None, 'CUSTOMERS is not in the target')]


def test_a_column_missing_from_the_target_is_reported(copy):
    _load(copy, 'CREATE TABLE customers (id INT PRIMARY KEY)', 'CREATE TABLE orders (id INT PRIMARY KEY, owner INT)')

    assert _counts(verifyReferences(copy, 'copy', {'ORDERS': 'orders', 'CUSTOMERS': 'customers'}, [ORDERS_KEY])) == [
        ('orders', False, None, 'orders has no column CUSTOMER_ID')]


def test_reserved_word_columns_are_quoted(copy):
    _load(copy, 'CREATE TABLE "order" ("group" INT PRIMARY KEY)', 'CREATE TABLE lines (id INT PRIMARY KEY, "group" INT REFERENCES "order"("group"))',
          'INSERT INTO lines VALUES (1, 5)')

    assert _counts(verifyReferences(copy, 'copy', {'LINES': 'lines'})) == [('lines', True, 1, None)]


def test_a_query_the_database_refuses_is_reported_not_raised(copy, monkeypatch):
    _load(copy, 'CREATE TABLE customers (id INT PRIMARY KEY)',
          'CREATE TABLE orders (id INT PRIMARY KEY, customer_id INT REFERENCES customers(id))')

    def refuse(query):
        raise RuntimeError('permission denied for table customers')

    monkeypatch.setattr(copy, 'query', refuse)

    assert _counts(verifyReferences(copy, 'copy', {'ORDERS': 'orders'})) == [
        ('orders', True, None, 'RuntimeError: permission denied for table customers')]


def test_the_query_names_every_column_of_a_composite_key():
    assert orphanQuery(DatabaseType.MSSQL, 'stores', ['country', 'code'], 'regions', ['country', 'code']) == (
        'SELECT COUNT(*) FROM stores c WHERE c.[country] IS NOT NULL AND c.[code] IS NOT NULL AND NOT EXISTS '
        '(SELECT 1 FROM regions p WHERE p.[country] = c.[country] AND p.[code] = c.[code])')


JOBS_YAML = """workers: 1
jobs:
  loadCustomers:
    active: true
    sourceDatabase: prod
    sourceQuery: SELECT * FROM customers
    targetDatabase: copy
    targetTableFinal: customers
    insertStrategy: upsert
    chunkSize: 10
  loadOrders:
    active: {active}
    predecessors: [loadCustomers]
    sourceDatabase: prod
    sourceQuery: SELECT * FROM orders
    targetDatabase: copy
    targetTableFinal: orders
    insertStrategy: upsert
    chunkSize: 10
"""


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'configuration').mkdir()
    (tmp_path / 'configuration' / 'database.yaml').write_text(
        'prod:\n  type: sqlite\n  database: prod.db\ncopy:\n  type: sqlite\n  database: copy.db\n')
    (tmp_path / 'configuration' / 'jobs.yaml').write_text(JOBS_YAML.format(active='true'))

    for name, orders in (('prod', 'customer_id INT REFERENCES customers(id)'), ('copy', 'customer_id INT')):
        with Database(connectionSettings=DatabaseConnectionConfig(type=DatabaseType.SQLITE, database=str(tmp_path / (name + '.db')))) as database:
            database.alter('CREATE TABLE customers (id INT PRIMARY KEY, email TEXT)')
            database.alter('CREATE TABLE orders (id INT PRIMARY KEY, {})'.format(orders))
            database.alter("INSERT INTO customers VALUES (1, 'a@corp.com')")
            database.alter('INSERT INTO orders VALUES (10, 1)')

    return tmp_path


def _copyStatement(workspace, statement):
    with Database(connectionSettings=DatabaseConnectionConfig(type=DatabaseType.SQLITE, database=str(workspace / 'copy.db'))) as database:
        database.alter(statement)


def test_verify_references_passes_a_copy_whose_references_resolve(workspace, capsys):
    assert main(['verify-references', '--quiet']) == EXIT_SUCCESS

    assert capsys.readouterr().out == ('OK       copy: orders (customer_id) -> customers (id) [not declared]: 0 orphaned row(s)\n'
                                       'Checked 1 foreign key(s): 0 with orphaned rows, 0 not checked\n')


def test_verify_references_fails_on_orphans_and_shows_no_values(workspace, capsys):
    _copyStatement(workspace, 'INSERT INTO orders VALUES (11, 7), (12, 8)')

    assert main(['verify-references', '--quiet', '--format', 'json']) == EXIT_JOBS_DID_NOT_SUCCEED

    report = json.loads(capsys.readouterr().out)
    assert report['summary'] == {'checked': 1, 'orphaned': 1, 'notChecked': 0}
    (reference,) = report['references']
    assert (reference['table'], reference['orphans'], reference['status'], reference['declared']) == ('orders', 2, 'orphans', False)
    assert '7' not in json.dumps(reference) and 'a@corp.com' not in json.dumps(report)


def test_verify_references_fails_on_a_key_it_cannot_check(workspace, capsys):
    _copyStatement(workspace, 'DROP TABLE customers')

    assert main(['verify-references', '--quiet']) == EXIT_JOBS_DID_NOT_SUCCEED
    assert 'ERROR    copy: orders (customer_id) -> customers (id) [not declared]: customers is not in the target' in capsys.readouterr().out


def test_verify_references_skips_inactive_jobs_unless_named(workspace, capsys):
    (workspace / 'configuration' / 'jobs.yaml').write_text(JOBS_YAML.format(active='false'))
    _copyStatement(workspace, 'INSERT INTO orders VALUES (11, 7)')

    assert main(['verify-references', '--quiet']) == EXIT_SUCCESS
    assert main(['verify-references', '--quiet', '--job', 'loadOrders']) == EXIT_JOBS_DID_NOT_SUCCEED

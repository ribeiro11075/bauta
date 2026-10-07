"""Synthetic rows on real servers, where every catalog and every driver's
types differ. Run with `pytest -m integration`.
"""
import uuid

import pytest

from bauta.database import Database
from bauta.generate.synthesize import synthesizeTable
from tests.integration.servers import SERVERS, serverSettings

pytestmark = pytest.mark.integration

# Per server: timestamp, boolean-ish, text, uuid-ish column types.
TYPES = {
    'mysql': ('DATETIME', 'BOOLEAN', 'TEXT', 'CHAR(36)'),
    'mariadb': ('DATETIME', 'BOOLEAN', 'TEXT', 'UUID'),
    'postgresql': ('TIMESTAMP', 'BOOLEAN', 'TEXT', 'UUID'),
    'oracle': ('TIMESTAMP', 'NUMBER(1)', 'CLOB', 'VARCHAR2(36)'),
    'mssql': ('DATETIME2', 'BIT', 'NVARCHAR(MAX)', 'UNIQUEIDENTIFIER'),
    }


@pytest.fixture(params=sorted(SERVERS))
def server(request):
    settings = serverSettings(request.param)
    database = Database(connectionSettings=settings)

    yield request.param, database

    database.close()


def test_a_related_schema_fills_with_valid_rows(server):
    name, database = server
    timestamp, flag, text, identifier = TYPES[name]
    suffix = uuid.uuid4().hex[:6]
    customers, products, orders, items = ('{}_{}'.format(table, suffix) for table in ('cust', 'prod', 'ord', 'item'))
    statements = [
        'CREATE TABLE {} (id INT PRIMARY KEY, email VARCHAR(60) NOT NULL, code VARCHAR(20) NOT NULL UNIQUE, first_name VARCHAR(20), '
        'birth_date DATE, balance DECIMAL(10,2), active {}, notes {}, created_at {}, token {})'.format(
            customers, flag, text, timestamp, identifier),
        'CREATE TABLE {} (sku VARCHAR(12) PRIMARY KEY, price DECIMAL(8,2) NOT NULL)'.format(products),
        'CREATE TABLE {0} (id INT PRIMARY KEY, customer_id INT NOT NULL, CONSTRAINT fk_{0} FOREIGN KEY (customer_id) REFERENCES {1}(id))'.format(
            orders, customers),
        'CREATE TABLE {0} (order_id INT NOT NULL, line INT NOT NULL, sku VARCHAR(12) NOT NULL, PRIMARY KEY (order_id, line), '
        'CONSTRAINT fk1_{0} FOREIGN KEY (order_id) REFERENCES {1}(id), CONSTRAINT fk2_{0} FOREIGN KEY (sku) REFERENCES {2}(sku))'.format(
            items, orders, products),
        ]
    for statement in statements:
        database.alter(statement)

    try:
        counts = [synthesizeTable(database, table, rows) for table, rows in ((customers, 40), (products, 6), (orders, 80), (items, 150))]

        assert counts == [40, 6, 80, 150]
        assert database.query('SELECT count(*) FROM {} WHERE customer_id NOT IN (SELECT id FROM {})'.format(orders, customers)) == [(0,)]
        assert database.query('SELECT count(DISTINCT email) FROM {}'.format(customers)) == [(40,)]
        assert database.query('SELECT count(*) FROM {} WHERE sku NOT IN (SELECT sku FROM {})'.format(items, products)) == [(0,)]

        # And again, continuing after the keys already there. A UNIQUE column
        # that isn't the key is what a repeated run used to collide on.
        assert synthesizeTable(database, customers, 10) == 10
        assert database.query('SELECT max(id) FROM {}'.format(customers))[0][0] == 50
        assert database.query('SELECT count(DISTINCT code) FROM {}'.format(customers)) == [(50,)]
    finally:
        for table in (items, orders, products, customers):
            database.alter('DROP TABLE {}'.format(table))


def test_a_uuid_primary_key_gets_a_different_uuid_in_every_row_and_run(server):
    """Where the server has a UUID type; elsewhere such a key is text, which
    the test above covers."""
    name, database = server
    identifier = TYPES[name][3]
    if identifier.startswith(('CHAR', 'VARCHAR')):
        pytest.skip('{} has no UUID type'.format(name))
    table = 'uuids_{}'.format(uuid.uuid4().hex[:6])
    database.alter('CREATE TABLE {} (id {} PRIMARY KEY, label VARCHAR(20))'.format(table, identifier))

    try:
        assert synthesizeTable(database, table, 30) == 30
        assert synthesizeTable(database, table, 30) == 30
        identifiers = [str(row[0]) for row in database.query('SELECT id FROM {}'.format(table))]
        assert len(set(identifiers)) == 60
        assert all(uuid.UUID(value) for value in identifiers)
    finally:
        database.alter('DROP TABLE {}'.format(table))


def test_generated_rows_keep_to_check_constraints_as_each_server_spells_them(server):
    """Each catalog spells a CHECK its own way -- PostgreSQL's = ANY (ARRAY[...]),
    SQL Server's OR of brackets, MySQL's character sets -- and none was read,
    so the first row breaking one was refused.
    """
    name, database = server[0], server[1]
    table = 'chk_{}'.format(uuid.uuid4().hex[:8])
    number = 'NUMBER(10,2)' if name == 'oracle' else 'DECIMAL(10,2)'
    text = 'VARCHAR2(10)' if name == 'oracle' else 'VARCHAR(10)'
    database.alter("CREATE TABLE {0} (id INT PRIMARY KEY, status {1} NOT NULL, amount {2}, pct INT, CONSTRAINT {0}_s CHECK (status IN "
                   "('open', 'closed', 'held')), CONSTRAINT {0}_a CHECK (amount > 0 AND amount < 1000), CONSTRAINT {0}_p CHECK "
                   "(pct BETWEEN 1 AND 100))".format(table, text, number))
    try:
        assert synthesizeTable(database, table, 300, seed=1) == 300
        statuses, low, high, smallest, largest = database.query('SELECT count(DISTINCT status), min(amount), max(amount), min(pct), max(pct) '
                                                                 'FROM {}'.format(table))[0]
        assert int(statuses) == 3 and 0 < low and high < 1000 and 1 <= smallest and largest <= 100
    finally:
        database.alter('DROP TABLE {}'.format(table))

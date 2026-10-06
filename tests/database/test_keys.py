"""Database.copyKeys: a swap's stage table given the target's primary key and
unique keys, so the target keeps them whichever table holds its name.

SQLite and DuckDB run here, in this process; tests/integration/
test_integration_databases.py swaps keyed tables on every database.
"""
import pytest

from bauta.configuration import ConfigurationError, connectionConfig, DatabaseType
from bauta.database import Database
from bauta.database.dialects.base import _uniqueColumnGroups, _withKeys
from bauta.database.dialects.sqlite import _qualifiedIndex


def test_unique_keys_are_grouped_in_order_once_each_without_an_expression():
    rows = [('b_idx', 'b'), ('a_idx', 'a'), ('a_idx', 'c'), ('expr', None), ('again', 'b'), ('expr', 'x')]

    assert _uniqueColumnGroups(rows) == [('b',), ('a', 'c')]


@pytest.mark.parametrize('created, table, expected', [
    ('CREATE TABLE t (id INT, a TEXT)', '"t"', 'CREATE TABLE "t" (id INT, a TEXT, PRIMARY KEY ("id"), UNIQUE ("a"))'),
    ('CREATE TABLE s.t(id INTEGER, a VARCHAR);', '"s"."t"', 'CREATE TABLE "s"."t"(id INTEGER, a VARCHAR, PRIMARY KEY ("id"), UNIQUE ("a"))'),
    ('create table "my (odd)"."t""x" (id int check (id > 0)) WITHOUT ROWID', '"t"',
     'CREATE TABLE "t" (id int check (id > 0), PRIMARY KEY ("id"), UNIQUE ("a")) WITHOUT ROWID'),
    ('CREATE TABLE [t] (id INT)', '"t"', 'CREATE TABLE "t" (id INT, PRIMARY KEY ("id"), UNIQUE ("a"))'),
    ])
def test_a_create_statement_is_given_keys_after_its_last_column(created, table, expected):
    assert _withKeys(created, table, ['"id"'], [['"a"']]) == expected


def test_an_index_in_an_attached_database_is_put_back_in_it():
    assert _qualifiedIndex('CREATE UNIQUE INDEX t_ab on t (a, b)', '"aux"') == 'CREATE UNIQUE INDEX "aux".t_ab on t (a, b)'
    assert _qualifiedIndex('CREATE INDEX IF NOT EXISTS t_b ON t (b)', '"aux"') == 'CREATE INDEX IF NOT EXISTS "aux".t_b ON t (b)'


@pytest.fixture(params=['sqlite', 'duckdb'])
def database(request, tmp_path):
    if request.param == 'duckdb':
        pytest.importorskip('duckdb')
    settings = connectionConfig(type=DatabaseType(request.param), path=str(tmp_path / 'keys.db'))

    with Database(connectionSettings=settings, create=True) as database:
        yield database


def _keys(database, table):
    return database.dialect.primaryKey(database.cursor, table), database.dialect.uniqueKeys(database.cursor, table)


def test_a_keyless_stage_is_given_the_targets_primary_and_unique_keys(database):
    database.alter('CREATE TABLE orders (id INT NOT NULL, ref VARCHAR(20), note TEXT, a INT, b INT, PRIMARY KEY (id), UNIQUE (ref), '
                   'UNIQUE (a, b))')
    database.alter('CREATE TABLE orders_stage (ID INT, REF VARCHAR(20), NOTE TEXT, A INT, B INT)')

    added = database.copyKeys(fromTable='orders', toTable='orders_stage')

    assert added == ['primary key (ID)', 'unique (REF)', 'unique (A, B)']
    primaryKey, uniqueKeys = _keys(database, 'orders_stage')
    assert (primaryKey, sorted(uniqueKeys)) == (['ID'], [('A', 'B'), ('REF',)])
    assert database.getPrimaryColumnNames('orders_stage') == ['ID']
    assert database.copyKeys(fromTable='orders', toTable='orders_stage') == []

    database.insert(table='orders_stage', data=[(1, 'r1', 'n', 1, 1)])
    with pytest.raises(Exception):
        database.insert(table='orders_stage', data=[(1, 'r2', 'n', 2, 2)])
    database.rollback()


def test_a_stage_that_has_the_key_is_given_only_what_it_lacks(database):
    database.alter('CREATE TABLE orders (id INT PRIMARY KEY, ref VARCHAR(20) UNIQUE)')
    database.alter('CREATE TABLE orders_stage (id INT PRIMARY KEY, ref VARCHAR(20))')

    assert database.copyKeys(fromTable='orders', toTable='orders_stage') == ['unique (ref)']


def test_a_target_without_keys_gives_nothing(database):
    database.alter('CREATE TABLE orders (id INT, ref VARCHAR(20))')
    database.alter('CREATE TABLE orders_stage (id INT, ref VARCHAR(20))')

    assert database.copyKeys(fromTable='orders', toTable='orders_stage') == []


def test_a_stage_holding_rows_is_not_created_again(database):
    database.alter('CREATE TABLE orders (id INT PRIMARY KEY)')
    database.alter('CREATE TABLE orders_stage (id INT)')
    database.insert(table='orders_stage', data=[(1,)])

    with pytest.raises(ConfigurationError, match='holds rows'):
        database.copyKeys(fromTable='orders', toTable='orders_stage')

    assert database.query('SELECT id FROM orders_stage') == [(1,)]


def _swapJob():
    from bauta.configuration import DataJobConfig

    return DataJobConfig(active=True, sourceConnection='db', sourceQuery='SELECT id, ref FROM source', targetConnection='db',
                         targetTableFinal='orders', targetTableStage='orders_stage', insertStrategy='swap', chunkSize=10)


@pytest.fixture
def swapTables(tmp_path):
    settings = connectionConfig(type=DatabaseType.SQLITE, path=str(tmp_path / 'swap.db'))

    with Database(connectionSettings=settings, create=True) as database:
        database.alter('CREATE TABLE source (id INT, ref TEXT)')
        database.alter('CREATE TABLE orders (id INT PRIMARY KEY, ref TEXT UNIQUE)')
        database.alter('CREATE TABLE orders_stage (id INT, ref TEXT)')
        database.insert(table='source', data=[(1, 'a'), (2, 'b')])

    return {'db': settings}


def test_a_swap_job_gives_its_stage_the_targets_keys_before_loading(swapTables, caplog):
    from bauta.jobs.pipeline import _executeDataJob

    with caplog.at_level('INFO', logger='bauta'):
        _executeDataJob('swapOrders', _swapJob(), swapTables)

    assert 'Gave stage table orders_stage the primary key (id) and unique (ref) of orders, so the swap keeps them' in caplog.text
    with Database(connectionSettings=swapTables['db']) as database:
        assert _keys(database, 'orders') == (['id'], [('ref',)])
        assert database.query('SELECT id, ref FROM orders ORDER BY id') == [(1, 'a'), (2, 'b')]


def test_a_key_that_cannot_be_given_is_a_warning_and_the_swap_goes_ahead(swapTables, caplog, monkeypatch):
    from bauta.jobs.pipeline import _executeDataJob

    def refuse(self, fromTable, toTable):
        raise RuntimeError('permission denied for table orders_stage')

    monkeypatch.setattr(Database, 'copyKeys', refuse)

    outcome = _executeDataJob('swapOrders', _swapJob(), swapTables)

    assert outcome.rowCount == 2
    assert 'Could not give stage table orders_stage the keys of orders, so after this swap orders lacks them until the next one' in caplog.text


def test_sqlite_keeps_the_stages_own_indexes_and_its_attached_database(tmp_path):
    settings = connectionConfig(type=DatabaseType.SQLITE, path=str(tmp_path / 'main.db'))

    with Database(connectionSettings=settings, create=True) as database:
        database.alter("ATTACH DATABASE '{}' AS aux".format(tmp_path / 'aux.db'))
        database.alter('CREATE TABLE aux.orders (id INT PRIMARY KEY, ref TEXT UNIQUE)')
        database.alter('CREATE TABLE aux.orders_stage (id INT, ref TEXT, note TEXT)')
        database.alter('CREATE INDEX aux.orders_stage_note ON orders_stage (note)')

        assert database.copyKeys(fromTable='aux.orders', toTable='aux.orders_stage') == ['primary key (id)', 'unique (ref)']

        assert _keys(database, 'aux.orders_stage') == (['id'], [('ref',)])
        assert database.query("SELECT name FROM aux.sqlite_master WHERE type = 'index' AND sql IS NOT NULL") == [('orders_stage_note',)]
        assert database.query("SELECT count(*) FROM main.sqlite_master WHERE name = 'orders_stage'") == [(0,)]

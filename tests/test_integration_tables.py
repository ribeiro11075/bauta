"""listTables against SQLite and the five servers.

What counts as "a table a job could copy" is a different question on each: a
view lives beside tables in one catalog and in another, Oracle keeps dropped
tables in a recycle bin and pieces of other tables in the same view, and
SQLite's own bookkeeping tables sit in sqlite_master. Each must come back with
the base tables and nothing else, named so that the name reads back as that
same table.

Servers that aren't reachable are skipped. Run with `pytest -m integration`.
"""
import importlib
import uuid

import pytest

from bauta.configuration import DatabaseConnectionConfig, DatabaseType
from bauta.database import Database
from servers import SERVERS

pytestmark = pytest.mark.integration

NAMES = ['sqlite'] + sorted(SERVERS)


@pytest.fixture(params=NAMES)
def database(request, tmp_path):
    if request.param == 'sqlite':
        settings = DatabaseConnectionConfig(type=DatabaseType.SQLITE, database=str(tmp_path / 'tables.db'))
    else:
        driver, settings = SERVERS[request.param]
        try:
            importlib.import_module(driver)
            Database(connectionSettings=settings).close()
        except Exception as error:
            pytest.skip('{} is not available ({})'.format(request.param, error))

    with Database(connectionSettings=settings) as database:
        created = []
        views = []
        database.created = created  # type: ignore[attr-defined]
        database.views = views  # type: ignore[attr-defined]
        yield database
        for view in reversed(views):
            try:
                database.alter('DROP VIEW {}'.format(view))
            except Exception:
                database.connection.rollback()
        for table in reversed(created):
            try:
                database.alter('DROP TABLE {}'.format(table))
            except Exception:
                database.connection.rollback()


def _create(database, name):
    table = '{}_{}'.format(name, uuid.uuid4().hex[:6])
    database.alter('CREATE TABLE {} (id INTEGER NOT NULL PRIMARY KEY, label VARCHAR(20))'.format(table))
    database.created.append(table)

    return table


def _createView(database, table):
    view = 'v_{}'.format(table)
    database.alter('CREATE VIEW {} AS SELECT id FROM {}'.format(view, table))
    database.views.append(view)

    return view


def _folded(database, name):
    """A name as this database's catalog holds it, which on Oracle is upper case."""

    from bauta.databaseDialects import catalogName

    return catalogName(database.type, name)


def test_list_tables_reports_the_tables_it_created(database):
    first, second = _create(database, 'alpha'), _create(database, 'beta')

    listed = database.listTables()

    assert _folded(database, first) in listed
    assert _folded(database, second) in listed


def test_list_tables_leaves_views_out(database):
    table = _create(database, 'viewed')
    view = _createView(database, table)

    listed = database.listTables()

    assert _folded(database, table) in listed
    assert _folded(database, view) not in listed


def test_list_tables_leaves_out_the_servers_own_tables(database):
    """Nothing a copy would read belongs to the server itself. The catalogs
    each hide their own a different way, so this asks rather than assumes.
    """
    _create(database, 'ours')

    listed = [name.lower() for name in database.listTables()]

    assert not any(name.startswith('sqlite_') for name in listed)
    assert not any(name.startswith(('sys', 'dba_', 'all_', 'user_', 'v$', 'bin$')) for name in listed)
    assert not any(name.startswith(('pg_', 'information_schema')) for name in listed)


def test_every_listed_table_can_be_read_back_by_name(database):
    """The shape that matters: what listTables returns is what catalogColumns
    and getPrimaryColumnNames take.
    """
    table = _create(database, 'readback')

    listed = database.listTables()
    assert _folded(database, table) in listed

    for name in listed:
        columns = database.catalogColumns(name)
        assert columns, 'no columns read back for {}'.format(name)

    assert [column.lower() for column in database.catalogColumns(_folded(database, table))] == ['id', 'label']
    assert [column.lower() for column in database.getPrimaryColumnNames(_folded(database, table))] == ['id']


def test_list_tables_is_sorted(database):
    _create(database, 'zulu')
    _create(database, 'alpha')

    listed = database.listTables()

    assert listed == sorted(listed)


def test_listing_a_named_schema_qualifies_what_it_returns(database):
    """With a schema, every name comes back qualified with it -- and still
    reads back as that table.
    """
    from bauta.databaseDialects import catalogName

    table = _create(database, 'qualified')

    if database.type == DatabaseType.SQLITE:
        pytest.skip('SQLite has no schema separate from the attached database; covered in test_database.py')

    schema = {
        DatabaseType.MYSQL: 'bauta_test', DatabaseType.MARIADB: 'bauta_test',
        DatabaseType.POSTGRESQL: 'public', DatabaseType.ORACLE: 'SYSTEM', DatabaseType.MSSQL: 'dbo',
        }[database.type]

    listed = database.listTables(schema=schema)

    assert '{}.{}'.format(catalogName(database.type, schema), _folded(database, table)) in listed
    assert [column.lower() for column in database.catalogColumns(
        '{}.{}'.format(catalogName(database.type, schema), _folded(database, table)))] == ['id', 'label']

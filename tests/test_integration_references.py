"""verify-references against SQLite and the five servers.

Orphaned rows get past a declared key a different way on each: MySQL and
MariaDB with FOREIGN_KEY_CHECKS off, PostgreSQL through a key added NOT VALID,
SQL Server through NOCHECK, Oracle through a disabled constraint, SQLite with
its enforcement off. Each must still list the key and count the rows. Keys only
a source declares are matched to the target's own spelling, which on Oracle is
upper case.

Servers that aren't reachable are skipped. Run with `pytest -m integration`.
"""
import importlib
import uuid

import pytest

from bauta.configuration import DatabaseConnectionConfig, DatabaseType
from bauta.database import Database
from bauta.databaseDialects import ForeignKey
from bauta.references import verifyReferences
from servers import SERVERS

pytestmark = pytest.mark.integration

NAMES = ['sqlite'] + sorted(SERVERS)


@pytest.fixture(params=NAMES)
def database(request, tmp_path):
    if request.param == 'sqlite':
        settings = DatabaseConnectionConfig(type=DatabaseType.SQLITE, database=str(tmp_path / 'references.db'))
    else:
        driver, settings = SERVERS[request.param]
        try:
            importlib.import_module(driver)
            Database(connectionSettings=settings).close()
        except Exception as error:
            pytest.skip('{} is not available ({})'.format(request.param, error))

    with Database(connectionSettings=settings) as database:
        created = []
        database.created = created  # type: ignore[attr-defined]
        yield database
        for table in reversed(created):
            database.alter('DROP TABLE IF EXISTS {}'.format(table))


def _create(database, name, definition):
    table = '{}_{}'.format(name, uuid.uuid4().hex[:6])
    database.alter('CREATE TABLE {} ({})'.format(table, definition.format(table=table)))
    database.created.append(table)
    return table


def _childPastItsKey(database, parent, rows):
    """A child table whose declared key its rows break, loaded the way each
    database allows.
    """

    key = 'CONSTRAINT fk_{{table}} FOREIGN KEY (parent_id) REFERENCES {} (id)'.format(parent)
    if database.type == DatabaseType.POSTGRESQL:
        child = _create(database, 'ref_child', 'id INT NOT NULL PRIMARY KEY, parent_id INT')
        database.insert(table=child, data=rows)
        database.alter('ALTER TABLE {0} ADD {1} NOT VALID'.format(child, key.format(table=child)))
        return child

    child = _create(database, 'ref_child', 'id INT NOT NULL PRIMARY KEY, parent_id INT, ' + key)
    constraint = 'fk_' + child
    switches = {
        DatabaseType.MYSQL: ('SET FOREIGN_KEY_CHECKS=0', 'SET FOREIGN_KEY_CHECKS=1'),
        DatabaseType.MARIADB: ('SET FOREIGN_KEY_CHECKS=0', 'SET FOREIGN_KEY_CHECKS=1'),
        DatabaseType.SQLITE: ('PRAGMA foreign_keys=OFF', 'PRAGMA foreign_keys=ON'),
        DatabaseType.MSSQL: ('ALTER TABLE {} NOCHECK CONSTRAINT {}'.format(child, constraint), None),
        DatabaseType.ORACLE: ('ALTER TABLE {} DISABLE CONSTRAINT {}'.format(child, constraint), None),
        }
    off, on = switches[database.type]
    database.alter(off)
    database.insert(table=child, data=rows)
    if on:
        database.alter(on)

    return child


def test_orphans_behind_a_declared_key_are_counted(database):
    parent = _create(database, 'ref_parent', 'id INT NOT NULL PRIMARY KEY')
    database.insert(table=parent, data=[(1,)])
    child = _childPastItsKey(database, parent, [(10, 1), (11, 2), (12, None), (13, 3)])

    results = verifyReferences(database, 'copy', {child.upper(): child})

    assert [(result.table.lower(), result.referencedTable.lower(), result.declared, result.orphans, result.problem) for result in results] == [
        (child, parent, True, 2, None)]


def test_orphans_behind_a_key_only_the_source_declares_are_counted(database):
    parent = _create(database, 'src_parent', 'id INT NOT NULL PRIMARY KEY')
    child = _create(database, 'src_child', 'id INT NOT NULL PRIMARY KEY, parent_id INT')
    regions = _create(database, 'src_regions', 'country VARCHAR(10) NOT NULL, code VARCHAR(10) NOT NULL, PRIMARY KEY (country, code)')
    stores = _create(database, 'src_stores', 'id INT NOT NULL PRIMARY KEY, country VARCHAR(10), code VARCHAR(10)')
    database.insert(table=parent, data=[(1,)])
    database.insert(table=child, data=[(10, 1), (11, 5), (12, None)])
    database.insert(table=regions, data=[('pt', 'lx')])
    database.insert(table=stores, data=[(1, 'pt', 'lx'), (2, 'pt', 'po'), (3, 'es', 'lx'), (4, 'pt', None)])
    sourceKeys = [ForeignKey(child, ('parent_id',), parent, ('id',), 'fk_child'),
                  ForeignKey(stores, ('country', 'code'), regions, ('country', 'code'), 'fk_stores')]

    results = verifyReferences(database, 'copy', {table.upper(): table for table in (parent, child, regions, stores)}, sourceKeys)

    assert [(result.table, result.declared, result.orphans, result.problem) for result in results] == [
        (child, False, 1, None), (stores, False, 2, None)]
    assert [column.lower() for column in results[1].columns] == ['country', 'code']


def test_a_target_in_another_schema_is_counted_there_not_in_the_connections_schema(database):
    """A same-named table in the connection's own schema used to be counted
    instead, so a broken copy was reported clean.
    """
    if database.type != DatabaseType.POSTGRESQL:
        pytest.skip('needs a dialect where one connection reaches two schemas by name')

    other = 'refs_other_{}'.format(uuid.uuid4().hex[:6])
    parent = _create(database, 'q_parent', 'id INT NOT NULL PRIMARY KEY')
    child = _create(database, 'q_child', 'id INT NOT NULL PRIMARY KEY, parent_id INT')
    database.alter('CREATE SCHEMA {}'.format(other))

    try:
        # The copy the job loads: same table names, in another schema, with an orphan.
        database.alter('CREATE TABLE {}.{} (id INT NOT NULL PRIMARY KEY)'.format(other, parent))
        database.alter('CREATE TABLE {}.{} (id INT NOT NULL PRIMARY KEY, parent_id INT)'.format(other, child))
        database.insert(table='{}.{}'.format(other, parent), data=[(1,)])
        database.insert(table='{}.{}'.format(other, child), data=[(10, 1), (11, 99)])
        # The connection's own schema holds a clean pair under the same names.
        database.insert(table=parent, data=[(1,)])
        database.insert(table=child, data=[(10, 1)])
        database.alter('ALTER TABLE {} ADD CONSTRAINT fk_{} FOREIGN KEY (parent_id) REFERENCES {} (id)'.format(child, child, parent))

        loaded = {table.upper(): '{}.{}'.format(other, table) for table in (parent, child)}
        results = verifyReferences(database, 'copy', loaded)

        assert [(result.table, result.orphans, result.problem) for result in results] == [
            ('{}.{}'.format(other, child), 1, None)]
    finally:
        database.alter('DROP SCHEMA {} CASCADE'.format(other))


def test_a_key_into_another_schema_keeps_that_schema(database):
    """Reported without it, the parent's name matched a different, same-named
    table: the count was wrong, and subset read the wrong table.
    """
    if database.type != DatabaseType.POSTGRESQL:
        pytest.skip('needs a dialect where one connection reaches two schemas by name')

    other = 'refs_other_{}'.format(uuid.uuid4().hex[:6])
    database.alter('CREATE SCHEMA {}'.format(other))

    try:
        database.alter('CREATE TABLE {}.elsewhere (id INT NOT NULL PRIMARY KEY)'.format(other))
        child = _create(database, 'x_child', 'id INT NOT NULL PRIMARY KEY, parent_id INT')
        # A different table of the same name in the connection's own schema.
        decoy = _create(database, 'elsewhere', 'id INT NOT NULL PRIMARY KEY')
        database.alter('ALTER TABLE {} ADD CONSTRAINT fk_{} FOREIGN KEY (parent_id) REFERENCES {}.elsewhere (id)'.format(child, child, other))
        database.insert(table='{}.elsewhere'.format(other), data=[(1,)])
        database.insert(table=child, data=[(10, 1)])

        (key,) = [foreignKey for foreignKey in database.getForeignKeys() if foreignKey.table.lower() == child.lower()]

        assert key.referencedTable.lower() == '{}.elsewhere'.format(other)
        assert [(result.orphans, result.problem) for result in verifyReferences(database, 'copy', {child.upper(): child})] == [(0, None)]
        assert decoy
    finally:
        database.alter('DROP SCHEMA {} CASCADE'.format(other))


def test_sql_server_sees_keys_outside_the_login_default_schema(database):
    """They were invisible, so subset silently dropped a qualified table's parents."""
    if database.type != DatabaseType.MSSQL:
        pytest.skip('about SQL Server schemas')

    other = 'refs_s_{}'.format(uuid.uuid4().hex[:6])
    database.alter('CREATE SCHEMA {}'.format(other))

    try:
        database.alter('CREATE TABLE {}.parent (id INT NOT NULL PRIMARY KEY)'.format(other))
        database.alter('CREATE TABLE {0}.child (id INT NOT NULL PRIMARY KEY, parent_id INT '
                       'CONSTRAINT fk_{1} FOREIGN KEY REFERENCES {0}.parent (id))'.format(other, uuid.uuid4().hex[:6]))

        keys = {(key.table.lower(), key.referencedTable.lower()) for key in database.getForeignKeys()}

        assert ('{}.child'.format(other), '{}.parent'.format(other)) in keys
    finally:
        database.alter('DROP TABLE {}.child'.format(other))
        database.alter('DROP TABLE {}.parent'.format(other))
        database.alter('DROP SCHEMA {}'.format(other))

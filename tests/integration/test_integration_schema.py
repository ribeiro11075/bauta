"""`schema` and `clear` across every pair of databases.

For each source and target -- SQLite plus the five servers, 36 pairs -- this
creates a parent and a child table on the source with that dialect's own
types, generates and applies the target's tables with `schema`, copies the rows
with a real data job, and compares the values. It's the only way to find the
problems that live between two drivers: a boolean one returns as an integer
that the other refuses, a date one returns as text that the other can't parse.

Then `clear` empties the target, children first, under a live foreign key, and
a swap through `schema`'s stage tables keeps the target's foreign keys.

Servers that aren't reachable are skipped. Run with `pytest -m integration`.
"""
import datetime
import decimal
import importlib
import json
import uuid

import pytest

from bauta.configuration import Configuration, connectionConfig, DatabaseType, DataJobsFile
from bauta.database import Database
from bauta.jobs.pipeline import _executeDataJob
from bauta.generate.schema import clearTables, createStatements, readTable
from tests.integration.servers import EMBEDDED, SERVERS

pytestmark = pytest.mark.integration

# Per source dialect: id, big, amount, ratio, name, code, body, born, seen, flag.
SOURCE_TYPES = {
    DatabaseType.SQLITE: ('INTEGER', 'BIGINT', 'DECIMAL(12,2)', 'REAL', 'VARCHAR(40)', 'CHAR(3)', 'TEXT', 'DATE', 'DATETIME', 'BOOLEAN'),
    DatabaseType.MYSQL: ('INT', 'BIGINT', 'DECIMAL(12,2)', 'DOUBLE', 'VARCHAR(40)', 'CHAR(3)', 'TEXT', 'DATE', 'DATETIME', 'BOOLEAN'),
    DatabaseType.MARIADB: ('INT', 'BIGINT', 'DECIMAL(12,2)', 'DOUBLE', 'VARCHAR(40)', 'CHAR(3)', 'TEXT', 'DATE', 'DATETIME', 'BOOLEAN'),
    DatabaseType.POSTGRESQL: ('INTEGER', 'BIGINT', 'NUMERIC(12,2)', 'DOUBLE PRECISION', 'VARCHAR(40)', 'CHAR(3)', 'TEXT', 'DATE', 'TIMESTAMP',
                              'BOOLEAN'),
    DatabaseType.ORACLE: ('NUMBER(10)', 'NUMBER(19)', 'NUMBER(12,2)', 'BINARY_DOUBLE', 'VARCHAR2(40)', 'CHAR(3)', 'CLOB', 'DATE', 'TIMESTAMP',
                          'NUMBER(1)'),
    DatabaseType.MSSQL: ('INT', 'BIGINT', 'DECIMAL(12,2)', 'FLOAT', 'NVARCHAR(40)', 'CHAR(3)', 'NVARCHAR(MAX)', 'DATE', 'DATETIME2', 'BIT'),
    DatabaseType.DUCKDB: ('INTEGER', 'BIGINT', 'DECIMAL(12,2)', 'DOUBLE', 'VARCHAR(40)', 'CHAR(3)', 'VARCHAR', 'DATE', 'TIMESTAMP', 'BOOLEAN'),
    }

COLUMNS = ('id', 'big', 'amount', 'ratio', 'name', 'code', 'body', 'born', 'seen', 'flag')

LONG_TEXT = 'a long body of text, ' * 300


def sourceRows(databaseType):
    """The same logical rows, in the Python types each driver accepts."""
    born, seen = datetime.date(2026, 1, 2), datetime.datetime(2026, 1, 2, 3, 4, 5)
    if databaseType == DatabaseType.SQLITE:
        born, seen = born.isoformat(), seen.isoformat(sep=' ')

    return [
        (1, 9000000000, decimal.Decimal('12.34'), 0.5, 'alpha', 'abc', LONG_TEXT, born, seen, True),
        (2, None, None, None, 'beta', None, None, None, None, False),
        ]


def normalized(row):
    """Values as comparable across drivers: dates as ISO text, flags as ints."""
    identifier, big, amount, ratio, name, code, body, born, seen, flag = row
    return (
        int(identifier),
        None if big is None else int(big),
        None if amount is None else decimal.Decimal(str(amount)).quantize(decimal.Decimal('0.01')),
        None if ratio is None else float(ratio),
        name,
        None if code is None else code.rstrip(),
        body,
        None if born is None else str(born)[:10],
        None if seen is None else str(seen)[:19].replace('T', ' '),
        int(flag),
        )


def connect(name, tmp_path_factory):
    if name == 'sqlite':
        return connectionConfig(type=DatabaseType.SQLITE, path=str(tmp_path_factory.mktemp('sqlite') / 'schema.db'))
    if name == 'duckdb':
        pytest.importorskip('duckdb')
        return connectionConfig(type=DatabaseType.DUCKDB, path=str(tmp_path_factory.mktemp('duckdb') / 'schema.duckdb'))

    driver, settings = SERVERS[name]
    try:
        importlib.import_module(driver)
        Database(connectionSettings=settings).close()
    except Exception as error:
        pytest.skip('{} is not available ({})'.format(name, error))

    return settings


NAMES = EMBEDDED + sorted(SERVERS)


@pytest.mark.parametrize('targetName', NAMES)
@pytest.mark.parametrize('sourceName', NAMES)
def test_schema_creates_target_tables_that_a_copy_loads_into(sourceName, targetName, tmp_path_factory):
    source = connect(sourceName, tmp_path_factory)
    target = connect(targetName, tmp_path_factory) if targetName != sourceName or sourceName != 'sqlite' else source
    suffix = uuid.uuid4().hex[:6]
    parent, child = 'sch_parent_{}'.format(suffix), 'sch_child_{}'.format(suffix)
    parentCopy, childCopy = parent + '_c', child + '_c'
    types = SOURCE_TYPES[source.type]

    with Database(connectionSettings=source) as database:
        database.alter('CREATE TABLE {} ({}, PRIMARY KEY (id))'.format(
            parent, ', '.join('{} {}{}'.format(column, columnType, ' NOT NULL' if column == 'name' else '')
                              for column, columnType in zip(COLUMNS, types))))
        database.alter('CREATE TABLE {} (id {} NOT NULL, parent_id {} NOT NULL, PRIMARY KEY (id), '
                       'CONSTRAINT fk_{} FOREIGN KEY (parent_id) REFERENCES {} (id))'.format(child, types[0], types[0], child, parent))
        database.insert(table=parent, data=sourceRows(source.type), chunkSize=10)
        database.insert(table=child, data=[(10, 1), (11, 1), (12, 2)], chunkSize=10)

        foreignKeys = database.getForeignKeys()
        definitions = [readTable(database, table, foreignKeys) for table in (child, parent)]

    # Copy under different names, so a same-server pair doesn't collide with
    # its own source tables.
    renamed = {parent.upper(): parentCopy, child.upper(): childCopy}
    definitions = [
        definition._replace(
            name=renamed[definition.name.upper()],
            foreignKeys=[foreignKey._replace(table=renamed[foreignKey.table.upper()], referencedTable=renamed[foreignKey.referencedTable.upper()],
                                             name=foreignKey.name + '_c')
                         for foreignKey in definition.foreignKeys])
        for definition in definitions
        ]
    statements = createStatements(source.type, target.type, definitions)

    try:
        with Database(connectionSettings=target) as database:
            for statement in statements:
                database.alter(statement.sql)

            assert database.tableExists(parentCopy) and database.tableExists(childCopy)
            assert [column.lower() for column in database.getPrimaryColumnNames(parentCopy)] == ['id']
            assert {foreignKey.referencedTable.lower() for foreignKey in database.getForeignKeys()
                    if foreignKey.table.lower() == childCopy.lower()} == {parentCopy.lower()}

        databases = {'source': source, 'target': target}
        for table, copy in ((parent, parentCopy), (child, childCopy)):
            job = Configuration.validateJobConfiguration({'workers': 1, 'jobs': {'copy': {
                'active': True, 'sourceConnection': 'source', 'targetConnection': 'target', 'sourceQuery': 'SELECT * FROM {}'.format(table),
                'targetTableFinal': copy, 'insertStrategy': 'upsert', 'chunkSize': 10}}}, DataJobsFile).jobs['copy']
            _executeDataJob('copy', job, databases)

        with Database(connectionSettings=target) as database:
            copied = [normalized(row) for row in database.query('SELECT {} FROM {} ORDER BY id'.format(', '.join(COLUMNS), parentCopy))]
            assert copied == [normalized(row) for row in sourceRows(source.type)]
            assert database.query('SELECT count(*) FROM {}'.format(childCopy))[0][0] == 3

            cleared = clearTables(database, [parentCopy, childCopy])
            assert [table.lower() for table, _ in cleared] == [childCopy.lower(), parentCopy.lower()]
            assert database.query('SELECT count(*) FROM {}'.format(parentCopy))[0][0] == 0
    finally:
        for settings, tables in ((target, (childCopy, parentCopy)), (source, (child, parent))):
            with Database(connectionSettings=settings) as database:
                for table in tables:
                    database.alter('DROP TABLE IF EXISTS {}'.format(table))


@pytest.mark.parametrize('name', NAMES)
def test_clear_rolls_back_when_a_table_outside_the_set_still_references_it(name, tmp_path_factory):
    """One transaction: a referencing table that isn't being cleared blocks the
    parent's DELETE, and the child's DELETE before it is undone too -- except
    on DuckDB, which can't clear a parent and child in one.
    """
    settings = connect(name, tmp_path_factory)
    suffix = uuid.uuid4().hex[:6]
    parent, child, other = 'clr_parent_{}'.format(suffix), 'clr_child_{}'.format(suffix), 'clr_other_{}'.format(suffix)

    with Database(connectionSettings=settings) as database:
        database.alter('CREATE TABLE {} (id INT PRIMARY KEY)'.format(parent))
        for table in (child, other):
            database.alter('CREATE TABLE {0} (id INT PRIMARY KEY, parent_id INT, CONSTRAINT fk_{0} FOREIGN KEY (parent_id) REFERENCES {1} (id))'.format(
                table, parent))
        database.insert(table=parent, data=[(1,)])
        database.insert(table=child, data=[(1, 1)])
        database.insert(table=other, data=[(1, 1)])

        try:
            with pytest.raises(Exception):
                clearTables(database, [parent, child])

            # DuckDB clears a table per transaction (see clearTables), so the
            # child it reached first stays empty.
            assert database.query('SELECT count(*) FROM {}'.format(child))[0][0] == (0 if settings.type == DatabaseType.DUCKDB else 1)
            assert database.query('SELECT count(*) FROM {}'.format(parent))[0][0] == 1
        finally:
            for table in (child, other, parent):
                database.alter('DROP TABLE IF EXISTS {}'.format(table))


@pytest.mark.parametrize('name', NAMES)
def test_schema_applies_a_key_to_a_unique_column_and_constraint_names_that_repeat(name, tmp_path_factory):
    """Two shapes that left half a schema behind: a foreign key to a UNIQUE
    column that isn't the primary key, which `subset --root products` makes,
    and two tables whose source constraints share a name, which MySQL,
    MariaDB, Oracle and SQL Server require to be unique across the schema.
    """
    settings = connect(name, tmp_path_factory)
    suffix = uuid.uuid4().hex[:6]
    parent, first, second = 'unq_p_{}'.format(suffix), 'unq_a_{}'.format(suffix), 'unq_b_{}'.format(suffix)
    copies = {table.upper(): table + '_c' for table in (parent, first, second)}
    integer, text = SOURCE_TYPES[settings.type][0], SOURCE_TYPES[settings.type][4]

    try:
        with Database(connectionSettings=settings) as database:
            database.alter('CREATE TABLE {} (id {} NOT NULL, code {} NOT NULL, PRIMARY KEY (id), UNIQUE (code))'.format(parent, integer, text))
            for child in (first, second):
                database.alter('CREATE TABLE {0} (id {1} NOT NULL, code {2} NOT NULL, PRIMARY KEY (id), '
                               'CONSTRAINT fk_{0} FOREIGN KEY (code) REFERENCES {3} (code))'.format(child, integer, text, parent))

            foreignKeys = database.getForeignKeys()
            # The same constraint name on both children, as two PostgreSQL or
            # SQLite tables may well have.
            definitions = [
                definition._replace(name=copies[definition.name.upper()], foreignKeys=[
                    foreignKey._replace(table=copies[foreignKey.table.upper()], referencedTable=copies[foreignKey.referencedTable.upper()],
                                        name='fk_shared_{}'.format(suffix))
                    for foreignKey in definition.foreignKeys])
                for definition in (readTable(database, table, foreignKeys) for table in (parent, first, second))
                ]

            for statement in createStatements(settings.type, settings.type, definitions):
                database.alter(statement.sql)

            database.insert(table=copies[parent.upper()], data=[(1, 'abc')])
            database.insert(table=copies[first.upper()], data=[(1, 'abc')])
            database.insert(table=copies[second.upper()], data=[(2, 'abc')])

            children = {copies[first.upper()].lower(), copies[second.upper()].lower()}
            declared = [foreignKey for foreignKey in database.getForeignKeys() if foreignKey.table.lower() in children]

            assert len(declared) == 2
            assert len({foreignKey.name.upper() for foreignKey in declared}) == 2
    finally:
        with Database(connectionSettings=settings) as database:
            for table in list(copies.values())[::-1] + [second, first, parent]:
                database.alter('DROP TABLE IF EXISTS {}'.format(table))


def _copyJob(sourceQuery, final, **fields):
    job = {'active': True, 'sourceConnection': 'db', 'targetConnection': 'db', 'sourceQuery': sourceQuery, 'targetTableFinal': final,
           'insertStrategy': 'upsert', 'chunkSize': 10}
    job.update(fields)
    return Configuration.validateJobConfiguration({'workers': 1, 'jobs': {'copy': job}}, DataJobsFile).jobs['copy']


@pytest.mark.parametrize('name', NAMES)
def test_a_swap_through_schema_stage_tables_loads_and_drops_the_targets_keys(name, tmp_path_factory):
    """Stage tables carry no foreign keys, so the load always completes, and
    the live table's keys alternate: the former stage declares none, the
    original still does. `audit --connect` reports it. A stage that carried
    them would fail as soon as its parent was swapped too.
    """
    settings = connect(name, tmp_path_factory)
    if settings.type == DatabaseType.DUCKDB:
        pytest.skip('DuckDB refuses to swap a table in a foreign key; see test_integration_duckdb.py')
    suffix = uuid.uuid4().hex[:6]
    parent, child = 'stg_parent_{}'.format(suffix), 'stg_child_{}'.format(suffix)
    parentCopy, childCopy, childStage = parent + '_c', child + '_c', child + '_c_s'
    integer = SOURCE_TYPES[settings.type][0]
    tables = (childStage, childCopy, parentCopy, child, parent)

    try:
        with Database(connectionSettings=settings) as database:
            database.alter('CREATE TABLE {} (id {} NOT NULL, PRIMARY KEY (id))'.format(parent, integer))
            database.alter('CREATE TABLE {0} (id {1} NOT NULL, parent_id {1}, PRIMARY KEY (id), '
                           'CONSTRAINT fk_{0} FOREIGN KEY (parent_id) REFERENCES {2} (id))'.format(child, integer, parent))
            database.insert(table=parent, data=[(1,), (2,)])
            database.insert(table=child, data=[(10, 1), (11, 2)])

            foreignKeys = database.getForeignKeys()
            renamed = {parent.upper(): parentCopy, child.upper(): childCopy}
            definitions = [
                definition._replace(name=renamed[definition.name.upper()], foreignKeys=[
                    foreignKey._replace(table=renamed[foreignKey.table.upper()], referencedTable=renamed[foreignKey.referencedTable.upper()],
                                        name=foreignKey.name + '_c')
                    for foreignKey in definition.foreignKeys])
                for definition in (readTable(database, table, foreignKeys) for table in (parent, child))
                ]
            for statement in createStatements(settings.type, settings.type, definitions, stageSuffix='_s'):
                database.alter(statement.sql)

        def referenced(table):
            with Database(connectionSettings=settings) as database:
                return {foreignKey.referencedTable.lower() for foreignKey in database.getForeignKeys() if foreignKey.table.lower() == table.lower()}

        def count(table):
            with Database(connectionSettings=settings) as database:
                return database.query('SELECT count(*) FROM {}'.format(table))[0][0]

        assert referenced(childCopy) == {parentCopy.lower()}
        assert referenced(childStage) == set()

        databases = {'db': settings}
        _executeDataJob('copy', _copyJob('SELECT id FROM {}'.format(parent), parentCopy), databases)
        swapChild = _copyJob('SELECT id, parent_id FROM {}'.format(child), childCopy, insertStrategy='swap', targetTableStage=childStage)

        # Both runs load. The live table alternates between the former stage,
        # which declares no keys, and the original, which does.
        for expected in (set(), {parentCopy.lower()}):
            _executeDataJob('copy', swapChild, databases)
            assert count(childCopy) == 2
            assert referenced(childCopy) == expected
    finally:
        with Database(connectionSettings=settings) as database:
            for table in tables:
                database.alter('DROP TABLE IF EXISTS {}'.format(table))


@pytest.mark.parametrize('name', NAMES)
def test_schema_keeps_text_keys_that_differ_only_in_case_apart(name, tmp_path_factory):
    """MySQL, MariaDB and SQL Server compare text loosely by default: `a` and
    `A`, and `ss` and the German sharp s, are one value, so four source keys
    became two rows in the copy -- or a primary-key violation once a chunk held
    both. A key column is created with a collation that compares exactly, on
    both sides of a foreign key, which the two must share.
    """
    settings = connect(name, tmp_path_factory)
    suffix = uuid.uuid4().hex[:6]
    parent, child = 'coll_p_{}'.format(suffix), 'coll_c_{}'.format(suffix)
    copies = {table.upper(): table + '_c' for table in (parent, child)}
    integer, text = SOURCE_TYPES[settings.type][0], SOURCE_TYPES[settings.type][4]
    keys = [('a',), ('A',), ('ss',), ('ß',)]

    try:
        with Database(connectionSettings=settings) as database:
            database.alter('CREATE TABLE {} (code {} NOT NULL, PRIMARY KEY (code))'.format(parent, text))
            database.alter('CREATE TABLE {0} (id {1} NOT NULL, code {2} NOT NULL, PRIMARY KEY (id), '
                           'CONSTRAINT fk_{0} FOREIGN KEY (code) REFERENCES {3} (code))'.format(child, integer, text, parent))

            foreignKeys = database.getForeignKeys()
            definitions = [
                definition._replace(name=copies[definition.name.upper()], foreignKeys=[
                    foreignKey._replace(table=copies[foreignKey.table.upper()], referencedTable=copies[foreignKey.referencedTable.upper()],
                                        name='fk_copy_{}'.format(suffix))
                    for foreignKey in definition.foreignKeys])
                for definition in (readTable(database, table, foreignKeys) for table in (parent, child))
                ]

            for statement in createStatements(settings.type, settings.type, definitions):
                database.alter(statement.sql)

            database.insert(table=copies[parent.upper()], data=keys)

            assert len(database.query('SELECT code FROM {}'.format(database.statementName(copies[parent.upper()])))) == len(keys)
    finally:
        with Database(connectionSettings=settings) as database:
            for table in [copies[child.upper()], copies[parent.upper()], child, parent]:
                database.alter('DROP TABLE IF EXISTS {}'.format(table))


@pytest.mark.parametrize('targetName', NAMES)
def test_duckdbs_own_types_copy_into_every_database(targetName, tmp_path_factory):
    """DuckDB's LIST, STRUCT and MAP, which `schema` maps to JSON, reached the
    other drivers as Python lists and dicts, which none of them could bind:
    the copy failed at its first chunk on all six. A UUID failed MySQL and
    MariaDB, a TIME Oracle, and an integer past 64 bits SQLite -- as they did
    from PostgreSQL. Each now arrives as whatever the target's column holds.
    """
    source = connect('duckdb', tmp_path_factory)
    target = connect(targetName, tmp_path_factory)
    suffix = uuid.uuid4().hex[:6]
    table, copy = 'zoo_{}'.format(suffix), 'zoo_{}_c'.format(suffix)

    with Database(connectionSettings=source) as database:
        database.alter('CREATE TABLE {} (id INTEGER PRIMARY KEY, tags VARCHAR[], props STRUCT(a INTEGER, b VARCHAR), counts MAP(VARCHAR, INTEGER), '
                       'big HUGEINT, key UUID, clock TIME, moment TIMESTAMPTZ)'.format(table))
        database.alter("INSERT INTO {} VALUES (1, ['a', 'b'], {{'a': 1, 'b': 'q'}}, MAP {{'k': 1}}, '1000000000000000000000000000000', "
                       "'00000000-0000-0000-0000-000000000001', '01:02:03', '2026-01-02 03:04:05+00'), "
                       "(2, NULL, NULL, NULL, NULL, NULL, NULL, NULL)".format(table))
        definition = readTable(database, table, database.getForeignKeys())

    try:
        with Database(connectionSettings=target) as database:
            for statement in createStatements(source.type, target.type, [definition._replace(name=copy)]):
                database.alter(statement.sql)

        job = Configuration.validateJobConfiguration({'jobs': {'copy': {
            'sourceConnection': 'source', 'targetConnection': 'target', 'sourceQuery': 'SELECT * FROM {}'.format(table),
            'targetTableFinal': copy, 'insertStrategy': 'upsert', 'unmasked': True}}}, DataJobsFile).jobs['copy']
        _executeDataJob('copy', job, {'source': source, 'target': target})

        with Database(connectionSettings=target) as database:
            rows = database.query('SELECT * FROM {} ORDER BY 1'.format(copy))
    finally:
        with Database(connectionSettings=target) as database:
            database.alter('DROP TABLE IF EXISTS {}'.format(copy))

    identifier, tags, props, counts, big, key, clock, moment = rows[0]
    asJson = lambda value: value if isinstance(value, (list, dict)) else json.loads(value)  # noqa: E731
    assert (asJson(tags), asJson(props), asJson(counts)) == (['a', 'b'], {'a': 1, 'b': 'q'}, {'k': 1})
    assert int(big) == 10 ** 30
    assert str(key) == '00000000-0000-0000-0000-000000000001'
    assert str(clock).startswith('01:02:03') or clock == datetime.timedelta(hours=1, minutes=2, seconds=3)
    assert str(moment)[:19].replace('T', ' ') == '2026-01-02 03:04:05'
    assert rows[1][1:] == (None,) * 7


# The types the first matrix leaves out, which is where copies between
# databases broke: each source's own UUID, time of day, JSON, binary, instant
# and unsigned 64-bit integer, where it has one. (column, DDL per source,
# value per source, how to compare what a target returns).
_KEY = uuid.UUID('00000000-0000-0000-0000-00000000abcd')
_INSTANT = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
_DOCUMENT = {'a': [1, 2], 'b': 'text'}

AWKWARD = [
    ('ref_key', {DatabaseType.POSTGRESQL: 'UUID', DatabaseType.MSSQL: 'UNIQUEIDENTIFIER', DatabaseType.DUCKDB: 'UUID',
             DatabaseType.MYSQL: 'CHAR(36)', DatabaseType.MARIADB: 'UUID', DatabaseType.ORACLE: 'VARCHAR2(36)', DatabaseType.SQLITE: 'TEXT'},
     lambda databaseType: _KEY if databaseType in (DatabaseType.POSTGRESQL, DatabaseType.MSSQL, DatabaseType.DUCKDB, DatabaseType.MARIADB) else str(_KEY),
     lambda value: str(value).lower()),
    ('clock_value', {DatabaseType.POSTGRESQL: 'TIME', DatabaseType.MSSQL: 'TIME', DatabaseType.DUCKDB: 'TIME', DatabaseType.MYSQL: 'TIME',
               DatabaseType.MARIADB: 'TIME'},
     lambda databaseType: datetime.time(1, 2, 3),
     lambda value: str(value)[:8].zfill(8)),
    ('doc_value', {DatabaseType.POSTGRESQL: 'JSONB', DatabaseType.MYSQL: 'JSON', DatabaseType.MARIADB: 'JSON', DatabaseType.DUCKDB: 'JSON',
             DatabaseType.MSSQL: 'NVARCHAR(MAX)', DatabaseType.ORACLE: 'CLOB', DatabaseType.SQLITE: 'TEXT'},
     lambda databaseType: _DOCUMENT if databaseType == DatabaseType.POSTGRESQL else json.dumps(_DOCUMENT),
     lambda value: value if isinstance(value, dict) else json.loads(value)),
    ('bin_value', {DatabaseType.POSTGRESQL: 'BYTEA', DatabaseType.MYSQL: 'BLOB', DatabaseType.MARIADB: 'BLOB', DatabaseType.DUCKDB: 'BLOB',
              DatabaseType.MSSQL: 'VARBINARY(MAX)', DatabaseType.ORACLE: 'BLOB', DatabaseType.SQLITE: 'BLOB'},
     lambda databaseType: b'\x00\x01\xfe\xff',
     lambda value: bytes(value)),
    ('instant_value', {DatabaseType.POSTGRESQL: 'TIMESTAMPTZ', DatabaseType.MSSQL: 'DATETIMEOFFSET', DatabaseType.DUCKDB: 'TIMESTAMPTZ',
                 DatabaseType.ORACLE: 'TIMESTAMP WITH TIME ZONE'},
     lambda databaseType: _INSTANT,
     lambda value: (value.astimezone(datetime.timezone.utc).replace(tzinfo=None) if getattr(value, 'tzinfo', None)
                    else datetime.datetime.fromisoformat(str(value)).astimezone(datetime.timezone.utc).replace(tzinfo=None)
                    if isinstance(value, str) and ('+' in value or value.endswith('Z')) else
                    datetime.datetime.fromisoformat(str(value)))),
    ('big_unsigned', {DatabaseType.MYSQL: 'BIGINT UNSIGNED', DatabaseType.MARIADB: 'BIGINT UNSIGNED', DatabaseType.DUCKDB: 'UBIGINT'},
     lambda databaseType: 2 ** 64 - 1,
     lambda value: int(value)),
    ]


@pytest.mark.parametrize('targetName', NAMES)
@pytest.mark.parametrize('sourceName', NAMES)
def test_awkward_types_copy_between_every_pair(sourceName, targetName, tmp_path_factory):
    """Each pair's `schema` creates the target, and a copy loads the source's
    own UUID, time of day, JSON, binary, instant and unsigned 64-bit integer
    into it, to come back as the same values. The first matrix's tamer types
    left four breaks unseen: a UUID into MySQL, a time into Oracle, an
    unsigned BIGINT into SQLite, and JSON arrays into anything but PostgreSQL.
    """
    source = connect(sourceName, tmp_path_factory)
    target = connect(targetName, tmp_path_factory) if targetName != sourceName or sourceName not in EMBEDDED else source
    columns = [(name, ddl[source.type], valueFor(source.type), compare) for name, ddl, valueFor, compare in AWKWARD if source.type in ddl]
    suffix = uuid.uuid4().hex[:6]
    table, copy = 'awk_{}'.format(suffix), 'awk_{}_c'.format(suffix)

    try:
        with Database(connectionSettings=source) as database:
            database.alter('CREATE TABLE {} (id INTEGER NOT NULL PRIMARY KEY, {})'.format(
                table, ', '.join('{} {}'.format(name, ddl) for name, ddl, _, _ in columns)))
            database.insert(table=table, data=[tuple([1] + [value for _, _, value, _ in columns]), tuple([2] + [None] * len(columns))])
            definition = readTable(database, table, [])

        with Database(connectionSettings=target) as database:
            for statement in createStatements(source.type, target.type, [definition._replace(name=copy)]):
                database.alter(statement.sql)

        job = Configuration.validateJobConfiguration({'jobs': {'copy': {
            'sourceConnection': 'source', 'targetConnection': 'target', 'sourceQuery': 'SELECT * FROM {} ORDER BY id'.format(table),
            'targetTableFinal': copy, 'insertStrategy': 'upsert', 'unmasked': True}}}, DataJobsFile).jobs['copy']
        _executeDataJob('copy', job, {'source': source, 'target': target})

        with Database(connectionSettings=target) as database:
            first, second = database.query('SELECT * FROM {} ORDER BY 1'.format(copy))
    finally:
        for settings, name in ((target, copy), (source, table)):
            with Database(connectionSettings=settings) as database:
                database.alter('DROP TABLE IF EXISTS {}'.format(name) if settings.type != DatabaseType.ORACLE else
                               "BEGIN EXECUTE IMMEDIATE 'DROP TABLE {}'; EXCEPTION WHEN OTHERS THEN NULL; END;".format(name))

    for (name, _, value, compare), copied in zip(columns, first[1:]):
        assert compare(copied) == compare(value), '{} came back as {!r}'.format(name, copied)
    assert list(second[1:]) == [None] * len(columns)

"""Primary keys, upserts and swaps against real servers, where the catalogs differ.

Regression tests for three ways the key lookup used to go wrong, each silently:

- a same-named table in another schema added its key columns, so an upsert
  matched on the wrong columns and left some unwritten;
- UNIQUE columns counted as key columns, so an upsert that changed one failed
  (Oracle) or was refused outright (PostgreSQL);
- a key-only table on MySQL used INSERT IGNORE, which also swallows real errors.

And for swap with schema-qualified names, which renames used to reject, and
with a table other tables reference, whose keys stay on the old table.

Parametrized over the five servers in docker-compose.yml; any that isn't
reachable, or whose driver isn't installed, is skipped with a reason. Run with
`pytest -m integration`.
"""
import datetime
import decimal
import uuid

import pytest

from bauta.configuration import ConfigurationError
from bauta.database import Database
from tests.integration.servers import SERVERS, serverSettings

pytestmark = pytest.mark.integration

# How to create and drop a second schema on each server, from the connection's
# own. Oracle's schemas are users.
SCHEMAS = {
    'mysql': ('CREATE DATABASE {0}', 'DROP DATABASE IF EXISTS {0}'),
    'mariadb': ('CREATE DATABASE {0}', 'DROP DATABASE IF EXISTS {0}'),
    'postgresql': ('CREATE SCHEMA {0}', 'DROP SCHEMA IF EXISTS {0} CASCADE'),
    'mssql': ('CREATE SCHEMA {0}', 'DROP SCHEMA IF EXISTS {0}'),
    'oracle': ('CREATE USER {0} IDENTIFIED BY "Pw{0}" QUOTA UNLIMITED ON USERS', 'DROP USER {0} CASCADE'),
    }


@pytest.fixture(params=sorted(SERVERS))
def server(request):
    settings = serverSettings(request.param)
    database = Database(connectionSettings=settings)

    created = []

    def table(definition: str, schema: str = '') -> str:
        name = '{}t_{}'.format(schema + '.' if schema else '', uuid.uuid4().hex[:8])
        database.alter('CREATE TABLE {} {}'.format(name, definition))
        created.append(name)
        return name

    table.created = created  # type: ignore[attr-defined]

    yield request.param, database, table

    _dropTables(database, created)
    database.close()


def _dropTables(database: Database, tables):
    while tables:
        name = tables.pop()
        try:
            database.alter('DROP TABLE {}'.format(name))
        except Exception:
            database.connection.rollback()


@pytest.fixture
def otherSchema(server):
    serverName, database, _ = server
    name = 'other_{}'.format(uuid.uuid4().hex[:6])
    create, drop = SCHEMAS[serverName]
    database.alter(create.format(name))

    yield name

    # Its tables go first: SQL Server won't drop a schema that still has any.
    created = server[2].created
    inSchema = [table for table in created if table.startswith(name + '.')]
    created[:] = [table for table in created if table not in inSchema]
    _dropTables(database, inSchema)
    database.alter(drop.format(name))


def _rows(database: Database, table: str):
    return sorted(tuple(row) for row in database.query('SELECT * FROM {}'.format(table)))


def test_a_same_named_table_in_another_schema_does_not_lend_its_key(server, otherSchema):
    serverName, database, table = server
    local = table('(a INT PRIMARY KEY, b INT, c INT)')
    unqualified = local.rpartition('.')[2]
    elsewhere = '{}.{}'.format(otherSchema, unqualified)
    database.alter('CREATE TABLE {} (a INT, b INT, c INT, PRIMARY KEY (a, b))'.format(elsewhere))

    try:
        assert [column.lower() for column in database.getPrimaryColumnNames(local)] == ['a']
        assert [column.lower() for column in database.getPrimaryColumnNames(elsewhere)] == ['a', 'b']

        database.upsert(table=local, data=[(1, 2, 3)])
        database.upsert(table=local, data=[(1, 9, 9)])

        assert _rows(database, local) == [(1, 9, 9)]
    finally:
        database.alter('DROP TABLE {}'.format(elsewhere))


def test_a_unique_column_is_not_part_of_the_upsert_key(server):
    _, database, table = server
    people = table('(id INT PRIMARY KEY, email VARCHAR(50) UNIQUE, name VARCHAR(50))')

    assert [column.lower() for column in database.getPrimaryColumnNames(people)] == ['id']

    database.upsert(table=people, data=[(1, 'a@example.test', 'Ann')])
    database.upsert(table=people, data=[(1, 'b@example.test', 'Ann')])

    assert _rows(database, people) == [(1, 'b@example.test', 'Ann')]


def test_a_row_matching_another_by_a_unique_key_is_refused_rather_than_merged_into_it(server):
    """A new row (id 2) carrying an email row 1 already had: every database
    refused it but MySQL and MariaDB, whose ON DUPLICATE KEY fires on any
    unique key and so overwrote row 1 with row 2's values, dropping row 2
    without an error. A masked email that collided did exactly this.
    """
    _, database, table = server
    people = table('(id INT PRIMARY KEY, email VARCHAR(50) UNIQUE, name VARCHAR(50))')
    stage = table('(id INT, email VARCHAR(50), name VARCHAR(50))')
    database.upsert(table=people, data=[(1, 'a@example.test', 'Ann')])

    with pytest.raises(Exception):
        database.upsert(table=people, data=[(2, 'a@example.test', 'Bob')])
    database.rollback()
    database.insert(table=stage, data=[(2, 'a@example.test', 'Bob')])
    with pytest.raises(Exception):
        database.upsertFromStage(targetTable=people, stageTable=stage)
    database.rollback()

    assert _rows(database, people) == [(1, 'a@example.test', 'Ann')]


def test_a_key_only_table_upserts_idempotently(server):
    _, database, table = server
    links = table('(a INT, b INT, PRIMARY KEY (a, b))')
    stage = table('(a INT, b INT)')
    database.insert(table=stage, data=[(1, 2), (3, 4)])

    for _ in range(2):
        database.upsert(table=links, data=[(1, 2)])
        database.upsertFromStage(targetTable=links, stageTable=stage)

    assert _rows(database, links) == [(1, 2), (3, 4)]


def test_a_key_only_upsert_still_reports_real_errors(server):
    """INSERT IGNORE turned a NULL key into a warning and a silently altered row."""
    _, database, table = server
    links = table('(a INT, b INT, PRIMARY KEY (a, b))')

    with pytest.raises(Exception):
        database.upsert(table=links, data=[(1, None)])


def test_an_upsert_into_a_table_without_a_primary_key_is_refused(server):
    _, database, table = server
    keyless = table('(id INT, name VARCHAR(50))')

    with pytest.raises(ConfigurationError, match='no primary key'):
        database.upsert(table=keyless, data=[(1, 'Ann')])


def test_swap_works_with_schema_qualified_names(server, otherSchema):
    _, database, table = server
    final = table('(id INT PRIMARY KEY)', schema=otherSchema)
    stage = table('(id INT PRIMARY KEY)', schema=otherSchema)
    database.insert(table=final, data=[(1,)])
    database.insert(table=stage, data=[(2,), (3,)])

    database.swap(targetTable=final, stageTable=stage)

    assert _rows(database, final) == [(2,), (3,)]
    assert _rows(database, stage) == [(1,)]
    assert database.tableExists(final)
    assert not database.tableExists('{}.{}_tmp'.format(otherSchema, final.rpartition('.')[2]))


def test_a_swap_leaves_other_tables_keys_on_the_old_table(server):
    """What audit's swap check rests on: a key referencing the target moves
    with the old table to the stage's name, and the next run can't empty it.
    """
    from bauta.review.audit import ConnectedFacts, auditJobs
    from bauta.configuration import DataJobConfig

    _, database, table = server
    final = table('(id INT PRIMARY KEY)')
    stage = table('(id INT PRIMARY KEY)')
    child = table('(id INT PRIMARY KEY, parent_id INT, FOREIGN KEY (parent_id) REFERENCES {}(id))'.format(final))
    database.insert(table=final, data=[(1,)])
    database.insert(table=child, data=[(10, 1)])

    def referenced():
        return {foreignKey.referencedTable.lower() for foreignKey in database.getForeignKeys() if foreignKey.table.lower() == child.lower()}

    def errors():
        jobs = {'loadParent': DataJobConfig(active=True, sourceConnection='prod', sourceQuery='select * from parent', targetConnection='copy',
                                            targetTableStage=stage, targetTableFinal=final, insertStrategy='swap', chunkSize=10)}
        return [finding['message'] for finding in auditJobs(jobs, ConnectedFacts(declaredForeignKeys={'copy': database.getForeignKeys()}))['findings']
                if finding['severity'] == 'error']

    assert referenced() == {final.lower()}
    (before,) = errors()
    assert 'which loadParent replaces by swap' in before

    database.swap(targetTable=final, stageTable=stage)

    assert referenced() == {stage.lower()}
    (after,) = errors()
    assert 'the stage table of loadParent, as an earlier swap leaves it' in after

    # Oracle names no foreign key, only its own ORA-02266.
    with pytest.raises(Exception, match='(?i)foreign key|ORA-02266'):
        database.truncate(stage)
    database.connection.rollback()


def test_database_history_and_key_fingerprints_work_on_every_server(server):
    """The history table's types, and fingerprint rows in the memory table,
    have to be accepted -- and read back -- by every dialect.
    """
    from bauta.jobs.dependencyGraph import JobOutcome, JobStatus
    from bauta.jobs.memory import DATABASE_MEMORY_SCHEMA, DatabaseMemory
    from bauta.jobs.reporting import DATABASE_HISTORY_SCHEMA, DatabaseHistory
    from bauta.jobs.runner import RunResult

    serverName, database, table = server
    historyTable = table(DATABASE_HISTORY_SCHEMA.split('bauta_history', 1)[1])
    memoryTable = table(DATABASE_MEMORY_SCHEMA.split('bauta_memory', 1)[1])
    settings = database.connectionSettings

    history = DatabaseHistory(settings, table=historyTable)
    history.append(RunResult(outcomes=[JobOutcome(job='a', status=JobStatus.FAILED, error='x' * 3000, startedAt=1.0e9, finishedAt=1.0e9 + 2.5),
                                       JobOutcome(job='b', status=JobStatus.SKIPPED),
                                       JobOutcome(job='c', status=JobStatus.COMPLETED, rowCount=7, startedAt=1.0e9, finishedAt=1.0e9 + 1,
                                                  stages={'read': 0.25, 'mask': 0.5, 'write': 0.125, 'throttled': 0.0})]), 'run-1')
    records = history.read(limit=5)

    assert {record['job'] for record in records} == {'a', 'b', 'c'}
    completed = next(record for record in records if record['job'] == 'c')
    assert [completed[field] for field in ('readSeconds', 'maskSeconds', 'writeSeconds', 'throttledSeconds')] == [0.25, 0.5, 0.125, 0.0]
    assert all(type(completed[field]) is float for field in ('readSeconds', 'throttledSeconds'))
    assert next(record for record in records if record['job'] == 'b')['readSeconds'] is None
    failed = next(record for record in records if record['job'] == 'a')
    assert failed['durationSeconds'] == 2.5 and len(failed['error']) == 2000
    assert type(failed['rowCount']) is int and type(failed['attempts']) is int
    assert [record['job'] for record in history.read(job='b')] == ['b']

    with DatabaseMemory(settings, table=memoryTable) as memory:
        memory.recordRun('a')
        memory.recordWatermark('a', 5)
        memory.recordKeyFingerprint('a', 'abc123')

        assert memory.readKeyFingerprints() == {'a': 'abc123'}
        assert memory.readWatermarks() == {'a': 5}

        watermarks = {'bytes': b'\x00\x00\x07\xd1', 'decimal': decimal.Decimal('12.50'), 'time': datetime.time(10, 30, 5)}
        for job, value in watermarks.items():
            memory.recordWatermark(job, value)
        read = memory.readWatermarks()
        assert {job: (type(read[job]), read[job]) for job in watermarks} == {job: (type(value), value) for job, value in watermarks.items()}
        assert set(memory.read()) == {'a'}


def test_a_manifest_stored_in_a_table_comes_back_intact_on_every_server(server):
    """Stored in pieces of the table's VARCHAR, which every dialect has to hand
    back byte for byte -- a piece that ends in spaces included -- or the
    digest stops matching.
    """
    from bauta.masking import sealManifest, verifyManifest
    from bauta.jobs.reporting import DATABASE_MANIFEST_SCHEMA, MANIFEST_PART_LENGTH, DatabaseManifests

    _, database, table = server
    manifests = DatabaseManifests(database.connectionSettings, table=table(DATABASE_MANIFEST_SCHEMA.split('bauta_manifest', 1)[1]))
    columns = [{'column': 'c{}'.format(index), 'strategy': 'key', 'note': 'é ' * 5} for index in range(60)]
    first = sealManifest({'jobs': [{'job': 'first', 'columns': columns}]}, signingKey='an-integration-signing-key')
    second = sealManifest({'jobs': [{'job': 'second', 'columns': columns[:1]}]})

    manifests.write(first, 'run-1')
    manifests.write(second, 'run-2')

    assert len(database.query('SELECT part FROM {} WHERE run_id = \'run-1\''.format(manifests.table))) > 2
    assert manifests.read('run-1') == ('run-1', first)
    assert manifests.read() == ('run-2', second)
    assert verifyManifest(manifests.read('run-1')[1], signingKey='an-integration-signing-key').signatureValid
    assert MANIFEST_PART_LENGTH == 2000


def test_a_reserved_word_column_loads_and_upserts(server):
    """`rank` and `order` are reserved on at least one server each; the column
    is created as `bauta schema` would, and loaded through both paths.
    """
    from bauta.database.dialects import quoteFolded

    name, database, table = server
    quoted = {column: quoteFolded(database.type, column) for column in ('id', 'rank', 'order')}
    target = table('({id} INT PRIMARY KEY, {rank} INT, {order} VARCHAR(20))'.format(**quoted))

    database.insert(table=target, data=[(1, 10, 'a'), (2, 20, 'b')], chunkSize=10, columns=['id', 'rank', 'order'])
    database.upsert(table=target, data=[(2, 21, 'B'), (3, 30, 'c')], chunkSize=10, columns=['ID', 'Rank', 'order'])

    rows = database.query('SELECT {id}, {rank}, {order} FROM {table} ORDER BY {id}'.format(table=target, **quoted))
    assert [tuple(row) for row in rows] == [(1, 10, 'a'), (2, 21, 'B'), (3, 30, 'c')]


def test_a_mixed_case_column_created_quoted_loads(server):
    """On Oracle and PostgreSQL, a column created as "CustomerId" only answers
    to that exact spelling; an unquoted load used to miss it.
    """
    from bauta.database.dialects import quoteIdentifier

    name, database, table = server
    column = quoteIdentifier(database.type, 'CustomerId')
    target = table('(id INT PRIMARY KEY, {} INT)'.format(column))

    database.upsert(table=target, data=[(1, 5)], chunkSize=10, columns=['id', 'customerid'])

    assert [tuple(row) for row in database.query('SELECT id, {} FROM {}'.format(column, target))] == [(1, 5)]


def test_foreign_keys_are_read_from_the_schema_named(server, otherSchema):
    """Every dialect read the connection's own schema's keys whatever was
    asked, so discover --schema, schema --schema and verify-references on
    `other.orders` found none. Named as listTables(schema) names tables.
    """
    serverName, database, table = server
    parent = table('(id INT PRIMARY KEY)', schema=otherSchema)
    child = table('(id INT PRIMARY KEY, parent_id INT, FOREIGN KEY (parent_id) REFERENCES {}(id))'.format(parent), schema=otherSchema)

    def pairs(keys):
        return {(key.table.lower(), key.referencedTable.lower()) for key in keys if key.table.lower() == child.lower()}

    assert pairs(database.getForeignKeys(otherSchema)) == {(child.lower(), parent.lower())}
    assert pairs(database.getForeignKeysFor([child])) == {(child.lower(), parent.lower())}
    assert {name.lower() for name in database.listTables(schema=otherSchema)} >= {child.lower(), parent.lower()}
    if serverName != 'mssql':
        # SQL Server reads every schema's keys where none is named, as it always has.
        assert pairs(database.getForeignKeys()) == set()
    assert database.foreignKeysElsewhere([None]).get(otherSchema.upper() if serverName == 'oracle' else otherSchema) == 1
    assert otherSchema.lower() not in {schema.lower() for schema in database.foreignKeysElsewhere([otherSchema])}


def test_nan_and_infinity_are_refused_by_name_where_a_server_cannot_hold_them(server):
    """MySQL and MariaDB read NaN as a column name ("Unknown column 'nan'"),
    SQL Server the same, and an Oracle NUMBER refused it as an invalid
    number: none of which says what was wrong, or where. PostgreSQL holds both.
    """
    import math

    from bauta.database import UnloadableValueError

    serverName, database, table = server
    name = table('(id INT PRIMARY KEY, v DOUBLE PRECISION)')

    for value, kind in ((float('nan'), 'NaN'), (float('-inf'), 'an infinity')):
        if serverName == 'postgresql':
            database.upsert(table=name, data=[(1, value)], columns=['id', 'v'])
            held = database.query('SELECT v FROM {}'.format(name))[0][0]
            assert math.isnan(held) if kind == 'NaN' else held == value
        else:
            with pytest.raises(UnloadableValueError, match=r'column [vV] was sent {}, which .* cannot hold'.format(kind)):
                database.upsert(table=name, data=[(1, 1.5), (2, value)], columns=['id', 'v'])
            database.rollback()


@pytest.mark.parametrize('name', ['mysql'])
def test_a_mysql_json_column_masked_by_a_plain_strategy_masks_its_value_and_stays_json(name, tmp_path):
    """MySQL returns JSON as its text, so `email` keyed on the address in its
    quotes and returned one a JSON column refused. MariaDB's JSON is
    LONGTEXT, reported as text, and keeps the text as it is.
    """
    import json

    from bauta.jobs.pipeline import _executeDataJob
    from tests.jobConfigs import dataJob

    settings = serverSettings(name)
    suffix = uuid.uuid4().hex[:8]
    source, target = 'json_src_{}'.format(suffix), 'json_tgt_{}'.format(suffix)
    with Database(connectionSettings=settings) as database:
        for table in (source, target):
            database.alter('CREATE TABLE {} (id INT PRIMARY KEY, address JSON, plain TEXT, n JSON, nint BIGINT)'.format(table))
        try:
            database.alter('''INSERT INTO {} VALUES (1, '"person1@realcorp.com"', 'person1@realcorp.com', '4815162342', 4815162342)'''.format(source))
            job = dataJob(sourceConnection='db', targetConnection='db', sourceQuery='SELECT id, address, plain, n, nint FROM {}'.format(source),
                          targetTableFinal=target, masking={'key': 'a-json-column-masking-test-key-0123', 'columns': {
                              'id': 'keep', 'address': {'strategy': 'email', 'domain': 'email'}, 'plain': {'strategy': 'email', 'domain': 'email'},
                              'n': {'strategy': 'key', 'domain': 'n'}, 'nint': {'strategy': 'key', 'domain': 'n'}}})

            _executeDataJob('json', job, {'db': settings})

            [(address, plain, number, integer)] = database.query('SELECT address, plain, n, nint FROM {}'.format(target))
            assert json.loads(address) == plain and plain.endswith('@example.test') and json.loads(number) == integer != 4815162342

            # JSON Lines writes MySQL's JSON as the JSON it is, as PostgreSQL's:
            # a masked string a string, a masked number a number.
            import gzip

            from bauta.configuration import connectionConfig

            files = connectionConfig(type='files', root=str(tmp_path / 'lake'), format='ndjson')
            _executeDataJob('json', job.model_copy(update={'targetConnection': 'lake', 'targetTableFinal': 'docs', 'insertStrategy': 'overwrite'}),
                            {'db': settings, 'lake': files})
            [part] = (tmp_path / 'lake').rglob('*.ndjson.gz')
            [line] = [json.loads(text) for text in gzip.decompress(part.read_bytes()).decode('utf-8').splitlines()]
            assert line['address'] == plain and line['n'] == integer
        finally:
            for table in (source, target):
                database.alter('DROP TABLE IF EXISTS {}'.format(table))


# A role each server can grant to, made once and kept: creating and dropping
# one per test races other runs, and it holds nothing.
GRANTEES = {
    'postgresql': ('bauta_reader', "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'bauta_reader') THEN CREATE ROLE bauta_reader; "
                                   "END IF; END $$"),
    'mysql': ("'bauta_reader'@'%'", "CREATE USER IF NOT EXISTS 'bauta_reader'@'%' IDENTIFIED BY 'Reader1!'"),
    'mariadb': ("'bauta_reader'@'%'", "CREATE USER IF NOT EXISTS 'bauta_reader'@'%' IDENTIFIED BY 'Reader1!'"),
    'oracle': ('BAUTA_READER', "BEGIN EXECUTE IMMEDIATE 'CREATE USER bauta_reader IDENTIFIED BY \"Reader1\"'; "
                               "EXCEPTION WHEN OTHERS THEN IF SQLCODE <> -1920 THEN RAISE; END IF; END;"),
    'mssql': ('bauta_reader', "IF USER_ID('bauta_reader') IS NULL CREATE USER bauta_reader WITHOUT LOGIN"),
    }


def test_a_swapped_table_keeps_its_grants_and_indexes_on_every_run(server):
    """The stage was given the target's keys but not its grants or plain
    indexes, and the two trade names every run: an application's role could
    read the copy after one run and not after the next, and its queries had
    their index every other run.
    """
    from bauta.configuration import DataJobConfig
    from bauta.jobs.pipeline import _executeDataJob

    serverName, database, table = server
    grantee, create = GRANTEES[serverName]
    database.alter(create)
    source = table('(id INT PRIMARY KEY, v VARCHAR(20))')
    target = table('(id INT PRIMARY KEY, v VARCHAR(20))')
    stage = table('(id INT, v VARCHAR(20))')
    database.insert(table=source, data=[(1, 'a'), (2, 'b')])
    database.alter('CREATE INDEX {0}_v ON {0} (v)'.format(target))
    database.alter('GRANT SELECT ON {} TO {}'.format(target, grantee))
    job = DataJobConfig(sourceConnection='s', targetConnection='s', sourceQuery='SELECT id, v FROM {}'.format(source), targetTableStage=stage,
                        targetTableFinal=target, insertStrategy='swap', unmasked=True)

    for _ in range(3):
        _executeDataJob('j', job, {'s': serverSettings(serverName)})

        assert [columns for _, columns in database.dialect.plainIndexes(database.cursor, target)] in ([('v',)], [('V',)])
        assert any(privilege.upper() == 'SELECT' for privilege, _, _ in database.dialect.tableGrants(database.cursor, target))
        assert database.query('SELECT count(*) FROM {}'.format(target)) == [(2,)]
        # Ended, or MySQL holds the table's metadata lock and the next
        # run's rename waits on it for good.
        database.rollback()


@pytest.mark.parametrize('serverName, keyType', [('mariadb', 'UUID'), ('mysql', 'CHAR(36)'), ('mariadb', 'CHAR(36)')])
def test_the_upsert_guard_works_whatever_type_the_key_is(serverName, keyType):
    """The guard refusing a row matched by another unique key first answered
    `SELECT 1`, an integer, beside the key: MariaDB refused to mix its UUID
    type with that, so every upsert into a table keyed by one failed.
    """
    settings = serverSettings(serverName)
    first, second = '11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222'
    with Database(connectionSettings=settings) as database:
        people = 't_{}'.format(uuid.uuid4().hex[:8])
        database.alter('CREATE TABLE {} (id {} PRIMARY KEY, email VARCHAR(50) UNIQUE, name VARCHAR(50))'.format(people, keyType))
        try:
            database.upsert(table=people, data=[(first, 'a@example.test', 'Ann')])
            database.upsert(table=people, data=[(first, 'a@example.test', 'Ann Lee'), (second, 'b@example.test', 'Bo')])
            with pytest.raises(Exception, match='a different row already there'):
                database.upsert(table=people, data=[('33333333-3333-3333-3333-333333333333', 'a@example.test', 'Cy')])

            assert sorted(name for _, _, name in _rows(database, people)) == ['Ann Lee', 'Bo']
        finally:
            database.alter('DROP TABLE {}'.format(people))

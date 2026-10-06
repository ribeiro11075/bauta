"""What only PostgreSQL does, against a real server: COPY, which has its own
text format and carries every value type a load may hold, and a swap that
recreates the views built on the target, since a PostgreSQL view follows the
table rather than its name. What every database does is in
test_integration_databases.py.

Needs the `postgresql` service from docker-compose.yml and psycopg. Run with
`pytest -m integration`; skipped with the reason if either is missing.
"""
import uuid

import pytest

pytest.importorskip('psycopg', reason='psycopg is not installed (pip install -e ".[postgresql]")')


pytestmark = pytest.mark.integration

DATABASE = 'postgresql'


@pytest.fixture
def typesTable(liveDatabase):
    tableName = 'copy_types_{}'.format(uuid.uuid4().hex[:8])
    liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, t TEXT, n NUMERIC(20,5), f FLOAT8, b BOOLEAN, d DATE, ts TIMESTAMPTZ, '
                       'tm TIME, u UUID, by BYTEA, big BIGINT, arr INT[])'.format(tableName))

    yield tableName

    liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(tableName))


def test_copy_round_trips_every_value_type_it_encodes(liveDatabase, typesTable):
    """COPY's text format has its own escaping; any mistake in it changes data
    rather than failing. Tabs, newlines, backslashes, a literal \\N, NaN and
    infinities, time zones and raw bytes all have to come back as sent.
    """
    import datetime
    import decimal
    import math

    identifier = uuid.uuid4()
    moment = datetime.datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=datetime.timezone.utc)
    rows = [
        (1, 'tab\there\nnew\\back \\N "q" ünï', decimal.Decimal('12345.67890'), float('inf'), True, datetime.date(2026, 1, 2),
         moment, datetime.time(1, 2, 3), identifier, b'\x00\x01\\\xff', 2 ** 62, None),
        (2, '', None, float('-inf'), False, None, None, None, None, b'', -1, None),
        (3, None, decimal.Decimal('-0.00001'), float('nan'), None, None, None, None, None, None, None, None),
        ]

    liveDatabase.insert(table=typesTable, data=rows)

    back = liveDatabase.query('SELECT id, t, n, f, b, d, ts, tm, u::text, by, big FROM {} ORDER BY id'.format(typesTable))

    assert back[0] == (1, rows[0][1], rows[0][2], float('inf'), True, rows[0][5], moment, rows[0][7], str(identifier), back[0][9], 2 ** 62)
    assert bytes(back[0][9]) == b'\x00\x01\\\xff'
    assert back[1][:5] == (2, '', None, float('-inf'), False) and bytes(back[1][9]) == b''
    assert back[2][1] is None and back[2][2] == decimal.Decimal('-0.00001') and math.isnan(back[2][3])


def test_a_chunk_copy_cannot_encode_is_inserted_statement_by_statement(liveDatabase, typesTable):
    liveDatabase.insert(table=typesTable, data=[(1, 'x', None, None, None, None, None, None, None, None, None, [1, 2])])

    assert liveDatabase.query('SELECT arr FROM {}'.format(typesTable)) == [([1, 2],)]


def test_a_copied_upsert_updates_existing_rows_and_keeps_the_last_of_repeated_keys(liveDatabase, peopleTable):
    liveDatabase.insert(table=peopleTable, data=[(1, 'old', 10)])

    liveDatabase.upsert(table=peopleTable, data=[(1, 'new', 11), (2, 'first', 20), (2, 'second', 21)], chunkSize=10)
    liveDatabase.upsert(table=peopleTable, data=[(3, 'third', 30)], columns=['id', 'name', 'amount'])

    assert liveDatabase.query('SELECT id, name, amount FROM {} ORDER BY id'.format(peopleTable)) == [
        (1, 'new', 11), (2, 'second', 21), (3, 'third', 30)]


def test_a_swap_repoints_views_at_the_new_target(liveDatabase):
    """A PostgreSQL view follows the table it was made on, not its name, so a
    swap used to leave views reading the old rows -- now the stage table,
    emptied by the next run.
    """
    suffix = uuid.uuid4().hex[:8]
    target, stage = 'orders_{}'.format(suffix), 'orders_{}_stage'.format(suffix)
    view, summary = 'recent_{}'.format(suffix), 'summary_{}'.format(suffix)
    role = 'reader_{}'.format(suffix)

    liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, amount INT)'.format(target))
    liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, amount INT)'.format(stage))
    liveDatabase.alter('CREATE VIEW {} AS SELECT id, amount FROM {} WHERE amount > 0'.format(view, target))
    liveDatabase.alter('CREATE VIEW {} AS SELECT count(*) AS orders FROM {}'.format(summary, view))
    liveDatabase.alter('CREATE ROLE {}'.format(role))
    liveDatabase.alter('GRANT SELECT ON {} TO {}'.format(view, role))

    try:
        liveDatabase.insert(table=target, data=[(1, 10)])
        liveDatabase.insert(table=stage, data=[(2, 20), (3, 30)])

        liveDatabase.swap(targetTable=target, stageTable=stage)

        assert liveDatabase.query('SELECT id FROM {} ORDER BY id'.format(view)) == [(2,), (3,)]
        assert liveDatabase.query('SELECT orders FROM {}'.format(summary)) == [(2,)]
        assert liveDatabase.query("SELECT has_table_privilege('{}', '{}', 'SELECT')".format(role, view)) == [(True,)]
    finally:
        for statement in ('DROP VIEW IF EXISTS {} '.format(summary), 'DROP VIEW IF EXISTS {}'.format(view), 'DROP TABLE IF EXISTS {}'.format(target),
                          'DROP TABLE IF EXISTS {}'.format(stage), 'DROP ROLE IF EXISTS {}'.format(role)):
            liveDatabase.alter(statement)


def test_a_json_column_is_copied_rather_than_refused(liveDatabase):
    """psycopg refuses to write a dict ("cannot adapt type 'dict'"), so a dict
    from another source -- DuckDB's STRUCT, Oracle's JSON -- failed a table
    with a JSON column at its first chunk. Arrays, which are lists like a
    JSON array, still load. Read back, json and jsonb are their text.
    """
    table = 'json_{}'.format(uuid.uuid4().hex[:8])
    liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, doc JSONB, plain JSON, tags TEXT[])'.format(table))

    try:
        documents = [(1, {'name': 'a', 'tags': [1, 2]}, {'b': None}, ['x', 'y']), (2, None, None, [])]
        liveDatabase.insert(table=table, data=documents)
        liveDatabase.upsert(table=table, data=[(1, {'name': 'b'}, {'c': 1}, ['z'])])

        assert liveDatabase.query('SELECT id, doc, plain, tags FROM {} ORDER BY id'.format(table)) == [
            (1, '{"name": "b"}', '{"c": 1}', ['z']), (2, None, None, [])]
    finally:
        liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(table))


def test_a_subset_selecting_a_table_for_several_reasons_does_not_slow_with_small_work_mem(liveDatabase):
    """A table selected for several reasons -- orders, for their customer and
    for their items -- was one WHERE joining EXISTS with OR. PostgreSQL ran
    each as a subplan, hashed only where it fit in work_mem and otherwise
    reading the whole selection again for every row: 2,000 customers of
    20,000 took 24 seconds at work_mem 64kB, and 2,000 of 200,000 at the
    default 4MB took 18. Each reason is now a branch of its own, a join.
    """
    import time

    from bauta.generate.subset import planSubset

    suffix = uuid.uuid4().hex[:6]
    customers, orders, items = ('{}_{}'.format(name, suffix) for name in ('customers', 'orders', 'items'))
    try:
        liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY)'.format(customers))
        liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, customer_id INT REFERENCES {}(id))'.format(orders, customers))
        liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, order_id INT REFERENCES {}(id))'.format(items, orders))
        liveDatabase.alter('INSERT INTO {} SELECT g FROM generate_series(1, 20000) g'.format(customers))
        liveDatabase.alter('INSERT INTO {} SELECT g, g % 20000 + 1 FROM generate_series(1, 40000) g'.format(orders))
        liveDatabase.alter('INSERT INTO {} SELECT g, g % 40000 + 1 FROM generate_series(1, 80000) g'.format(items))
        liveDatabase.alter('ANALYZE')

        foreignKeys = [foreignKey for foreignKey in liveDatabase.getForeignKeys() if foreignKey.table.endswith(suffix)]
        plan = planSubset(foreignKeys, root=customers, where='id <= 2000', materialize=True)

        liveDatabase.execute("SET work_mem = '64kB'")
        started = time.time()
        counts = {table: len(liveDatabase.query(plan.queries[table])) for table in plan.tables}

        assert counts == {customers: 2000, orders: 4000, items: 8000}
        assert time.time() - started < 5
    finally:
        liveDatabase.rollback()
        for table in (items, orders, customers):
            liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(table))


def test_the_catalog_reports_an_array_with_its_element_type_and_schema_keeps_it(liveDatabase):
    """information_schema says only ARRAY, which `schema` made TEXT even for a
    PostgreSQL target.
    """
    from bauta.generate.schema import createStatements, readTable
    from bauta.configuration import DatabaseType

    name = 'arrays_{}'.format(uuid.uuid4().hex[:8])
    liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, tags VARCHAR(20)[], scores INT[], "Mixed" TEXT)'.format(name))
    try:
        assert [(column.name, column.dataType) for column in liveDatabase.getColumnDefinitions(name)] == [
            ('id', 'integer'), ('tags', 'character varying(20)[]'), ('scores', 'integer[]'), ('Mixed', 'text')]

        [statement] = createStatements(DatabaseType.POSTGRESQL, DatabaseType.POSTGRESQL, [readTable(liveDatabase, name, [])])
        assert '"tags" character varying(20)[]' in statement.sql and '"Mixed" TEXT' in statement.sql
    finally:
        liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(name))


def test_json_crosses_as_written_to_postgresql_duckdb_and_json_lines(connectionSettings, liveDatabase, tmp_path):
    """psycopg parsed json and jsonb into Python, and a document lost what it
    was: "123" arrived as 123 and "true" as true, a bare number failed the
    job as an integer sent to jsonb, a fraction passed through a float --
    12345678901234567890.123 became 12345678901234567000 -- and DuckDB
    refused a mixed array beside a plain string. They cross as their text.
    """
    import json

    from bauta.configuration import connectionConfig
    from bauta.jobs.pipeline import _executeDataJob
    from tests.jobConfigs import dataJob

    suffix = uuid.uuid4().hex[:8]
    source, target = 'json_source_{}'.format(suffix), 'json_target_{}'.format(suffix)
    documents = ['"123"', '"true"', '"s"', '123', 'null', '{"n": 12345678901234567890.123, "p": 1.10}', '[1, {"z": "x"}]', '{"a" :  "1"}']
    liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, j JSONB, k JSON)'.format(source))
    liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, j JSONB, k JSON)'.format(target))
    try:
        liveDatabase.alter('INSERT INTO {} VALUES {}'.format(source, ', '.join(
            "({0}, '{1}', '{1}')".format(index, document) for index, document in enumerate(documents)) + ', (99, NULL, NULL)'))
        job = dataJob(sourceConnection='pg', targetConnection='pg', sourceQuery='SELECT id, j, k FROM {}'.format(source), targetTableFinal=target,
                      unmasked=True)

        _executeDataJob('json', job, {'pg': connectionSettings})
        compare = 'SELECT s.id FROM {} s JOIN {} t USING (id) WHERE s.j::text IS DISTINCT FROM t.j::text OR s.k::text IS DISTINCT FROM t.k::text'
        assert liveDatabase.query(compare.format(source, target)) == []

        duckdb = connectionConfig(type='duckdb', path=str(tmp_path / 'copy.duckdb'))
        from bauta.database import Database
        with Database(connectionSettings=duckdb, create=True) as database:
            database.alter('CREATE TABLE {} (id INT PRIMARY KEY, j JSON, k JSON)'.format(target))
        _executeDataJob('json', job.model_copy(update={'targetConnection': 'duck'}), {'pg': connectionSettings, 'duck': duckdb})
        with Database(connectionSettings=duckdb) as database:
            copied = dict(database.query('SELECT id, j FROM {}'.format(target)))
        assert copied[0] == '"123"' and copied[3] == '123' and '12345678901234567890.123' in copied[5] and copied[99] is None

        files = connectionConfig(type='files', root=str(tmp_path / 'lake'), format='ndjson')
        _executeDataJob('json', job.model_copy(update={'targetConnection': 'lake', 'targetTableFinal': 'docs', 'insertStrategy': 'overwrite'}),
                        {'pg': connectionSettings, 'lake': files})
        import gzip

        [part] = (tmp_path / 'lake').rglob('*.ndjson.gz')
        lines = {row['id']: row for row in map(json.loads, gzip.decompress(part.read_bytes()).decode('utf-8').splitlines())}
        # Nested, as documents, not strings holding them.
        assert lines[0]['j'] == '123' and lines[3]['j'] == 123 and lines[6]['j'] == [1, {'z': 'x'}]
    finally:
        liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(source))
        liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(target))


def test_jsonb_columns_of_documents_among_nulls_and_of_bare_strings_are_caught(liveDatabase, tmp_path, monkeypatch, capsys):
    """With jsonb read as its text, discover and audit guessed JSON from a
    value's first character: one null document, or a column of bare JSON
    strings, proposed keep, "no sign of personal data", and the audit of a
    job keeping them said nothing. The column's type says it is JSON.
    """
    import sqlite3

    from tests.review.jsonColumns import assertBothCatchIt, rows

    table = 'people_events_{}'.format(uuid.uuid4().hex[:8])
    liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, events JSONB, contact JSONB)'.format(table))
    try:
        liveDatabase.insert(table=table, data=rows())
        monkeypatch.chdir(tmp_path)
        sqlite3.connect(str(tmp_path / 'copy.db')).close()

        assertBothCatchIt(tmp_path, 'source:\n  type: postgresql\n  host: 127.0.0.1\n  port: 5433\n  user: postgres\n  password: postgres\n'
                                    '  database: bauta_test\ncopy:\n  type: sqlite\n  path: ../copy.db\n', table, capsys)
    finally:
        liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(table))


def test_a_json_column_masked_by_a_plain_strategy_masks_as_it_did_and_stays_json(connectionSettings, liveDatabase, tmp_path):
    """jsonb arriving as its text, `email` keyed on "ana@...", quotes and all:
    a different mask from HEAD's and from the same address in a text column,
    returned as u...@example.test, which isn't JSON -- a jsonb target refused
    it and JSON Lines wrote every line invalid while the job succeeded. The
    value is decoded before masking and encoded after.
    """
    import gzip
    import json

    from bauta.configuration import connectionConfig
    from bauta.jobs.pipeline import _executeDataJob
    from bauta.masking import MaskingPlan
    from tests.jobConfigs import dataJob

    key = 'a-json-column-masking-test-key-0123'
    columns = {'id': 'keep', 'address': {'strategy': 'email', 'domain': 'email'}, 'plain': {'strategy': 'email', 'domain': 'email'},
               'n': {'strategy': 'key', 'domain': 'n'}, 'nint': {'strategy': 'key', 'domain': 'n'}, 'doc': 'hash', 'kept': 'keep'}
    suffix = uuid.uuid4().hex[:8]
    source, target = 'json_plain_source_{}'.format(suffix), 'json_plain_target_{}'.format(suffix)
    definition = '(id INT PRIMARY KEY, address JSONB, plain TEXT, n JSONB, nint BIGINT, doc JSONB, kept JSONB)'
    liveDatabase.alter('CREATE TABLE {} {}'.format(source, definition))
    liveDatabase.alter('CREATE TABLE {} {}'.format(target, definition))
    try:
        liveDatabase.alter('''INSERT INTO {} VALUES (1, '"person1@realcorp.com"', 'person1@realcorp.com', '4815162342', 4815162342,
                              '{{"a": 1, "b": "x"}}', '"123"'), (2, 'null', NULL, NULL, NULL, NULL, '1.10'), (3, NULL, NULL, NULL, NULL, NULL, NULL)'''
                           .format(source))
        job = dataJob(sourceConnection='pg', targetConnection='pg', sourceQuery='SELECT id, address, plain, n, nint, doc, kept FROM {}'.format(source),
                      targetTableFinal=target, masking={'key': key, 'columns': columns})

        _executeDataJob('json', job, {'pg': connectionSettings})

        [one, two, three] = liveDatabase.query('SELECT id, address::text, plain, n::text, nint, doc::text, kept::text FROM {} ORDER BY id'.format(target))
        assert one[1] == json.dumps(one[2]) and one[2].endswith('@example.test')
        assert one[3] == str(one[4]) and one[4] != 4815162342
        # What masking the parsed document gives, as psycopg used to hand it over.
        expected = MaskingPlan(key=key, columns={'doc': 'hash'}).bind(['doc']).apply([({'a': 1, 'b': 'x'},)])[0][0]
        assert one[5] == json.dumps(expected) and one[6] == '"123"'
        assert two[1] is None and two[6] == '1.10' and three[1:] == (None,) * 6

        files = connectionConfig(type='files', root=str(tmp_path / 'lake'), format='ndjson')
        _executeDataJob('json', job.model_copy(update={'targetConnection': 'lake', 'targetTableFinal': 'docs', 'insertStrategy': 'overwrite'}),
                        {'pg': connectionSettings, 'lake': files})
        [part] = (tmp_path / 'lake').rglob('*.ndjson.gz')
        lines = [json.loads(line) for line in gzip.decompress(part.read_bytes()).decode('utf-8').splitlines()]
        assert lines[0]['address'] == one[2] and lines[0]['n'] == one[4] and lines[0]['kept'] == '123' and lines[1]['address'] is None
    finally:
        liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(source))
        liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(target))

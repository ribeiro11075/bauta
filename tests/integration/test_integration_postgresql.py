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
    """psycopg reads a json or jsonb column as a dict and then refuses to write
    one back ("cannot adapt type 'dict'"), so a table with a JSON column failed
    at its first chunk. Arrays, which are lists like a JSON array, still load.
    """
    table = 'json_{}'.format(uuid.uuid4().hex[:8])
    liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, doc JSONB, plain JSON, tags TEXT[])'.format(table))

    try:
        documents = [(1, {'name': 'a', 'tags': [1, 2]}, {'b': None}, ['x', 'y']), (2, None, None, [])]
        liveDatabase.insert(table=table, data=documents)
        liveDatabase.upsert(table=table, data=[(1, {'name': 'b'}, {'c': 1}, ['z'])])

        assert liveDatabase.query('SELECT id, doc, plain, tags FROM {} ORDER BY id'.format(table)) == [
            (1, {'name': 'b'}, {'c': 1}, ['z']), (2, None, None, [])]
    finally:
        liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(table))

"""The conversion table, checked directly: for each database, what each type
a driver hands back is sent as. That these values then load is checked
against the real databases, in test_integration_schema.py's copies.
"""
import datetime
import decimal
import random
import uuid

import pytest

from bauta.configuration import DatabaseType
from bauta.database.values import CONVERSIONS, ValuePreparer, conversionFor, durationText, prepareParameters, prepareValues

POSTGRESQL, MYSQL, MARIADB, ORACLE, MSSQL, SQLITE, DUCKDB = (DatabaseType.POSTGRESQL, DatabaseType.MYSQL, DatabaseType.MARIADB, DatabaseType.ORACLE,
                                                             DatabaseType.MSSQL, DatabaseType.SQLITE, DatabaseType.DUCKDB)

KEY = uuid.UUID(int=1)
CLOCK = datetime.time(1, 2, 3, 456789)
MOMENT = datetime.datetime(2026, 1, 2, 3, 4, 5, 678901)
DAY = datetime.date(2026, 1, 2)
DURATION = datetime.timedelta(hours=30, microseconds=5)

# (value, databases it is sent to as it is, {database: what it is sent as}).
TABLE = [
    (DURATION, set(), {database: '30:00:00.000005' for database in DatabaseType}),
    (['a', 1], {DUCKDB, POSTGRESQL}, {database: '["a", 1]' for database in (MYSQL, MARIADB, ORACLE, MSSQL, SQLITE)}),
    ({'k': [decimal.Decimal('1.5')]}, {DUCKDB}, dict({database: '{"k": ["1.5"]}' for database in (MYSQL, MARIADB, ORACLE, MSSQL, SQLITE)},
                                                   **{POSTGRESQL: lambda sent: type(sent).__name__ == 'Jsonb'})),
    (KEY, {POSTGRESQL, MSSQL, DUCKDB}, {database: str(KEY) for database in (MYSQL, MARIADB, ORACLE, SQLITE)}),
    (CLOCK, {POSTGRESQL, DUCKDB}, {database: '01:02:03.456789' for database in (MYSQL, MARIADB, ORACLE, MSSQL, SQLITE)}),
    (MOMENT, {POSTGRESQL, MYSQL, MARIADB, ORACLE, DUCKDB}, {MSSQL: '2026-01-02 03:04:05.678901', SQLITE: '2026-01-02 03:04:05.678901'}),
    (DAY, set(DatabaseType) - {SQLITE}, {SQLITE: '2026-01-02'}),
    (decimal.Decimal('123456789012345678.1234567890'), set(DatabaseType) - {SQLITE}, {SQLITE: '123456789012345678.1234567890'}),
    (2 ** 63, set(DatabaseType) - {SQLITE, DUCKDB}, {SQLITE: '9223372036854775808', DUCKDB: '9223372036854775808'}),
    (2 ** 63 - 1, set(DatabaseType), {}),
    (True, set(DatabaseType), {}),
    ('text', set(DatabaseType), {}),
    (b'\x00', set(DatabaseType), {}),
    (1.5, set(DatabaseType), {}),
    (None, set(DatabaseType), {}),
    ]


@pytest.mark.parametrize('value, unchanged, converted', TABLE, ids=lambda value: type(value).__name__ if not isinstance(value, (set, dict)) else '')
@pytest.mark.parametrize('database', list(DatabaseType), ids=lambda database: database.value)
def test_each_type_is_sent_to_each_database_as_its_driver_takes_it(database, value, unchanged, converted):
    """Every database named, so a type added to the table for one database
    is decided for all of them. Wrong here, each failed a load at its first
    chunk: a list on four drivers, a UUID on mysql-connector, a time on
    oracledb, an integer past 64 bits on sqlite3.
    """
    assert database in unchanged or database in converted, 'the table says nothing of {} for {}'.format(type(value).__name__, database.value)
    if database == POSTGRESQL and isinstance(value, dict):
        pytest.importorskip('psycopg.types.json', reason='a dict goes to PostgreSQL as psycopg\'s Jsonb')

    [(sent,)] = prepareValues(database, [(value,)])

    if database in unchanged:
        assert sent is value
    elif callable(converted[database]):
        assert converted[database](sent)
    else:
        assert sent == converted[database]


def test_a_list_goes_to_postgresql_as_json_only_into_a_json_column():
    """psycopg writes a list as an array, which a `text[]` column needs and a
    `jsonb` column refused: a DuckDB LIST copied into PostgreSQL never loaded.
    """
    Jsonb = pytest.importorskip('psycopg.types.json').Jsonb

    jsonb, textArray = 3802, 1009
    [row] = prepareValues(POSTGRESQL, [(['a'], ['b'], {'c': 1})], columnTypes=[jsonb, textArray, None])

    assert isinstance(row[0], Jsonb) and row[1] == ['b'] and isinstance(row[2], Jsonb)


def test_a_chunk_with_nothing_to_convert_is_returned_as_it_is():
    """Every chunk of every load passes through; one needing nothing isn't rebuilt."""
    rows = [(1, 'a', 1.5), (2, 'b', None)]

    assert prepareValues(SQLITE, rows) is rows


def test_only_the_columns_holding_a_convertible_value_are_rebuilt():
    left, right = ('kept',), ('kept too',)
    [first, second] = prepareValues(MSSQL, [(left, MOMENT), (right, MOMENT)])

    assert first[0] is left and second[0] is right and first[1] == '2026-01-02 03:04:05.678901'


def _oneValueAtATime(database, rows):
    """What prepareValues must amount to: each value converted on its own."""
    return [tuple(value if (conversion := conversionFor(database, type(value))) is None else conversion(value, None) for value in row)
            for row in rows]


def _chunks():
    """A load's chunks, shaped to take every path a ValuePreparer has, in an
    order that makes it change course: nothing to convert, integers within 64
    bits and past them, integers beside NULLs, NULLs scattered enough to make
    most rows' type signatures distinct, and values every database converts
    differently.
    """
    random.seed(20260927)
    shapes = {
        'plain': lambda number: (1.5 * number, 'text {}'.format(number), None, b'x'),
        'ids': lambda number: (number, 'text', 2 ** 40 + number, None),
        'wide': lambda number: (number, 'text', 2 ** 63 if number == 150 else number, None),
        'sparse': lambda number: tuple(random.choice([None, number, 'text']) for _ in range(12)),
        'mixed': lambda number: (number, None if number % 3 else number, MOMENT, DAY, decimal.Decimal('1.50'), KEY, CLOCK, True),
        }

    return [[tuple(shapes[shape](number)) for number in range(200)]
            for shape in ('plain', 'ids', 'ids', 'wide', 'plain', 'ids', 'sparse', 'ids', 'mixed', 'sparse', 'plain')]


@pytest.fixture(params=['native', 'python'])
def columnKinds(request, monkeypatch):
    """Each test twice: with the native extension finding each column's types
    where it is installed, and with Python finding them.
    """
    import bauta.database.values as values

    if request.param == 'python':
        monkeypatch.setattr(values, '_columnKinds', lambda rows: None)
    elif values._columnKinds([(1,)]) is None:
        pytest.skip('the bauta_rs extension is not in use')

    return request.param


@pytest.mark.parametrize('database', list(DatabaseType), ids=lambda database: database.value)
def test_a_load_prepares_every_chunk_as_its_values_one_at_a_time(database, columnKinds):
    """A ValuePreparer changes how much of the next chunk it looks at from
    what the last one held; the chunks it returns must not change with it.
    """
    preparer = ValuePreparer(database)

    for rows in _chunks():
        assert preparer.prepare(rows) == _oneValueAtATime(database, rows)


def test_a_chunk_with_nothing_to_convert_is_returned_as_it_is_mid_load(columnKinds):
    """Integers within 64 bits need nothing on SQLite, however the preparer
    found that out: the chunk comes back whole, and a converted chunk rebuilds
    only its converted columns.
    """
    preparer = ValuePreparer(SQLITE)
    ids = [(number, 'text', None) for number in range(200)]

    assert preparer.prepare(ids) is ids
    assert preparer.prepare(ids) is ids
    rows = [(number, ('kept',), DAY) for number in range(200)]
    prepared = preparer.prepare(rows)
    assert prepared[7][1] is rows[7][1] and prepared[7][2] == '2026-01-02'


def test_the_extension_finds_the_types_and_ranges_python_would():
    """columnKinds stands in for Python's type() over each column, and for
    whether its ints fit in 64 bits; rows it can't read are left to Python.
    """
    import bauta.database.values as values

    if values._columnKinds([(1,)]) is None:
        pytest.skip('the bauta_rs extension is not in use')

    class Flag(int):
        pass

    for rows in _chunks() + [[(2 ** 63, Flag(1), True, None), (-2 ** 63, 0, 1, 'a')], [(2 ** 63 - 1, -2 ** 63 - 1)]]:
        kinds, fits = values._columnKinds(rows)
        columns = list(zip(*rows))
        assert [set(found) for found in kinds] == [set(map(type, column)) for column in columns]
        assert fits == [all(-2 ** 63 <= value < 2 ** 63 for value in column if type(value) is int) for column in columns]

    for unread in ([(1,), (1, 2)], [[1]], ((1,),)):
        assert values._columnKinds(unread) is None


def test_a_watermark_is_bound_as_a_loaded_value_would_be():
    """sqlite3 took a Decimal or a datetime watermark only through adapters
    registered for the whole process, which changed sqlite3 for anything else
    in it; pymssql rounded a datetime one to the millisecond.
    """
    assert prepareParameters(SQLITE, [decimal.Decimal('1.5'), MOMENT]) == ('1.5', '2026-01-02 03:04:05.678901')
    assert prepareParameters(MSSQL, (MOMENT,)) == ('2026-01-02 03:04:05.678901',)


def test_every_database_has_a_row():
    assert set(CONVERSIONS) == set(DatabaseType)


def test_a_negative_duration_keeps_its_sign():
    assert durationText(-datetime.timedelta(hours=1, minutes=2, seconds=3)) == '-01:02:03'

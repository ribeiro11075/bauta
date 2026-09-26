"""The conversion table, checked directly: for each database, what each type
a driver hands back is sent as. That these values then load is checked
against the real databases, in test_integration_schema.py's copies.
"""
import datetime
import decimal
import uuid

import pytest

from bauta.configuration import DatabaseType
from bauta.database.values import CONVERSIONS, durationText, prepareParameters, prepareValues

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
    (2 ** 63, set(DatabaseType) - {SQLITE}, {SQLITE: '9223372036854775808'}),
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
    from psycopg.types.json import Jsonb

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

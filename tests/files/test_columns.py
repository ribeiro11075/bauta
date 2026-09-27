"""What a column of a file target is settled as, from the values a driver
returns, and what a settled column refuses.
"""
import datetime
import decimal

import pytest

from bauta.configuration import ColumnType, parseColumnType
from bauta.files.columns import FileTypeError, inferType, reportedDecimal, toArrow

UTC = datetime.timezone.utc


@pytest.mark.parametrize('values, settled', [
    ([None, None], None),
    ([True, False], 'bool'),
    ([1, True], 'int64'),
    ([1, 2 ** 63], 'decimal(38,0)'),
    ([1, 2.5], 'float64'),
    ([1, decimal.Decimal('1.5')], 'decimal(38,10)'),
    ([decimal.Decimal('1.' + '1' * 12)], 'decimal(38,12)'),
    (['a', 1, 2.5], 'string'),
    ([b'\x00'], 'binary'),
    ([datetime.date(2026, 1, 1)], 'date'),
    ([datetime.date(2026, 1, 1), datetime.datetime(2026, 1, 1, 12)], 'timestamp'),
    ([datetime.datetime(2026, 1, 1, tzinfo=UTC)], 'timestamptz'),
    ([datetime.time(12, 30)], 'time'),
    ])
def test_a_chunks_values_settle_its_columns_type(values, settled):
    columnType = inferType('c', values)

    assert (str(columnType) if columnType else None) == settled


def test_a_decimal_takes_the_precision_and_scale_reported_over_what_its_values_show():
    assert inferType('c', [decimal.Decimal('1.5')], reported=(12, 2)) == ColumnType('decimal', 12, 2)


@pytest.mark.parametrize('values, message', [
    (['a', b'b'], 'holds both text and bytes'),
    ([10 ** 40], 'holds integers of more than 38 digits'),
    ([decimal.Decimal('NaN')], 'holds a NaN or infinite decimal'),
    ([datetime.datetime(2026, 1, 1), datetime.datetime(2026, 1, 1, tzinfo=UTC)], 'holds times both with and without a time zone'),
    ([2.5, decimal.Decimal('1')], 'holds Decimal, float'),
    ([object()], 'holds object'),
    ])
def test_values_that_settle_no_one_type_are_refused_saying_what_to_do(values, message):
    with pytest.raises(FileTypeError, match='column c {}.*declare it in targetColumnTypes'.format(message)):
        inferType('c', values)


@pytest.mark.parametrize('description, reported', [
    (('c', 1700, None, None, 12, 2, None), (12, 2)),
    (('c', 'DECIMAL(18,3)', None, None, None, None, None), (18, 3)),
    # Oracle's NUMBER without a precision.
    (('c', 'DB_TYPE_NUMBER', None, None, 0, -127, None), None),
    (('c', 246, None, None, None, None, 1, 0, 45), None),
    (('c',), None),
    (None, None),
    ])
def test_only_a_usable_reported_precision_and_scale_is_taken(description, reported):
    assert reportedDecimal(description) == reported


@pytest.fixture
def arrow():
    """Writing a column needs pyarrow; settling its type doesn't."""
    return pytest.importorskip('pyarrow', reason='writing a column needs pyarrow (pip install -e ".[files]")')


@pytest.mark.parametrize('declared, values, message', [
    ('int8', [200], 'an integer outside its range'),
    ('uint32', [-1], 'an integer outside its range'),
    ('bool', [1], 'a row holds int'),
    ('date', [datetime.datetime(2026, 1, 1, 12)], 'holds a time of day too'),
    ('timestamp', [datetime.datetime(2026, 1, 1, tzinfo=UTC)], 'declare it timestamptz'),
    ('timestamptz', [datetime.datetime(2026, 1, 1)], 'declare it timestamp'),
    ('binary', ['text'], 'a row holds str'),
    ('decimal(4,2)', [decimal.Decimal('123.45')], "doesn't fit it"),
    ])
def test_a_settled_column_refuses_what_it_cannot_hold_exactly(arrow, declared, values, message):
    with pytest.raises(FileTypeError, match=message):
        toArrow('c', values, parseColumnType(declared), lenient=True)


def test_a_declared_text_column_takes_numbers_and_dates_as_their_text(arrow):
    array = toArrow('c', ['a', 1, decimal.Decimal('2.50'), datetime.date(2026, 1, 1), datetime.time(12, 30), None],
                    parseColumnType('string'), lenient=True)

    assert array.to_pylist() == ['a', '1', '2.50', '2026-01-01', '12:30:00', None]


def test_a_text_column_nobody_chose_takes_text_alone(arrow):
    with pytest.raises(FileTypeError, match='a row holds int'):
        toArrow('c', ['a', 1], parseColumnType('string'), lenient=False)


def test_an_instant_is_written_in_utc_and_a_date_in_a_timestamp_column_as_its_midnight(arrow):
    offset = datetime.timezone(datetime.timedelta(hours=-5))

    assert toArrow('c', [datetime.datetime(2026, 1, 1, 7, tzinfo=offset)], parseColumnType('timestamptz'), lenient=False).to_pylist() == \
        [datetime.datetime(2026, 1, 1, 12, tzinfo=UTC)]
    assert toArrow('c', [datetime.date(2026, 1, 1)], parseColumnType('timestamp'), lenient=False).to_pylist() == \
        [datetime.datetime(2026, 1, 1)]


@pytest.mark.parametrize('declared, arrowName', [('float32', 'float'), ('uint64', 'uint64'), ('time', 'time64[us]'), ('binary', 'binary')])
def test_each_declared_type_is_the_arrow_type_of_its_name(arrow, declared, arrowName):
    assert str(toArrow('c', [None], parseColumnType(declared), lenient=False).type) == arrowName

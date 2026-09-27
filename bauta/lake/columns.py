"""What a column of a file target is written as, and its values turned into
an Arrow array of that type.

A column's type is declared in targetColumnTypes, or settled by the first
chunk that holds a value in it, and fixed from then on: a Parquet file has
one schema, and a table's parts should share it. A later value that doesn't
fit fails the job, naming the column and what to declare, rather than being
cast into something else -- pyarrow itself would write 1.5 into an integer
column as 1.

Nothing here puts a value in a message: the rows are masked by now, but a
job that doesn't mask is still copying someone's data.
"""
from __future__ import annotations

import datetime
import decimal
import re
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from ..configuration import ColumnType, ConfigurationError
from ..configuration.columnTypes import MAXIMUM_DECIMAL_PRECISION, isInteger
from ..database.values import durationText, jsonText

# The scale a decimal is given when the source doesn't report one: Oracle's
# NUMBER without a precision, SQLite, MySQL's driver. Values in one column
# differ in scale there, and the first chunk's widest needn't be the widest.
DEFAULT_DECIMAL_SCALE = 10

_INTEGER_RANGES = {
    'int8': (-2 ** 7, 2 ** 7), 'int16': (-2 ** 15, 2 ** 15), 'int32': (-2 ** 31, 2 ** 31), 'int64': (-2 ** 63, 2 ** 63),
    'uint8': (0, 2 ** 8), 'uint16': (0, 2 ** 16), 'uint32': (0, 2 ** 32), 'uint64': (0, 2 ** 64),
    }

_REPORTED_DECIMAL = re.compile(r'DECIMAL\((\d+),\s*(\d+)\)', re.IGNORECASE)

UNKNOWN_KINDS_ADVICE = 'declare it in targetColumnTypes'


class FileTypeError(ConfigurationError):
    """A value that doesn't fit its column's type. A ConfigurationError, since
    retrying writes the same value into the same type.
    """


def normalized(value: Any) -> Any:
    """A value as a file holds it: what Parquet has no type for becomes text,
    spelled as bauta spells it for a text column in a database.
    """

    if isinstance(value, (dict, list)):
        return jsonText(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime.timedelta):
        return durationText(value)
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)

    return value


def reportedDecimal(description: Optional[Sequence[Any]]) -> Optional[Tuple[int, int]]:
    """The precision and scale the driver reports for a column, from its
    cursor.description row, where it reports a usable one: psycopg and
    oracledb in the DB-API's places, DuckDB in its type's name.
    """

    if not description:
        return None

    try:
        precision, scale = description[4], description[5]
    except (IndexError, TypeError):
        precision = scale = None

    if isinstance(precision, int) and isinstance(scale, int) and 0 < precision <= MAXIMUM_DECIMAL_PRECISION and 0 <= scale <= precision:
        return precision, scale

    match = _REPORTED_DECIMAL.search(str(description[1]) if len(description) > 1 else '')
    if match:
        precision, scale = int(match.group(1)), int(match.group(2))
        if 0 < precision <= MAXIMUM_DECIMAL_PRECISION and scale <= precision:
            return precision, scale

    return None


def _scale(value: decimal.Decimal) -> int:

    exponent = value.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


# The Python types a column's values are told apart by, the narrower first:
# a bool is an int, and a datetime a date. A driver's own subclass of one --
# a str subclass for an enum -- counts as the type it extends.
_KINDS = (bool, int, float, decimal.Decimal, str, bytes, datetime.datetime, datetime.date, datetime.time)


def _kind(value: Any) -> type:

    for kind in _KINDS:
        if isinstance(value, kind):
            return kind

    return type(value)


def _digits(value: int) -> int:

    return len(str(abs(value)))


def inferType(column: str, values: Sequence[Any], reported: Optional[Tuple[int, int]] = None) -> Optional[ColumnType]:
    """The type the values of one chunk settle a column as, or None when they
    are all null. `values` are normalized. Mixed numbers and text, which
    SQLite's columns hold, are text.
    """

    kinds: Set[type] = {_kind(value) for value in values if value is not None}
    if not kinds:
        return None

    def refuse(why: str) -> FileTypeError:
        return FileTypeError('column {} {}, so its type can\'t be told from its values; {}'.format(column, why, UNKNOWN_KINDS_ADVICE))

    if kinds == {bool}:
        return ColumnType('bool')

    if str in kinds:
        if bytes in kinds:
            raise refuse('holds both text and bytes')
        return ColumnType('string')

    if kinds <= {bool, int}:
        low, high = _INTEGER_RANGES['int64']
        if all(value is None or low <= value < high for value in values):
            return ColumnType('int64')
        # Wider than any engine's integer but uint64, which few read.
        if max(_digits(value) for value in values if value is not None) > MAXIMUM_DECIMAL_PRECISION:
            raise refuse('holds integers of more than {} digits'.format(MAXIMUM_DECIMAL_PRECISION))
        return ColumnType('decimal', MAXIMUM_DECIMAL_PRECISION, 0)

    if kinds <= {bool, int, float}:
        return ColumnType('float64')

    if kinds <= {bool, int, decimal.Decimal}:
        if any(isinstance(value, decimal.Decimal) and not value.is_finite() for value in values):
            raise refuse('holds a NaN or infinite decimal, which a decimal column can\'t')
        if reported is not None:
            return ColumnType('decimal', reported[0], reported[1])
        seen = max((_scale(value) for value in values if isinstance(value, decimal.Decimal)), default=0)
        return ColumnType('decimal', MAXIMUM_DECIMAL_PRECISION, min(max(seen, DEFAULT_DECIMAL_SCALE), MAXIMUM_DECIMAL_PRECISION))

    if kinds == {bytes}:
        return ColumnType('binary')

    if kinds <= {datetime.date, datetime.datetime}:
        if kinds == {datetime.date}:
            return ColumnType('date')
        zoned = {value.tzinfo is not None for value in values if isinstance(value, datetime.datetime)}
        if len(zoned) > 1:
            raise refuse('holds times both with and without a time zone')
        return ColumnType('timestamptz' if zoned == {True} else 'timestamp')

    if kinds == {datetime.time}:
        return ColumnType('time')

    raise refuse('holds {}'.format(', '.join(sorted(kind.__name__ for kind in kinds))))


def arrowType(columnType: ColumnType) -> Any:

    import pyarrow

    kind = columnType.kind
    if kind == 'decimal':
        return pyarrow.decimal128(columnType.precision, columnType.scale)
    if kind == 'timestamp':
        return pyarrow.timestamp('us')
    if kind == 'timestamptz':
        return pyarrow.timestamp('us', tz='UTC')
    if kind == 'time':
        return pyarrow.time64('us')
    if kind == 'date':
        return pyarrow.date32()
    if kind == 'string':
        return pyarrow.string()
    if kind == 'binary':
        return pyarrow.binary()
    if kind == 'bool':
        return pyarrow.bool_()

    return getattr(pyarrow, kind)()


def _asText(value: Any) -> Any:

    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()

    return str(value)


def _floatAsDecimal(value: float) -> decimal.Decimal:

    return decimal.Decimal(repr(value))


def _asUtc(value: datetime.datetime) -> datetime.datetime:

    return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)


# What each kind takes, beyond its own Python type, and how. None where it
# takes the value as it is.
_Accepts = Dict[type, Optional[Callable[[Any], Any]]]


def _accepts(columnType: ColumnType, lenient: bool) -> _Accepts:
    """The Python types a column of `columnType` takes. A lenient text column
    -- one declared text, or settled as text by mixed values -- takes numbers
    and dates too, as their text; one settled as text only because nothing in
    it had a value yet takes text alone.
    """

    kind = columnType.kind

    if isInteger(columnType):
        return {int: None, bool: int}
    if kind in ('float32', 'float64'):
        return {float: None, int: float, bool: float}
    if kind == 'decimal':
        # A float by its shortest text, which is what was stored: 0.1 is
        # 0.1, not the binary fraction nearest it.
        return {decimal.Decimal: None, int: None, bool: int, float: _floatAsDecimal}
    if kind == 'string':
        return {str: None, **({other: _asText for other in (int, float, decimal.Decimal, bool, datetime.date, datetime.datetime, datetime.time)}
                               if lenient else {})}
    if kind == 'binary':
        return {bytes: None}
    if kind == 'bool':
        return {bool: None}
    if kind == 'date':
        return {datetime.date: None}
    if kind == 'timestamp':
        return {datetime.datetime: None, datetime.date: lambda value: datetime.datetime(value.year, value.month, value.day)}
    if kind == 'timestamptz':
        return {datetime.datetime: _asUtc}
    if kind == 'time':
        return {datetime.time: None}

    raise AssertionError('no kind {}'.format(kind))


def toArrow(column: str, values: List[Any], columnType: ColumnType, lenient: bool) -> Any:
    """`values`, normalized, as an Arrow array of the column's type; a
    FileTypeError naming the column when one doesn't fit.
    """

    import pyarrow

    accepts = _accepts(columnType, lenient)
    kinds = {_kind(value) for value in values if value is not None}
    if columnType.kind == 'timestamp' and any(isinstance(value, datetime.datetime) and value.tzinfo is not None for value in values):
        raise FileTypeError('column {} is written as timestamp, without a time zone, and a row holds one with; declare it '
                            'timestamptz in targetColumnTypes'.format(column))
    if columnType.kind == 'timestamptz' and any(isinstance(value, datetime.datetime) and value.tzinfo is None for value in values):
        raise FileTypeError('column {} is written as timestamptz, and a row holds a time without a time zone, which could be '
                            'any instant; declare it timestamp in targetColumnTypes'.format(column))
    if columnType.kind == 'date' and datetime.datetime in kinds:
        raise FileTypeError('column {} is written as date, and a row holds a time of day too; declare it timestamp in '
                            'targetColumnTypes'.format(column))

    refused = sorted(kind.__name__ for kind in kinds if kind not in accepts)
    if refused:
        raise FileTypeError('column {} is written as {}, and a row holds {}; {} as what it holds'.format(
            column, columnType, ', '.join(refused), UNKNOWN_KINDS_ADVICE))

    conversions = {kind: accepts[kind] for kind in kinds if accepts[kind] is not None}
    if conversions:
        values = [value if value is None or _kind(value) not in conversions else conversions[_kind(value)](value)  # type: ignore[misc]
                  for value in values]

    if isInteger(columnType):
        low, high = _INTEGER_RANGES[columnType.kind]
        if any(value is not None and not low <= value < high for value in values):
            raise FileTypeError('column {} is written as {}, and a row holds an integer outside its range; declare a wider '
                                'type in targetColumnTypes, such as int64 or decimal(38,0)'.format(column, columnType))

    try:
        return pyarrow.array(values, type=arrowType(columnType))
    except (pyarrow.ArrowInvalid, pyarrow.ArrowTypeError, OverflowError, ValueError, TypeError) as error:
        # A decimal that needs more digits or places than the column has is
        # the usual one: pyarrow says it would lose data, and doesn't.
        raise FileTypeError('column {} is written as {}, and a row holds a value that doesn\'t fit it ({}); declare a type that '
                            'holds it in targetColumnTypes'.format(column, columnType, type(error).__name__)) from None

"""What a value becomes on its way to each database: one table, CONVERSIONS,
from Python type to what that database's driver is sent instead.

A value comes from one driver and goes to another, and drivers disagree on
what they accept. A type that isn't in a database's row is sent as it is.
Loads and bound query parameters -- a watermark -- both pass through here.
"""
from __future__ import annotations

import datetime
import decimal
import itertools
import json
import math
import uuid
from operator import itemgetter
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from ..configuration import DatabaseType

# A conversion takes the value and the type code of the column it is going
# to -- known on PostgreSQL and Oracle only, see WANTS_COLUMN_TYPES -- and returns what
# the driver is sent.
Conversion = Callable[[Any, Any], Any]


class UnloadableValueError(ValueError):
    """A value the target column can't hold, found before it is sent. Never
    retried: the same rows would fail the same way. Names the column and the
    kind of value, never the value.
    """


class UniqueKeyClashError(UnloadableValueError):
    """An upserted row that matched a different row by a unique key other
    than the primary key, refused rather than merged into it. Never retried.
    See MySQLDialect._onDuplicateKey.
    """


_NON_FINITE_NAMES = {'nan': 'NaN', 'inf': 'an infinity'}


def refuseNonFinite(refused: frozenset, rows: Sequence[Sequence[Any]], columns: Sequence[str], table: str, databaseName: str,
                    holds: Callable[[int], bool]) -> None:
    """Raises UnloadableValueError for a NaN or infinity in a column of `rows`
    that `refused` says this database can't hold, unless `holds(index)` says
    that column can. Only columns holding a float have their values read.
    """

    if not refused or not rows:
        return

    for index, column in enumerate(zip(*rows)):
        if float not in set(map(type, column)) or holds(index):
            continue
        for value in column:
            if type(value) is float and not math.isfinite(value):
                kind = 'nan' if math.isnan(value) else 'inf'
                if kind in refused:
                    raise UnloadableValueError(
                        '{} column {} was sent {}, which {} cannot hold in it{}. Turn it into NULL with the nullIfNotFinite '
                        'transform (bauta.transform.builtinTransforms:nullIfNotFinite), or in sourceQuery'.format(
                            table, columns[index] if index < len(columns) else index + 1, _NON_FINITE_NAMES[kind], databaseName,
                            ' (it would store it as NULL)' if databaseName == 'SQLite' else ''))


def durationText(value: datetime.timedelta) -> str:
    """A duration as `[-]HH:MM:SS[.ffffff]`, the way MySQL writes a TIME.

    MySQL's TIME is a duration, from -838:59:59 to 838:59:59, and its driver
    returns a timedelta, which no other driver takes as one: SQL Server's and
    SQLite's refuse it, psycopg writes an interval that PostgreSQL reads into
    a TIME column as a wrong time of day, and oracledb writes Python's own
    `-35 days, 1:00:01`. Every database parses this spelling back.
    """

    sign = '-' if value < datetime.timedelta(0) else ''
    magnitude = abs(value)
    hours, rest = divmod(int(magnitude.total_seconds()), 3600)
    minutes, seconds = divmod(rest, 60)
    fraction = '.{:06d}'.format(magnitude.microseconds) if magnitude.microseconds else ''

    return '{}{:02d}:{:02d}:{:02d}{}'.format(sign, hours, minutes, seconds, fraction)


def jsonText(value: Any) -> str:
    """A list or dictionary as JSON. What JSON has no type for inside one -- a
    Decimal, a date, a UUID in a DuckDB STRUCT -- is written as its text.
    """

    return json.dumps(value, default=str, ensure_ascii=False)


# The integers a driver binds as integers: 64 bits, signed.
_BOUND_INTEGERS = range(-2 ** 63, 2 ** 63)

# The json and jsonb type codes: the PostgreSQL columns a list goes to as JSON,
# and whose values, read as their text, are JSON documents.
POSTGRESQL_JSON_TYPES = frozenset({114, 3802})
_POSTGRESQL_JSON = POSTGRESQL_JSON_TYPES


# MySQL's type code for JSON. MariaDB's JSON is LONGTEXT, reported as text,
# so nothing tells it from a text column.
MYSQL_JSON_TYPE = 245


def jsonColumnIndexes(databaseType: Optional[DatabaseType], description: Optional[Sequence[Sequence[Any]]]) -> List[int]:
    """The positions of the columns that hold JSON as its text, by what the
    source's driver reports of each: PostgreSQL's json and jsonb, MySQL's
    JSON and DuckDB's.
    """

    def isJson(code: Any) -> bool:
        if databaseType == DatabaseType.POSTGRESQL:
            return code in POSTGRESQL_JSON_TYPES
        if databaseType == DatabaseType.MYSQL:
            return code == MYSQL_JSON_TYPE
        if databaseType == DatabaseType.DUCKDB:
            return str(code).upper() == 'JSON'
        return False

    return [index for index, column in enumerate(description or ()) if len(column) > 1 and isJson(column[1])]


def decodedJson(rows: Sequence[Sequence[Any]], description: Optional[Sequence[Sequence[Any]]],
                databaseType: Optional[DatabaseType] = DatabaseType.POSTGRESQL) -> List[Tuple[Any, ...]]:
    """`rows` with each value of a JSON column, which arrives as its text,
    parsed: an object or array as one, a JSON string as the string, and JSON
    null as None -- what to classify, for `discover` and `audit`, which read
    samples and never load them. See jsonColumnIndexes.
    """

    indexes = jsonColumnIndexes(databaseType, description)
    if not indexes:
        return [tuple(row) for row in rows]

    decoded = []
    for row in rows:
        values = list(row)
        for index in indexes:
            if isinstance(values[index], str):
                try:
                    values[index] = json.loads(values[index])
                except ValueError:
                    pass
        decoded.append(tuple(values))

    return decoded


def _duration(value: Any, columnType: Any) -> Any:

    return durationText(value)


def _json(value: Any, columnType: Any) -> Any:

    return jsonText(value)


def _text(value: Any, columnType: Any) -> Any:

    return str(value)


def _iso(value: Any, columnType: Any) -> Any:

    return value.isoformat()


def _isoDateTime(value: Any, columnType: Any) -> Any:

    return value.isoformat(sep=' ')


def _wideInteger(value: Any, columnType: Any) -> Any:
    """Past 64 bits as its exact text, which the database casts to the
    column's type: sqlite3 raises OverflowError on such an integer, and
    DuckDB before 1.5 binds one as a float, rounding it even into a HUGEINT.
    """

    return str(value) if value not in _BOUND_INTEGERS else value


def _postgresqlJson(value: Any, columnType: Any) -> Any:
    """psycopg refuses a dict, and writes a list as an array -- what a `text[]`
    column needs, and what a json or jsonb column refuses.
    """

    from psycopg.types.json import Jsonb

    return Jsonb(value)


def _postgresqlList(value: Any, columnType: Any) -> Any:

    return _postgresqlJson(value, columnType) if columnType in _POSTGRESQL_JSON else value


# Each database's row names the types its driver can't take as they are. A
# type is looked up through its bases, so a subclass of datetime is converted
# as one; None sends a type as it is, as bool, a subclass of int, needs where
# int has a conversion.
#
# - Durations as text, everywhere: see durationText.
# - Lists and dictionaries (a PostgreSQL array or JSON, a DuckDB LIST, STRUCT
#   or MAP) as JSON text, which `schema` maps them to: none of sqlite3,
#   mysql-connector, oracledb and pymssql binds one. DuckDB takes both as they
#   are; PostgreSQL decides by the column.
# - A UUID and a time of day as text for mysql-connector and oracledb, which
#   refuse them.
# - Times with their microseconds as ISO text on SQL Server: pymssql renders a
#   bound datetime to the millisecond, losing what a DATETIME2 column holds.
# - SQLite's dates, times, Decimals and UUIDs as text: it has no type for
#   them, and Python's built-in adapters for dates are deprecated since 3.12.
#   A Decimal kept as exact text needs a column of text affinity, which is
#   what `schema` creates for one.
# - Integers past 64 bits as exact text on SQLite and DuckDB; see _wideInteger.
CONVERSIONS: Dict[DatabaseType, Dict[type, Optional[Conversion]]] = {
    DatabaseType.POSTGRESQL: {datetime.timedelta: _duration, dict: _postgresqlJson, list: _postgresqlList},
    DatabaseType.MYSQL: {datetime.timedelta: _duration, dict: _json, list: _json, uuid.UUID: _text, datetime.time: _iso},
    DatabaseType.MARIADB: {datetime.timedelta: _duration, dict: _json, list: _json, uuid.UUID: _text, datetime.time: _iso},
    DatabaseType.ORACLE: {datetime.timedelta: _duration, dict: _json, list: _json, uuid.UUID: _text, datetime.time: _iso},
    DatabaseType.MSSQL: {datetime.timedelta: _duration, dict: _json, list: _json, datetime.datetime: _isoDateTime, datetime.time: _iso},
    DatabaseType.SQLITE: {datetime.timedelta: _duration, dict: _json, list: _json, uuid.UUID: _text, datetime.time: _iso,
                          datetime.datetime: _isoDateTime, datetime.date: _iso, decimal.Decimal: _text, int: _wideInteger, bool: None},
    DatabaseType.DUCKDB: {datetime.timedelta: _duration, int: _wideInteger, bool: None},
    }

# The databases whose conversions, or binds (DatabaseDialect.bindTypes),
# depend on the column a value goes to, whose type codes cost a statement per
# table to read.
WANTS_COLUMN_TYPES = frozenset({DatabaseType.POSTGRESQL, DatabaseType.ORACLE})

_found: Dict[Tuple[DatabaseType, type], Optional[Conversion]] = {}


def conversionFor(databaseType: DatabaseType, kind: type) -> Optional[Conversion]:
    """The conversion a value of `kind` takes to `databaseType`, found through
    its bases, or None to send it as it is.
    """

    key = (databaseType, kind)
    if key not in _found:
        table = CONVERSIONS[databaseType]
        _found[key] = next((table[base] for base in kind.__mro__ if base in table), None)

    return _found[key]


_NONE = type(None)

# Rows to each distinct type signature below which finding a chunk's columns
# by their signatures beats transposing it: NULLs scattered over many columns
# make most rows' signatures distinct.
_ROWS_PER_SIGNATURE = 8


def _columnKinds(rows: List[Tuple[Any, ...]]) -> Optional[Tuple[List[List[type]], List[bool]]]:
    """Each column's exact types, and whether its ints all fit in 64 bits,
    from the native extension in one pass; None without it, or for rows it
    doesn't read, which the caller then reads in Python.
    """

    from ..masking.core import nativeExtension

    native = nativeExtension()

    return native.columnKinds(rows) if native is not None else None


def _fitIn64Bits(column: Sequence[Any], kinds: Set[type]) -> bool:
    """Whether a column of ints, and perhaps NULLs, needs no _wideInteger: its
    smallest and largest, found in C, stand for the rest.
    """

    values = column if _NONE not in kinds else [value for value in column if value is not None]

    return not values or (min(values) in _BOUND_INTEGERS and max(values) in _BOUND_INTEGERS)


class ValuePreparer:
    """prepareValues for the chunks of one load. What it learns of one chunk
    changes how much of the next it looks at, never what it returns.

    Which columns hold a value to convert comes from each column's exact
    types, which the native extension finds in one pass where it is in use.
    In Python, a chunk usually either holds nothing to convert, which one pass
    over its values in C finds, or holds the same kinds in the same columns as
    the chunk before -- an integer id bound for SQLite, say. For those it skips
    that pass, and finds the columns from the rows' distinct type signatures,
    another pass in C, rather than by transposing the chunk and scanning every
    column. Where NULLs make the signatures too many, it transposes, then and
    for the rest of the load.

    A column of ints within 64 bits, and NULLs, needs nothing on the two
    databases that convert ints, which is nearly every such column: it is
    checked whole, not value by value.
    """

    def __init__(self, databaseType: DatabaseType) -> None:
        self.databaseType = databaseType
        # Whether the last chunk held a value a conversion applies to.
        self._converting = False
        self._bySignature = True


    def _kindsInPython(self, rows: List[Tuple[Any, ...]]) -> Tuple[Optional[List[Set[type]]], Optional[List[Tuple[Any, ...]]]]:
        """Each column's exact types, and the chunk transposed where that was
        the way to find them; no types where the chunk holds nothing to
        convert.
        """

        if not self._converting and not any(conversionFor(self.databaseType, kind) for kind in set(map(type, itertools.chain.from_iterable(rows)))):
            return None, None

        if self._bySignature:
            signatures = set(map(tuple, map(map, itertools.repeat(type), rows)))
            if len(signatures) * _ROWS_PER_SIGNATURE <= len(rows):
                return [set(kinds) for kinds in zip(*signatures)], None
            self._bySignature = False

        columns = list(zip(*rows))

        return [set(map(type, column)) for column in columns], columns


    def prepare(self, rows: List[Tuple[Any, ...]], columnTypes: Optional[Sequence[Any]] = None) -> List[Tuple[Any, ...]]:

        if not rows:
            return rows

        columns: Optional[List[Tuple[Any, ...]]] = None
        fits: Optional[List[bool]] = None
        kindsByColumn: Optional[List[Set[type]]]
        found = _columnKinds(rows)
        if found is not None:
            kindsByColumn, fits = [set(kinds) for kinds in found[0]], found[1]
        else:
            kindsByColumn, columns = self._kindsInPython(rows)
            if kindsByColumn is None:
                return rows

        self._converting = False
        converted: Dict[int, Tuple[Any, ...]] = {}
        for index, kinds in enumerate(kindsByColumn):
            conversions = {kind: conversionFor(self.databaseType, kind) for kind in kinds}
            applying = {conversion for conversion in conversions.values() if conversion is not None}
            if not applying:
                continue
            self._converting = True

            column = columns[index] if columns is not None else tuple(map(itemgetter(index), rows))
            if applying == {_wideInteger} and kinds <= {int, _NONE} and (fits[index] if fits is not None else _fitIn64Bits(column, kinds)):
                continue

            columnType = columnTypes[index] if columnTypes is not None and index < len(columnTypes) else None
            values = tuple(value if (conversion := conversions[type(value)]) is None else conversion(value, columnType) for value in column)
            # A conversion may leave every value as it was, and the column is
            # then kept as it is.
            if any(new is not old for new, old in zip(values, column)):
                converted[index] = values

        if not converted:
            return rows

        if columns is None:
            columns = list(zip(*rows))
        for index, values in converted.items():
            columns[index] = values

        return list(zip(*columns))


def prepareValues(databaseType: DatabaseType, rows: List[Tuple[Any, ...]], columnTypes: Optional[Sequence[Any]] = None) -> List[Tuple[Any, ...]]:
    """`rows` as `databaseType`'s driver must receive them: a chunk with
    nothing to convert as it is, and otherwise only the columns holding a
    value to convert converted, the rest carried through untouched. A load of
    many chunks keeps a ValuePreparer instead.
    """

    return ValuePreparer(databaseType).prepare(rows, columnTypes)


def prepareParameters(databaseType: DatabaseType, parameters: Sequence[Any]) -> Tuple[Any, ...]:
    """Bound query parameters, such as a watermark, as the driver must take
    them: converted as a one-row load would be.
    """

    return prepareValues(databaseType, [tuple(parameters)])[0]

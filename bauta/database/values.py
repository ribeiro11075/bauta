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
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..configuration import DatabaseType

# A conversion takes the value and the type code of the column it is going
# to -- known on PostgreSQL only, see WANTS_COLUMN_TYPES -- and returns what
# the driver is sent.
Conversion = Callable[[Any, Any], Any]


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


# The integers SQLite stores as integers: 64 bits, signed.
_SQLITE_INTEGERS = range(-2 ** 63, 2 ** 63)

# The json and jsonb type codes: the PostgreSQL columns a list goes to as JSON.
_POSTGRESQL_JSON = {114, 3802}


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


def _sqliteInteger(value: Any, columnType: Any) -> Any:
    """Past 64 bits as its exact text, which `schema` creates such a column as;
    sqlite3 raises OverflowError on one.
    """

    return str(value) if value not in _SQLITE_INTEGERS else value


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
CONVERSIONS: Dict[DatabaseType, Dict[type, Optional[Conversion]]] = {
    DatabaseType.POSTGRESQL: {datetime.timedelta: _duration, dict: _postgresqlJson, list: _postgresqlList},
    DatabaseType.MYSQL: {datetime.timedelta: _duration, dict: _json, list: _json, uuid.UUID: _text, datetime.time: _iso},
    DatabaseType.MARIADB: {datetime.timedelta: _duration, dict: _json, list: _json, uuid.UUID: _text, datetime.time: _iso},
    DatabaseType.ORACLE: {datetime.timedelta: _duration, dict: _json, list: _json, uuid.UUID: _text, datetime.time: _iso},
    DatabaseType.MSSQL: {datetime.timedelta: _duration, dict: _json, list: _json, datetime.datetime: _isoDateTime, datetime.time: _iso},
    DatabaseType.SQLITE: {datetime.timedelta: _duration, dict: _json, list: _json, uuid.UUID: _text, datetime.time: _iso,
                          datetime.datetime: _isoDateTime, datetime.date: _iso, decimal.Decimal: _text, int: _sqliteInteger, bool: None},
    DatabaseType.DUCKDB: {datetime.timedelta: _duration},
    }

# The databases whose conversions depend on the column a value goes to, whose
# type codes cost a statement per table to read.
WANTS_COLUMN_TYPES = frozenset({DatabaseType.POSTGRESQL})

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


def prepareValues(databaseType: DatabaseType, rows: List[Tuple[Any, ...]], columnTypes: Optional[Sequence[Any]] = None) -> List[Tuple[Any, ...]]:
    """`rows` as `databaseType`'s driver must receive them.

    Every chunk of every load comes through here, so it does as little as it
    can: the distinct types in the chunk are gathered in C, and a chunk with
    none to convert is returned as it is. Otherwise only the columns holding
    one are converted, and the rest carried through untouched.
    """

    if not rows:
        return rows

    if all(conversionFor(databaseType, kind) is None for kind in set(map(type, itertools.chain.from_iterable(rows)))):
        return rows

    columns = list(zip(*rows))
    changed = False
    for index, column in enumerate(columns):
        conversions = {kind: conversionFor(databaseType, kind) for kind in set(map(type, column))}
        if not any(conversions.values()):
            continue
        columnType = columnTypes[index] if columnTypes is not None and index < len(columnTypes) else None
        converted = tuple(value if (conversion := conversions[type(value)]) is None else conversion(value, columnType) for value in column)
        # A conversion may leave every value as it was -- SQLite's integers,
        # nearly always within 64 bits -- and the chunk is then kept whole.
        if any(new is not old for new, old in zip(converted, column)):
            columns[index] = converted
            changed = True

    return list(zip(*columns)) if changed else rows


def prepareParameters(databaseType: DatabaseType, parameters: Sequence[Any]) -> Tuple[Any, ...]:
    """Bound query parameters, such as a watermark, as the driver must take
    them: converted as a one-row load would be.
    """

    return prepareValues(databaseType, [tuple(parameters)])[0]

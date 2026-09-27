"""The column types a file target writes, as a job declares them in
`targetColumnTypes` and as the writer infers them: one small vocabulary,
parsed here without pyarrow, so `bauta validate` checks a declaration
offline and without the files extra installed.
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional


# Arrow's decimal128 holds 38 digits; every engine reading Parquet takes it.
MAXIMUM_DECIMAL_PRECISION = 38

_INTEGERS = ('int8', 'int16', 'int32', 'int64', 'uint8', 'uint16', 'uint32', 'uint64')

# Each kind, and the other names it may be declared by.
KINDS = {
    'string': ('text', 'varchar'),
    'binary': ('bytes',),
    'bool': ('boolean',),
    'float32': ('float',),
    'float64': ('double',),
    'date': (),
    'time': (),
    # Without a time zone: the wall-clock value, as the source held it.
    'timestamp': (),
    # An instant, written in UTC.
    'timestamptz': (),
    **{integer: () for integer in _INTEGERS},
    }

_ALIASES = {alias: kind for kind, aliases in KINDS.items() for alias in aliases}

_DECIMAL = re.compile(r'^decimal\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)$')


class ColumnType(NamedTuple):
    """One column's type in a file. `precision` and `scale` are a decimal's
    alone.
    """

    kind: str
    precision: Optional[int] = None
    scale: Optional[int] = None

    def __str__(self) -> str:

        if self.kind == 'decimal':
            return 'decimal({},{})'.format(self.precision, self.scale)

        return self.kind


def parseColumnType(text: str) -> ColumnType:
    """A declared type, such as `int64`, `decimal(18,2)` or `timestamptz`;
    ValueError naming the choices otherwise.
    """

    spelled = text.strip().lower()

    match = _DECIMAL.match(spelled)
    if match:
        precision, scale = int(match.group(1)), int(match.group(2))
        if not 1 <= precision <= MAXIMUM_DECIMAL_PRECISION:
            raise ValueError('decimal precision must be from 1 to {}, got {}'.format(MAXIMUM_DECIMAL_PRECISION, precision))
        if scale > precision:
            raise ValueError('decimal scale {} is larger than its precision {}'.format(scale, precision))
        return ColumnType('decimal', precision, scale)

    kind = _ALIASES.get(spelled, spelled)
    if kind not in KINDS:
        raise ValueError('{!r} is not a column type; choose from {}, or decimal(precision,scale)'.format(text, ', '.join(KINDS)))

    return ColumnType(kind)


def isInteger(columnType: ColumnType) -> bool:

    return columnType.kind in _INTEGERS

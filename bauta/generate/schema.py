"""Creates target tables from source ones, and empties targets before a refresh.

Source types map through a small portable vocabulary into the target's
dialect; anything it can't express becomes text, with a comment. Only a
table's shape is copied: columns, nullability, primary and foreign keys --
not indexes, defaults, checks, triggers or grants.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

from ..configuration import DatabaseType
from ..database.dialects import ColumnDefinition, ForeignKey, bareName, quoteFolded, quoteFoldedTable, splitTableName, tooLongName


class SchemaError(Exception):
    """A schema that can't be generated or cleared as asked."""


class PortableType(NamedTuple):
    """A column type in terms every dialect can render.

    kind is one of: smallint, integer, bigint, decimal, float, boolean, text,
    fixedText, date, timestamp, timestampTz, time, binary, uuid, json.

    `precision` and `scale` size a decimal. On an integer kind, `precision` is
    the digits the source column holds, set only where that is more than the
    kind's name suggests -- SQLite's 64-bit INTEGER -- so the target's
    narrower type can be noted.
    """

    kind: str
    length: Optional[int] = None
    precision: Optional[int] = None
    scale: Optional[int] = None
    note: Optional[str] = None


class TableDefinition(NamedTuple):

    name: str
    columns: List[ColumnDefinition]
    primaryKey: List[str]
    foreignKeys: List[ForeignKey]


class Statement(NamedTuple):
    """One DDL statement, with any notes about lossy type choices."""

    table: str
    sql: str
    notes: List[str]


INTEGER_BOOLEAN_NOTE = 'the source stores this as an integer, so it stays one'


def _integerForPrecision(precision: Optional[int]) -> PortableType:

    if precision is None:
        return PortableType('bigint', note='unconstrained integer; mapped to a 64-bit integer')
    if precision <= 4:
        return PortableType('smallint')
    if precision <= 9:
        return PortableType('integer')
    if precision <= 18:
        return PortableType('bigint')

    return PortableType('decimal', precision=precision, scale=0)


def _text(length: Optional[int], fixed: bool = False) -> PortableType:
    """Unbounded when the catalog says so: None, or -1 for SQL Server's (MAX)."""

    if length is None or length < 0:
        return PortableType('text')

    return PortableType('fixedText' if fixed else 'text', length=length)


def portableType(sourceType: DatabaseType, column: ColumnDefinition) -> PortableType:
    """Maps one source column to the portable vocabulary."""

    name = column.dataType.lower().strip()
    base = re.sub(r'\(.*?\)', '', name).strip()
    # MySQL and MariaDB report `int unsigned` as one type name; every other
    # attribute (`zerofill`, character sets) is dropped the same way.
    unsigned = base.endswith(' unsigned')
    if unsigned:
        base = base[:-len(' unsigned')].strip()

    if sourceType == DatabaseType.ORACLE:
        if base == 'number':
            # INTEGER is stored as NUMBER with scale 0 and no precision.
            if column.scale == 0:
                return _integerForPrecision(column.precision)
            if column.precision is None:
                return PortableType('decimal', note='unconstrained NUMBER; mapped to an unbounded decimal')
            return PortableType('decimal', precision=column.precision, scale=column.scale)
        if base in ('float', 'binary_double', 'binary_float'):
            return PortableType('float')
        if base in ('varchar2', 'nvarchar2', 'varchar'):
            return _text(column.length)
        if base in ('char', 'nchar'):
            return _text(column.length, fixed=True)
        if base in ('clob', 'nclob', 'long'):
            return PortableType('text')
        if base == 'date':
            return PortableType('timestamp', note='Oracle DATE carries a time of day; mapped to a timestamp')
        if base.startswith('timestamp'):
            return PortableType('timestampTz' if 'time zone' in base else 'timestamp')
        if base in ('blob', 'raw', 'long raw'):
            return PortableType('binary')
        return PortableType('text', note='unrecognized Oracle type {}; mapped to text'.format(column.dataType))

    if sourceType == DatabaseType.SQLITE:
        # SQLite's own type-affinity rules, in its documented order.
        upper = base.upper()
        if 'INT' in upper:
            # Whatever it is declared as, SQLite stores an integer in up to 8 bytes.
            return PortableType('bigint') if 'BIG' in upper else PortableType('integer', precision=SQLITE_INTEGER_DIGITS)
        if 'BOOL' in upper:
            return PortableType('smallint', note=INTEGER_BOOLEAN_NOTE)
        if any(word in upper for word in ('CHAR', 'CLOB', 'TEXT')):
            return _text(column.length, fixed=upper in ('CHAR', 'NCHAR'))
        if 'BLOB' in upper or not upper:
            return PortableType('binary') if upper else PortableType('text', note='no declared type; mapped to text')
        if any(word in upper for word in ('REAL', 'FLOA', 'DOUB')):
            return PortableType('float')
        if 'DATETIME' in upper or 'TIMESTAMP' in upper:
            return PortableType('timestamp')
        if 'DATE' in upper:
            return PortableType('date')
        if 'TIME' in upper:
            return PortableType('time')
        if any(word in upper for word in ('DEC', 'NUM')):
            return PortableType('decimal', precision=column.precision, scale=column.scale)
        return PortableType('text', note='unrecognized SQLite type {}; mapped to text'.format(column.dataType))

    if sourceType == DatabaseType.DUCKDB:
        # DuckDB's own names; the ones it shares with PostgreSQL fall through
        # to the table below. Nested types have no counterpart elsewhere.
        if base == 'boolean':
            return PortableType('boolean')
        if base == 'utinyint':
            return PortableType('smallint')
        if base == 'usmallint':
            return PortableType('integer')
        if base == 'uinteger':
            return PortableType('bigint')
        if base in ('ubigint', 'hugeint', 'uhugeint'):
            # HUGEINT's range runs to 39 digits, one past what DECIMAL holds on
            # DuckDB, Oracle and SQL Server; a target clamps it, and says so.
            digits = {'ubigint': UNSIGNED_BIGINT_DIGITS, 'hugeint': 39, 'uhugeint': 39}[base]
            return PortableType('decimal', precision=digits, scale=0,
                                note='DuckDB {} has no integer counterpart; mapped to a decimal of {} digits'.format(base.upper(), digits))
        if base in ('timestamp_s', 'timestamp_ms', 'timestamp_ns', 'datetime'):
            return PortableType('timestamp')
        if base.endswith(']') or base.startswith(('struct', 'map', 'union', 'list')):
            return PortableType('json', note='DuckDB {} has no counterpart; mapped to JSON'.format(column.dataType))
        if base == 'interval':
            return PortableType('text', note='DuckDB INTERVAL has no portable counterpart; mapped to text')

    # MySQL, MariaDB, PostgreSQL, SQL Server and DuckDB all report through
    # information_schema, with names that overlap enough to share one table.
    if unsigned and base in _UNSIGNED:
        # MySQL's unsigned integers run past the signed type of the same name
        # -- a SMALLINT UNSIGNED to 65535, an INT UNSIGNED to 4294967295 -- so
        # each takes the next type up, and BIGINT UNSIGNED, which no target's
        # integer holds, a decimal.
        return _UNSIGNED[base]
    if base in ('tinyint', 'smallint', 'int2'):
        return PortableType('smallint')
    if base in ('int', 'integer', 'mediumint', 'int4', 'serial'):
        return PortableType('integer')
    if base in ('bigint', 'int8', 'bigserial'):
        return PortableType('bigint')
    if base in ('decimal', 'numeric', 'money', 'smallmoney'):
        if base in ('money', 'smallmoney'):
            return PortableType('decimal', precision=19, scale=4)
        return PortableType('decimal', precision=column.precision, scale=column.scale)
    if base in ('float', 'double', 'double precision', 'real', 'float4', 'float8'):
        return PortableType('float')
    # Only these two drivers hand back real booleans. MySQL's BOOLEAN is a
    # TINYINT and its BIT(n) a bit string, and both arrive as integers, which
    # PostgreSQL would refuse to load into a BOOLEAN column.
    if (sourceType == DatabaseType.POSTGRESQL and base in ('boolean', 'bool')) or (sourceType == DatabaseType.MSSQL and base == 'bit'):
        return PortableType('boolean')
    if base in ('bit', 'boolean', 'bool'):
        return PortableType('bigint' if base == 'bit' else 'smallint', note=INTEGER_BOOLEAN_NOTE)
    if base in ('varchar', 'nvarchar', 'character varying', 'varchar2'):
        return _text(column.length)
    if base in ('char', 'nchar', 'character', 'bpchar'):
        return _text(column.length, fixed=True)
    if base in ('text', 'ntext', 'tinytext', 'mediumtext', 'longtext', 'citext', 'xml', 'enum', 'set'):
        # MySQL reports TEXT's 65535-byte limit as a length; it isn't one in
        # characters, and the type is unbounded for any practical purpose.
        note = 'MySQL {} values; mapped to text'.format(base.upper()) if base in ('enum', 'set') else None
        return PortableType('text', note=note)
    if base == 'date':
        return PortableType('date')
    if base in ('datetime', 'datetime2', 'smalldatetime', 'timestamp', 'timestamp without time zone'):
        return PortableType('timestamp')
    if base in ('datetimeoffset', 'timestamp with time zone', 'timestamptz'):
        return PortableType('timestampTz')
    if base in ('time', 'time without time zone'):
        return PortableType('time')
    if base in ('binary', 'varbinary', 'blob', 'tinyblob', 'mediumblob', 'longblob', 'bytea', 'image'):
        return PortableType('binary')
    if base in ('uuid', 'uniqueidentifier'):
        return PortableType('uuid')
    if base in ('json', 'jsonb'):
        return PortableType('json')

    return PortableType('text', note='unrecognized type {}; mapped to text'.format(column.dataType))


# Where a key column can't be an unbounded type -- MySQL can't index TEXT,
# SQL Server can't index NVARCHAR(MAX), Oracle can't index a CLOB -- it gets
# this bounded length instead.
KEY_TEXT_LENGTH = 255

# Oracle keeps a TIME as text. MySQL's driver returns a TIME as a timedelta,
# whose text runs to '-35 days, 1:00:01.999999' at the type's limits.
ORACLE_TIME_LENGTH = 32

SQLITE_INTEGER_DIGITS = 19

# The digits of an unsigned 64-bit integer: MySQL's BIGINT UNSIGNED and
# DuckDB's UBIGINT reach 18446744073709551615.
UNSIGNED_BIGINT_DIGITS = 20

# What each of MySQL's and MariaDB's unsigned integers is copied as: the
# smallest type holding its whole range.
_UNSIGNED = {
    'tinyint': PortableType('smallint'),
    'smallint': PortableType('integer'),
    'mediumint': PortableType('integer'),
    'int': PortableType('bigint'),
    'integer': PortableType('bigint'),
    'bigint': PortableType('decimal', precision=UNSIGNED_BIGINT_DIGITS, scale=0,
                           note='BIGINT UNSIGNED runs past every signed 64-bit integer; mapped to a decimal of 20 digits'),
    }

# The digits each target's integer types take. SQLite gives all three the same
# 64-bit affinity whatever they are called, and Oracle renders them as
# NUMBER(n), which holds every value of that many digits.
INTEGER_DIGITS = {'smallint': 4, 'integer': 9, 'bigint': 18}
_TARGET_INTEGER_DIGITS = {
    DatabaseType.ORACLE: {'smallint': 5, 'integer': 10, 'bigint': 19},
    DatabaseType.SQLITE: {'smallint': 19, 'integer': 19, 'bigint': 19},
    }


def _decimal(name: str, precision: Optional[int], scale: Optional[int], maxPrecision: int, maxScale: int,
             defaultScale: Optional[int] = None, unbounded: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """A decimal in the target's limits, and a note wherever they cut it.

    `unbounded` is what this target calls a decimal of any size. Without one,
    a source column that declared no precision has to be given the largest
    decimal the target has, which is a choice, not a copy.
    """

    if not precision:
        if unbounded:
            return unbounded, None
        rendered = '{}({},{})'.format(name, maxPrecision, defaultScale if defaultScale is not None else maxScale)
        return rendered, 'the source declares no precision or scale; mapped to {}, which rounds anything longer'.format(rendered)

    keptPrecision, keptScale = min(precision, maxPrecision), min(scale or 0, maxScale)
    rendered = '{}({},{})'.format(name, keptPrecision, keptScale)
    if (keptPrecision, keptScale) != (precision, scale or 0):
        return rendered, 'the source is {}({},{}), more than this target holds; clamped to {}'.format(name, precision, scale or 0, rendered)

    return rendered, None


# The collation a key column needs on the databases whose default compares
# text loosely, where two keys the source keeps apart would otherwise become
# one row: `a` and `A`, `ss` and the German sharp s, and on MySQL and MariaDB
# `a` and `a ` too. SQL Server compares trailing spaces loosely whatever the
# collation, so there it refuses the second key instead of merging it.
KEY_COLLATIONS = {
    DatabaseType.MYSQL: 'utf8mb4_0900_bin',
    DatabaseType.MARIADB: 'utf8mb4_nopad_bin',
    DatabaseType.MSSQL: 'Latin1_General_BIN2',
    }

# The kinds a collation applies to.
_COLLATED_KINDS = ('text', 'fixedText', 'uuid')


def renderType(targetType: DatabaseType, portable: PortableType, isKey: bool) -> Tuple[str, Optional[str]]:
    """The target dialect's type for a portable one, and a note if it's lossy.

    A key column also carries a collation that compares text exactly, where the
    target's default wouldn't; see KEY_COLLATIONS. Every column a key is made
    of gets it, on both sides of a foreign key, since the two must agree.
    """

    rendered, note = _renderedType(targetType, portable, isKey)
    collation = KEY_COLLATIONS.get(targetType) if isKey and portable.kind in _COLLATED_KINDS else None

    return ('{} COLLATE {}'.format(rendered, collation) if collation else rendered), note


def _renderedType(targetType: DatabaseType, portable: PortableType, isKey: bool) -> Tuple[str, Optional[str]]:

    kind, length, precision, scale = portable.kind, portable.length, portable.precision, portable.scale
    note = None

    if kind == 'text' and length is None and isKey and targetType not in (DatabaseType.POSTGRESQL, DatabaseType.SQLITE, DatabaseType.DUCKDB):
        length = KEY_TEXT_LENGTH
        note = 'unbounded text in a key; bounded to {} characters'.format(KEY_TEXT_LENGTH)

    if kind in INTEGER_DIGITS and precision:
        held = _TARGET_INTEGER_DIGITS.get(targetType, INTEGER_DIGITS)[kind]
        if precision > held:
            note = 'the source holds values of {} digits and this type {}; longer ones will be refused as they load'.format(precision, held)

    if targetType in (DatabaseType.MYSQL, DatabaseType.MARIADB):
        if kind == 'decimal':
            return _decimal('DECIMAL', precision, scale, 65, 30)
        if kind == 'text' and (length is None or length > 16383):
            return 'LONGTEXT', note
        rendered = {
            'smallint': 'SMALLINT', 'integer': 'INT', 'bigint': 'BIGINT', 'float': 'DOUBLE', 'boolean': 'BOOLEAN',
            'text': 'VARCHAR({})'.format(length), 'fixedText': 'CHAR({})'.format(length), 'date': 'DATE',
            'timestamp': 'DATETIME(6)', 'timestampTz': 'DATETIME(6)', 'time': 'TIME(6)', 'binary': 'LONGBLOB',
            'uuid': 'CHAR(36)', 'json': 'JSON',
            }[kind]
        if kind == 'timestampTz':
            note = 'MySQL has no time-zone-aware timestamp; the offset is not kept'
        return rendered, note

    if targetType == DatabaseType.POSTGRESQL:
        if kind == 'decimal':
            return _decimal('NUMERIC', precision, scale, 1000, 1000, unbounded='NUMERIC')
        return {
            'smallint': 'SMALLINT', 'integer': 'INTEGER', 'bigint': 'BIGINT', 'float': 'DOUBLE PRECISION', 'boolean': 'BOOLEAN',
            'text': 'VARCHAR({})'.format(length) if length else 'TEXT', 'fixedText': 'CHAR({})'.format(length), 'date': 'DATE',
            'timestamp': 'TIMESTAMP', 'timestampTz': 'TIMESTAMPTZ', 'time': 'TIME', 'binary': 'BYTEA', 'uuid': 'UUID', 'json': 'JSONB',
            }[kind], note

    if targetType == DatabaseType.MSSQL:
        if kind == 'decimal':
            return _decimal('DECIMAL', precision, scale, 38, 38, defaultScale=10)
        if kind in ('text', 'json') and (length is None or length > 4000):
            return 'NVARCHAR(MAX)', note
        return {
            'smallint': 'SMALLINT', 'integer': 'INT', 'bigint': 'BIGINT', 'float': 'FLOAT', 'boolean': 'BIT',
            'text': 'NVARCHAR({})'.format(length), 'fixedText': 'NCHAR({})'.format(length), 'date': 'DATE',
            'timestamp': 'DATETIME2', 'timestampTz': 'DATETIMEOFFSET', 'time': 'TIME', 'binary': 'VARBINARY(MAX)',
            'uuid': 'UNIQUEIDENTIFIER',
            }[kind], note

    if targetType == DatabaseType.ORACLE:
        if kind == 'decimal':
            return _decimal('NUMBER', precision, scale, 38, 127, unbounded='NUMBER')
        if kind in ('text', 'json') and (length is None or length > 4000):
            return 'CLOB', note
        if kind == 'time':
            return 'VARCHAR2({} CHAR)'.format(ORACLE_TIME_LENGTH), \
                'Oracle has no TIME type; mapped to text, wide enough for the day-long values MySQL\'s TIME allows'
        if kind == 'boolean':
            return 'NUMBER(1)', 'mapped to NUMBER(1)'
        if kind == 'timestampTz':
            return 'TIMESTAMP WITH TIME ZONE', 'Oracle keeps the offset of the session that loads the row, not the source\'s; ' \
                'the instant moves unless that session is UTC'
        return {
            'smallint': 'NUMBER(5)', 'integer': 'NUMBER(10)', 'bigint': 'NUMBER(19)', 'float': 'BINARY_DOUBLE',
            'text': 'VARCHAR2({} CHAR)'.format(length), 'fixedText': 'CHAR({} CHAR)'.format(length), 'date': 'DATE',
            'timestamp': 'TIMESTAMP', 'timestampTz': 'TIMESTAMP WITH TIME ZONE', 'binary': 'BLOB', 'uuid': 'VARCHAR2(36 CHAR)',
            }[kind], note

    if targetType == DatabaseType.DUCKDB:
        if kind == 'decimal':
            # DuckDB's bare DECIMAL is DECIMAL(18,3), which would round.
            return _decimal('DECIMAL', precision, scale, 38, 38, defaultScale=10)
        return {
            'smallint': 'SMALLINT', 'integer': 'INTEGER', 'bigint': 'BIGINT', 'float': 'DOUBLE', 'boolean': 'BOOLEAN',
            'text': 'VARCHAR', 'fixedText': 'VARCHAR', 'date': 'DATE', 'timestamp': 'TIMESTAMP', 'timestampTz': 'TIMESTAMPTZ', 'time': 'TIME',
            'binary': 'BLOB', 'uuid': 'UUID', 'json': 'JSON',
            }[kind], note or ('DuckDB has no fixed-length text; mapped to VARCHAR, which keeps no padding' if kind == 'fixedText' else None)

    # SQLite: declared names that give each value the right affinity.
    if kind == 'decimal':
        # TEXT, not DECIMAL: SQLite has no exact decimal type, and a column
        # whose declared name gives it NUMERIC affinity stores the value as an
        # integer or a float, rounding it. Text keeps every digit, and the
        # drivers that read the copy parse it back.
        return 'TEXT', ('SQLite has no exact decimal type; stored as text, which keeps every digit, rather than as the '
                        'float a DECIMAL column would hold')
    return {
        'smallint': 'SMALLINT', 'integer': 'INTEGER', 'bigint': 'BIGINT', 'float': 'REAL', 'boolean': 'BOOLEAN',
        'text': 'VARCHAR({})'.format(length) if length else 'TEXT', 'fixedText': 'CHAR({})'.format(length), 'date': 'DATE',
        'timestamp': 'TIMESTAMP', 'timestampTz': 'TIMESTAMP', 'time': 'TIME', 'binary': 'BLOB', 'uuid': 'VARCHAR(36)', 'json': 'TEXT',
        }[kind], note


def tableKey(table: str) -> str:
    """A table name as this module matches it: each part without the quotes a
    reserved word or a mixed-case name needs, and upper-cased, since the
    catalogs a source's foreign keys come from report names bare.
    """

    schema, name = splitTableName(table)

    return '.'.join(bareName(part) for part in ([schema, name] if schema else [name])).upper()


def readTable(database: Any, table: str, foreignKeys: Sequence[ForeignKey]) -> TableDefinition:
    """A table's shape from a live source Database, named as it was asked for
    rather than as the catalog spells it -- upper case on Oracle -- so every
    table one invocation creates is named the same way.
    """

    columns = database.getColumnDefinitions(table)
    if not columns:
        raise SchemaError('table {} was not found in the source database'.format(table))

    return TableDefinition(name=table, columns=columns, primaryKey=database.getPrimaryColumnNames(table),
                           foreignKeys=[foreignKey for foreignKey in foreignKeys if tableKey(foreignKey.table) == tableKey(table)])


def orderParentsFirst(tables: Iterable[str], foreignKeys: Sequence[ForeignKey]) -> List[str]:
    """Tables ordered so each comes after every table it references.

    Self-references don't constrain the order. A cycle between tables can't be
    ordered at all, and raises.
    """

    byName = {tableKey(table): table for table in tables}
    parents: Dict[str, Set[str]] = {name: set() for name in byName}

    for foreignKey in foreignKeys:
        child, parent = tableKey(foreignKey.table), tableKey(foreignKey.referencedTable)
        if child in byName and parent in byName and child != parent:
            parents[child].add(parent)

    ordered: List[str] = []
    remaining = set(byName)

    while remaining:
        ready = sorted(name for name in remaining if not parents[name] & remaining)
        if not ready:
            raise SchemaError('foreign keys form a cycle among: {}, so no order puts every table after the tables it references. '
                              'Take those tables one at a time, or leave one of the keys out of the set'.format(
                                  ', '.join(sorted(byName[name] for name in remaining))))
        ordered += ready
        remaining -= set(ready)

    return [byName[name] for name in ordered]


def createStatements(sourceType: DatabaseType, targetType: DatabaseType, tables: Sequence[TableDefinition],
                     includeForeignKeys: bool = True, stageSuffix: Optional[str] = None, stagesOnly: bool = False) -> List[Statement]:
    """CREATE TABLE statements for `tables`, parents first. Foreign keys only
    between tables in the set; stage tables (`<table><stageSuffix>`) get none.

    A stage table cannot carry them: a key follows the table it was declared
    on, so once its parent is swapped the key checks the emptied old table and
    every row is refused, and a key to the parent's own stage table would check
    the rows that swap displaced. See how a swap works in docs/design.md.
    """

    order = orderParentsFirst([table.name for table in tables], [foreignKey for table in tables for foreignKey in table.foreignKeys]
                              if includeForeignKeys else [])
    byName = {tableKey(table.name): table for table in tables}
    names = {tableKey(table.name): table.name for table in tables}
    unique = _uniqueConstraints(tables, names) if includeForeignKeys else {}
    taken: Set[str] = set()
    statements = []

    for name in order:
        table = byName[tableKey(name)]
        if not stagesOnly:
            statements.append(_createTable(sourceType, targetType, table, table.name, includeForeignKeys, names,
                                           unique.get(tableKey(name), []), taken))
        if stageSuffix:
            # No foreign keys, so none of the unique constraints they need either.
            statements.append(_createTable(sourceType, targetType, table, table.name + stageSuffix, False, names, (), taken))

    return statements


def _uniqueConstraints(tables: Sequence[TableDefinition], created: Dict[str, str]) -> Dict[str, List[Tuple[str, ...]]]:
    """Per table, the column groups a foreign key in the set references that its
    primary key doesn't already cover.

    Every dialect requires a unique constraint behind a foreign key, and a
    source can declare one on a UNIQUE column that isn't the primary key --
    `sku_aliases.sku -> products.sku`. Only the primary key is copied
    otherwise, and the key is then refused.
    """

    required: Dict[str, List[Tuple[str, ...]]] = {}

    for table in tables:
        for foreignKey in table.foreignKeys:
            parentName = tableKey(foreignKey.referencedTable)
            parent = next((candidate for candidate in tables if tableKey(candidate.name) == parentName), None)
            if parent is None or parentName not in created:
                continue
            columns = tuple(column.upper() for column in foreignKey.referencedColumns)
            # Order doesn't matter: the primary key's own index covers its columns in any order.
            if set(columns) == {column.upper() for column in parent.primaryKey}:
                continue
            groups = required.setdefault(parentName, [])
            if columns not in groups:
                groups.append(columns)

    return required


def _createTable(sourceType: DatabaseType, targetType: DatabaseType, table: TableDefinition, name: str,
                 includeForeignKeys: bool, created: Dict[str, str], unique: Sequence[Tuple[str, ...]], taken: Set[str]) -> Statement:

    keyColumns = {column.upper() for column in table.primaryKey}
    for foreignKey in table.foreignKeys:
        keyColumns.update(column.upper() for column in foreignKey.columns)
    # The columns another table's foreign key references, which carry the
    # unique constraints below: a key and what it references must be collated
    # alike, or MySQL and SQL Server refuse the key outright.
    keyColumns.update(column.upper() for group in unique for column in group)

    lines = []
    notes = []

    def quoted(names: Iterable[str]) -> str:
        return ', '.join(quoteFolded(targetType, name) for name in names)

    for column in table.columns:
        portable = portableType(sourceType, column)
        rendered, renderNote = renderType(targetType, portable, column.name.upper() in keyColumns)
        nullable = column.nullable and column.name.upper() not in {key.upper() for key in table.primaryKey}
        lines.append('{} {}{}'.format(quoteFolded(targetType, column.name), rendered, '' if nullable else ' NOT NULL'))
        for note in (portable.note, renderNote):
            if note:
                notes.append('{}: {}'.format(column.name, note))

    if table.primaryKey:
        lines.append('PRIMARY KEY ({})'.format(quoted(table.primaryKey)))

    spelled = {column.name.upper(): column.name for column in table.columns}
    for columns in unique:
        lines.append('UNIQUE ({})'.format(quoted(spelled.get(column, column) for column in columns)))

    if includeForeignKeys:
        for foreignKey in table.foreignKeys:
            if tableKey(foreignKey.referencedTable) not in created:
                notes.append('foreign key {} -> {} left out: {} is not being created'.format(
                    ', '.join(foreignKey.columns), foreignKey.referencedTable, foreignKey.referencedTable))
                continue
            lines.append('CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({})'.format(
                _constraintName(targetType, foreignKey.name, taken), quoted(foreignKey.columns),
                quoteFoldedTable(targetType, created[tableKey(foreignKey.referencedTable)]), quoted(foreignKey.referencedColumns)))

    tooLong = tooLongName(targetType, name)
    if tooLong is not None:
        notes.append('name: {}'.format(tooLong))

    # Quoted like the columns, so a table named for a reserved word -- `group`,
    # `order` -- is created rather than failing to parse.
    sql = 'CREATE TABLE {} (\n    {}\n)'.format(quoteFoldedTable(targetType, name), ',\n    '.join(lines))

    return Statement(table=name, sql=sql, notes=notes)


def _constraintName(targetType: DatabaseType, name: str, taken: Set[str]) -> str:
    """Source constraint names, with anything outside [A-Za-z0-9_] replaced and
    cut to 63 characters, the shortest limit among the dialects.

    A name already used gets a numbered suffix: constraint names are per table
    on PostgreSQL and SQLite but must be unique across the schema on MySQL,
    MariaDB, Oracle and SQL Server, and cutting to the limit makes two long
    names that share a prefix equal. Either way `--apply` would fail partway
    and leave half a schema behind. Names are compared upper-cased, since
    Oracle and MySQL fold them.
    """

    cleaned = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if not re.match(r'[A-Za-z]', cleaned):
        cleaned = 'fk_' + cleaned

    limit = 63 if targetType != DatabaseType.MYSQL else 64
    chosen = cleaned[:limit]
    attempt = 1
    while chosen.upper() in taken:
        attempt += 1
        suffix = '_{}'.format(attempt)
        chosen = cleaned[:limit - len(suffix)] + suffix

    taken.add(chosen.upper())

    return chosen


def renderScript(statements: Sequence[Statement], heading: Sequence[str]) -> str:
    """The statements as one SQL script, each note as a comment above its table."""

    parts = ['\n'.join('-- ' + line if line else '--' for line in heading)] if heading else []

    for statement in statements:
        comments = ''.join('-- {}\n'.format(note) for note in statement.notes)
        parts.append('{}{};'.format(comments, statement.sql))

    return '\n\n'.join(parts) + '\n'


def clearOrder(tables: Iterable[str], foreignKeys: Sequence[ForeignKey]) -> List[str]:
    """Tables ordered so each is emptied before the tables it references."""

    return list(reversed(orderParentsFirst(tables, foreignKeys)))


def clearTables(database: Any, tables: Sequence[str]) -> List[Tuple[str, int]]:
    """Deletes every row of `tables`, in one transaction, children first, and
    returns (table, rows deleted). DELETE, since three dialects won't
    TRUNCATE a table a foreign key references.

    On DuckDB, one transaction per table: it checks a foreign key against what
    is committed, so a parent's DELETE is refused while its children's DELETE
    is still uncommitted. A clear stopped part-way there leaves the tables it
    reached empty, and running it again finishes it.
    """

    order = clearOrder(tables, database.getForeignKeys())
    cleared = []
    commitEach = not database.dialect.checksForeignKeysWithinTransaction()

    try:
        for table in order:
            cleared.append((table, database.execute('DELETE FROM {}'.format(database.statementName(table)))))
            if commitEach:
                database.commit()
        database.commit()
    except Exception:
        database.rollback()
        raise

    return cleared

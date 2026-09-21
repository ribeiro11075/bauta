"""How a table or column name is read, quoted and folded, the same way on
all six databases. See "How names are written" in docs/design.md.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from ...configuration import IDENTIFIER, DatabaseType



# How each database quotes an identifier, where it isn't with double quotes.
_IDENTIFIER_QUOTES = {DatabaseType.MYSQL: ('`', '`'), DatabaseType.MARIADB: ('`', '`'), DatabaseType.MSSQL: ('[', ']')}

# How a database folds an unquoted identifier, where it doesn't keep it as
# written. The others compare identifiers case-insensitively anyway.
_UNQUOTED_CASE = {DatabaseType.ORACLE: str.upper, DatabaseType.POSTGRESQL: str.lower}

# Every quoting style the dialects use, for reading a name written in any of
# them. No database allows these characters in a name written without quotes,
# so a name carrying one was quoted.
_QUOTE_PAIRS = dict([('"', '"')] + list(_IDENTIFIER_QUOTES.values()))

# The longest name each database keeps, and whether it counts bytes or
# characters. A longer name isn't refused: it is silently cut to the limit, so
# two names alike up to it become one table. SQLite has no limit.
IDENTIFIER_LIMITS = {
    DatabaseType.POSTGRESQL: (63, 'bytes'),
    DatabaseType.MYSQL: (64, 'characters'),
    DatabaseType.MARIADB: (64, 'characters'),
    DatabaseType.ORACLE: (128, 'bytes'),
    DatabaseType.MSSQL: (128, 'characters'),
    }


def quoteIdentifier(databaseType: DatabaseType, name: str) -> str:
    """`name`, quoted, so a reserved word (`rank`, `order`) works as a column
    name. Quoting makes the name case-sensitive on Oracle and PostgreSQL, so
    `name` must be spelled as the catalog spells it.
    """

    opening, closing = _IDENTIFIER_QUOTES.get(databaseType, ('"', '"'))

    return opening + name.replace(closing, closing * 2) + closing


def quoteFolded(databaseType: DatabaseType, name: str) -> str:
    """`name` quoted as the database would store it unquoted -- upper case on
    Oracle, lower case on PostgreSQL -- for a column being created, which
    then answers to the same unquoted name it would have without quotes.
    """

    fold = _UNQUOTED_CASE.get(databaseType)
    if fold is not None and IDENTIFIER.match(name):
        name = fold(name)

    return quoteIdentifier(databaseType, name)


def splitTableName(table: str) -> Tuple[Optional[str], str]:
    """`schema.table` -> ('schema', 'table'); a bare `table` -> (None, 'table'),
    None meaning the connection's current schema. Both parts keep the spelling
    they were written in, quotes and all, so they can go back into a statement.

    A dot inside quotes belongs to the name: `dbo.[a.b]` is one table `a.b` in
    schema `dbo`, not a schema `dbo.[a`.
    """

    parts: List[str] = []
    current: List[str] = []
    closing = None

    for character in table:
        if closing is not None:
            closing = None if character == closing else closing
        elif character in _QUOTE_PAIRS:
            closing = _QUOTE_PAIRS[character]
        elif character == '.':
            parts.append(''.join(current))
            current = []
            continue
        current.append(character)

    parts.append(''.join(current))
    schema = '.'.join(parts[:-1])

    return schema or None, parts[-1]


def unqualifiedName(table: str) -> str:
    """The name without its schema -- what `RENAME TO` and sp_rename take."""

    return splitTableName(table)[1]


def _isQuoted(databaseType: DatabaseType, name: str) -> bool:

    opening, closing = _IDENTIFIER_QUOTES.get(databaseType, ('"', '"'))

    return len(name) > 1 and name.startswith(opening) and name.endswith(closing)


def catalogName(databaseType: DatabaseType, name: str) -> str:
    """`name` as the catalog holds it: a quoted name unquoted, and a name
    written without quotes folded the way this database folds one.

    Catalog queries bind these, so a table a person can only name in quotes --
    a reserved word, mixed case on Oracle or PostgreSQL, a space -- is found
    rather than looked up with its quotes still on, which used to report every
    such table as having no columns and no primary key.

    A name no database would accept without quotes is taken as it is written,
    since there is no unquoted spelling of it to fold -- the same rule
    quoteFolded follows, so the two agree on what a written name means.
    """

    if _isQuoted(databaseType, name):
        return bareName(name)

    fold = _UNQUOTED_CASE.get(databaseType)

    return fold(name) if fold is not None and IDENTIFIER.match(name) else name


def bareName(name: str) -> str:
    """`name` without whatever quotes it was written in, whichever database's
    style they are.

    For matching a name written in one database's spelling against a name read
    from another's, which audit and verify-references do ignoring case. A
    lookup against one database binds catalogName instead.
    """

    closing = _QUOTE_PAIRS.get(name[:1])
    if closing is not None and len(name) > 1 and name.endswith(closing):
        return name[1:-1].replace(closing * 2, closing)

    return name


def catalogTableName(databaseType: DatabaseType, table: str) -> Tuple[Optional[str], str]:
    """The schema and table a catalog query binds, from a name as written."""

    schema, name = splitTableName(table)

    return (None if schema is None else catalogName(databaseType, schema)), catalogName(databaseType, name)


def _quotedParts(databaseType: DatabaseType, table: str, quote: Callable[[DatabaseType, str], str]) -> str:

    schema, name = splitTableName(table)
    # A part written in quotes is spelled exactly as it means to be, whichever
    # quoting the caller asked for; only a bare part is the caller's to fold.
    parts = [quoteIdentifier(databaseType, bareName(part)) if _isQuoted(databaseType, part) else quote(databaseType, part)
             for part in ([schema, name] if schema else [name])]

    return '.'.join(parts)


def tooLongName(databaseType: DatabaseType, table: str) -> Optional[str]:
    """Says which part of `table` this database would cut, and to what, or None.

    PostgreSQL cutting a name to 63 bytes is silent: two jobs whose targets
    differ only past that loaded the same table, and the second swap then
    renamed over the first's rows.
    """

    limit = IDENTIFIER_LIMITS.get(databaseType)
    if limit is None:
        return None

    length, unit = limit
    for part in catalogTableName(databaseType, table):
        if part is None:
            continue
        measured = len(part.encode('utf-8')) if unit == 'bytes' else len(part)
        if measured > length:
            return '{} is {} {} long, and {} keeps only {}, so it names whatever other table shares its first {}'.format(
                part, measured, unit, databaseType.value, length, length)

    return None


def catalogTable(databaseType: DatabaseType, table: str) -> str:
    """A table name as the catalog holds it, schema and all: catalogTableName
    written back as one name.
    """

    schema, name = catalogTableName(databaseType, table)

    return '{}.{}'.format(schema, name) if schema else name


def quoteTableName(databaseType: DatabaseType, table: str) -> str:
    """A table name a catalog reported, quoted part by part for a statement --
    so `group` becomes `"group"` and stays one identifier, and a qualified name
    stays two. The spelling is kept as it is, which is how the catalog holds it.
    """

    return _quotedParts(databaseType, table, quoteIdentifier)


def quoteFoldedTable(databaseType: DatabaseType, table: str) -> str:
    """quoteTableName for a table being created, whose name is folded the way
    this database folds an unquoted one -- see quoteFolded -- so it answers to
    the same name unquoted. A name already quoted keeps its own spelling.
    """

    return _quotedParts(databaseType, table, quoteFolded)


def suffixedName(databaseType: DatabaseType, table: str, suffix: str) -> str:
    """`table` with `suffix` on its own name, keeping the spelling it was
    written in: a quoted name grows inside its quotes, since `[group]_tmp` is
    not a name SQL Server can parse.
    """

    schema, name = splitTableName(table)
    if _isQuoted(databaseType, name):
        name = quoteIdentifier(databaseType, bareName(name) + suffix)
    else:
        name += suffix

    return '{}.{}'.format(schema, name) if schema else name

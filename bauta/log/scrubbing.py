"""Removes data values from database error messages before they are reported.

Drivers quote the values a statement choked on, which may be unmasked
production values, and those messages reach logs, history and webhooks. Each
pattern matches a format seen from a real server and replaces only the value,
keeping which constraint or column failed. Where a server quotes the statement
itself, the whole quote goes, since values can't be told from the SQL around
them.

A message from anything but this package -- a driver's exception, or a
driver's own logger -- then has every quoted text left removed too, unless the
word before it says it names a table, column or constraint: a format no pattern
knows loses its detail rather than leaking its value. See
docs/concepts/security.md.

SQL Server's messages arrive as the repr of pymssql's (code, bytes) tuple, on
one line and with quotes that may be backslash-escaped, hence QUOTE -- and the
lookbehinds that keep a closing quote's backslash out of the value.
"""
from __future__ import annotations

import re
from typing import Iterator, List, Optional, Tuple

REDACTED = '<redacted>'

QUOTE = r"\\?'"

# Where a field of a PostgreSQL message ends: the next `DETAIL:  `-style line,
# the `LINE 1: ` quoting the statement, or the end of the text. A value may
# hold newlines, so a field isn't a line.
POSTGRESQL_FIELD_END = r'(?=\n(?:[A-Z][A-Z ]*:  |LINE \d+: )|\n?\Z)'

# Where a line of an Oracle message ends, for the same reason: at the next
# ORA- line or the link to Oracle's help that closes the message.
ORACLE_LINE_END = r'(?=\nORA-\d{5}: |\nHelp: https://docs\.oracle\.com/|\n?\Z)'

_PATTERNS: List[Tuple['re.Pattern[str]', str]] = [(re.compile(pattern, flags), replacement) for pattern, flags, replacement in (

    # PostgreSQL: DETAIL:  Key (email)=(ann@example.com) already exists.
    # A value may itself contain `) already exists`, so the key ends only where
    # its sentence ends the field -- or, for an exclusion constraint, where the
    # conflicting key starts, which the next pattern handles.
    (r'(Key \((?:(?!\)=\().)*\)=\().*?(\) (?:(?:already exists\.|is duplicated\.|is (?:not present in|still referenced from) table "[^"\n]*"\.)'
     + POSTGRESQL_FIELD_END + r'|conflicts with (?:existing )?key \()|\Z)',
     re.DOTALL, r'\1' + REDACTED + r'\2'),
    # PostgreSQL: ... conflicts with existing key (name)=(ann@example.com).
    (r'(conflicts with (?:existing )?key \((?:(?!\)=\().)*\)=\().*?(\)\.' + POSTGRESQL_FIELD_END + r'|\Z)', re.DOTALL, r'\1' + REDACTED + r'\2'),
    # PostgreSQL: LINE 1: INSERT INTO t VALUES (1, 'ann@example.com', ...  and the
    # caret line under it. psycopg's ClientCursor writes values into the
    # statement, and the server quotes the statement around the error.
    (r'(\nLINE \d+: ).*?' + POSTGRESQL_FIELD_END, re.DOTALL, r'\1' + REDACTED),
    # PostgreSQL: CONTEXT:  SQL statement "INSERT ... VALUES ('ann@example.com')"
    # and the function it ran in, on the line after.
    (r'(SQL statement ").*?("(?=\n(?:PL/pgSQL function |SQL function ))|"' + POSTGRESQL_FIELD_END + r'|\Z)', re.DOTALL, r'\1' + REDACTED + r'\2'),
    # PostgreSQL: DETAIL:  Failing row contains (4, null, 1).
    (r'(Failing row contains ).*?' + POSTGRESQL_FIELD_END, re.DOTALL, r'\1(' + REDACTED + ').'),
    # PostgreSQL: CONTEXT:  COPY customers, line 3, column email: "..."  (or the whole line, with no column)
    (r'(COPY [^\n,]+, line \d+(?:, column [^\n:]+)?: ).*?' + POSTGRESQL_FIELD_END, re.DOTALL, r'\1' + REDACTED),
    # PostgreSQL: CONTEXT:  JSON data, line 1: ...
    (r'(JSON data, line \d+: ).*?' + POSTGRESQL_FIELD_END, re.DOTALL, r'\1' + REDACTED),
    # PostgreSQL: DETAIL:  Token "..." is invalid.
    (r'(Token )".*?"( is invalid)', 0, r'\1"' + REDACTED + r'"\2'),
    # PostgreSQL: value "12345678901" is out of range for type integer
    (r'(value )".*?"( is out of range)', 0, r'\1"' + REDACTED + r'"\2'),
    # PostgreSQL: invalid byte sequence for encoding "UTF8": 0xe9 0x20
    (r'(byte sequence (?:for encoding "[^"\n]*": )?)0x[0-9a-fA-F]{2}(?: 0x[0-9a-fA-F]{2})*', 0, r'\1' + REDACTED),
    # PostgreSQL: invalid input syntax for type integer: "abc"
    (r'(invalid input (?:syntax|value) for [^\n:]*: ).*?' + POSTGRESQL_FIELD_END, re.DOTALL, r'\1"' + REDACTED + '"'),
    # Anything else that ends a line with a quoted value after a colon, as PostgreSQL's messages do
    (r'(: )"[^\n]*"(?=\n|\Z)', 0, r'\1"' + REDACTED + '"'),

    # MySQL and MariaDB: Duplicate entry 'ann@example.com' for key 'email'
    (r"(Duplicate entry ').*(' for key)", re.DOTALL, r'\1' + REDACTED + r'\2'),
    # MySQL and MariaDB: Incorrect integer value: 'abc' for column 'n' at row 1
    (r"(Incorrect [\w ]+ value: ').*(' for (?:column|function))", re.DOTALL, r'\1' + REDACTED + r'\2'),
    # MySQL and MariaDB: ... the right syntax to use near 'ann@example.com', 3)' at line 1
    (r"(to use near ').*(' at line \d+)", re.DOTALL, r'\1' + REDACTED + r'\2'),
    # MySQL and MariaDB: Truncated incorrect DOUBLE value: 'abc'
    (r"(Truncated incorrect [\w ]+ value: )[^\n]*", 0, r"\1'" + REDACTED + "'"),

    # Oracle 23ai: ORA-03301: (ORA-00001 details) row with column values (EMAIL:'...') already exists
    (r'(\(ORA-\d+ details\) ).*?' + ORACLE_LINE_END, re.DOTALL, r'\1' + REDACTED),
    # Oracle 23ai: ORA-01722: unable to convert string value containing 'S' to a number: N
    (r'(unable to convert string value containing ).*?' + ORACLE_LINE_END, re.DOTALL, r'\1' + REDACTED),

    # SQL Server: The duplicate key value is (ann@example.com).
    (r'(The duplicate key value is \().*(\)\.)', 0, r'\1' + REDACTED + r'\2'),
    # SQL Server: String or binary data would be truncated ... Truncated value: 'abc'.
    (r'(Truncated value: ' + QUOTE + r').*(?<!\\)(' + QUOTE + r'\.)', 0, r'\1' + REDACTED + r'\2'),
    # SQL Server: Conversion failed when converting the nvarchar value 'abc' to data type int.
    (r'(converting the \w+ value ' + QUOTE + r').*(?<!\\)(' + QUOTE + r' to data type)', 0, r'\1' + REDACTED + r'\2'),
    # SQL Server: Incorrect syntax near 'ann'.  pymssql writes values into the
    # statement, so the text near an error can be one.
    (r'(Incorrect syntax near ' + QUOTE + r').*(?<!\\)(' + QUOTE + r'\.)', 0, r'\1' + REDACTED + r'\2'),
    # SQL Server: Unclosed quotation mark after the character string 'ann'.
    (r'(Unclosed quotation mark after the character string ' + QUOTE + r').*(?<!\\)(' + QUOTE + r'\.)', 0, r'\1' + REDACTED + r'\2'),
    # SQL Server: The conversion of the varchar value '99999999999' overflowed an int column.
    (r'(conversion of the \w+ value ' + QUOTE + r').*(?<!\\)(' + QUOTE + r' overflowed)', 0, r'\1' + REDACTED + r'\2'),

    # DuckDB: Could not convert string 'ann@example.com' to INT32 -- quoted
    # with either quote, and the value may hold the same one, so the quote
    # that ends it is the last before ` to ` and a type.
    (r'(Could not convert string )([\'"])[^\n]*\2( to [A-Z])', 0, r'\1\2' + REDACTED + r'\2\3'),
    # DuckDB: Duplicate key "email: ann@example.com" violates unique constraint.
    (r'(Duplicate key ")[^\n]*(" violates )', 0, r'\1' + REDACTED + r'\2'),
    # DuckDB: Violates foreign key constraint because key "id: 5" does not
    # exist in the referenced table / is still referenced by a foreign key
    (r'(because key ")[^\n]*(" (?:does not exist|is still referenced))', 0, r'\1' + REDACTED + r'\2'),
    # DuckDB: Could not parse string "..." according to format specifier
    # "%Y-%m-%d", then the string again on a line of its own, and a caret.
    (r'(Could not parse string ")[^\n]*(" according to format specifier "[^"\n]*")(?:\n(?!\^)[^\n]*)?', 0, r'\1' + REDACTED + r'\2'),
    # DuckDB: date field value out of range: "2024-13-45" / invalid timestamp
    # field format: "x", expected format is (...)
    (r'((?:date|time|timestamp) field (?:value out of range|format): ")[^\n]*(")', 0, r'\1' + REDACTED + r'\2'),
    # DuckDB: Type INT64 with value 99999999999 can't be cast ... -- unquoted
    (r'(with value ).*?( can\'t be cast)', 0, r'\1' + REDACTED + r'\2'),
    )]


def scrubText(text: str) -> str:
    """`text` with every value a known driver message quotes replaced by <redacted>.

    Idempotent, so text that passes through more than one reporting step is
    unchanged by the second.
    """

    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)

    return text


# The word before a quote that says the quote names something rather than
# quoting data: `relation "orders"`, `for key 'users.email'`, `in object
# 'dbo.orders'`. Bare `key` isn't one: DuckDB writes `Duplicate key "id: 5"`.
_IDENTIFIER_BEFORE = re.compile(
    r'(?:\b(?:relation|table|column|constraint|index|schema|database|catalog|type|function|procedure|view|sequence|object|role|user|'
    r'trigger|encoding|collation|extension|file|directory|for key|of key|format specifier)|\b(?:table or view)):?\s*$', re.IGNORECASE)

# pymssql's message: the repr of (code, bytes), the whole text in one quote.
_PYMSSQL_MESSAGE = re.compile(r'(\(\d+, b)([\'"])(.*)\2(\)\s*)$', re.DOTALL)


def _quotes(text: str) -> Iterator[Tuple[int, str]]:
    """Each quote in `text`, as (position, character), a backslash before it
    being part of it: pymssql escapes the quotes inside its message.
    """

    for match in re.finditer(r'\\?([\'"])', text):
        yield match.start(), match.group(1)


def _removeQuotedData(text: str) -> str:
    """`text` with each quoted part removed, from the first quote that doesn't
    name a table, column or constraint (nor hold <redacted> already) to the last
    quote on its line -- or to the end of the text, where the quotes left on
    the line don't pair up, as a value holding a quote or a newline leaves them.
    """

    quotes = list(_quotes(text))
    pieces = []
    position = 0
    index = 0
    keptUntil: Optional[int] = None

    while index < len(quotes):
        start, character = quotes[index]
        closing = next((later for later in range(index + 1, len(quotes)) if quotes[later][1] == character), None)
        if closing is None:
            break
        end = quotes[closing][0] + (2 if text[quotes[closing][0]] == '\\' else 1)
        inside = text[start:end].strip('\\\'"')
        follows = keptUntil is not None and start == keptUntil + 1 and text[keptUntil] == '.'

        if inside == REDACTED or follows or _IDENTIFIER_BEFORE.search(text[position:start]):
            keptUntil = end
            index = closing + 1
            continue

        lineEnd = text.find('\n', start)
        lineEnd = len(text) if lineEnd == -1 else lineEnd
        onLine = [quote for quote in quotes[index:] if quote[0] < lineEnd]
        balanced = all(sum(1 for _, each in onLine if each == kind) % 2 == 0 for kind in '\'"')
        # Where they don't, a quote is part of a value, and so may be anything after it.
        stop = onLine[-1][0] + (2 if text[onLine[-1][0]] == '\\' else 1) if balanced else len(text)

        pieces.append(text[position:start])
        pieces.append(REDACTED)
        position = stop
        keptUntil = None
        index = next((later for later in range(index, len(quotes)) if quotes[later][0] >= stop), len(quotes))

    pieces.append(text[position:])

    return ''.join(pieces)


def scrubForeignText(text: str) -> str:
    """scrubText, then every quoted part left that isn't a name removed: for a
    message this package didn't write, whose format may be one no pattern
    knows. Idempotent, as scrubText is.
    """

    text = scrubText(text)

    wrapped = _PYMSSQL_MESSAGE.match(text)
    if wrapped is not None:
        opening, quote, inner, closing = wrapped.groups()
        return '{}{}{}{}{}'.format(opening, quote, _removeQuotedData(inner), quote, closing)

    return _removeQuotedData(text)


def isForeign(error: BaseException) -> bool:
    """Whether an error was raised outside this package -- by a driver, a
    library, or Python itself -- so its message may quote data.
    """

    return not type(error).__module__.startswith('bauta')


def foreignMessages(error: Optional[BaseException]) -> List[str]:
    """The messages of `error` and the errors behind it that came from outside
    this package, longest first, so a message within another is replaced after it.
    """

    messages = []
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isForeign(error) and str(error):
            messages.append(str(error))
        error = error.__cause__ or error.__context__

    return sorted(set(messages), key=len, reverse=True)


def scrubWithin(text: str, error: Optional[BaseException]) -> str:
    """`text` -- a log message or a traceback -- with each foreign message
    behind `error` scrubbed as scrubForeignText does, and the rest by scrubText.
    """

    for message in foreignMessages(error):
        text = text.replace(message, scrubForeignText(message))

    return scrubText(text)


def describeError(error: BaseException) -> str:
    """`TypeName: message`, scrubbed -- how an error is reported anywhere it
    leaves the process. A message from outside the package loses every quoted
    part that doesn't name something, whether or not a pattern knows its format.
    """

    message = str(error)

    return '{}: {}'.format(type(error).__name__, scrubForeignText(message) if isForeign(error) else scrubText(message))

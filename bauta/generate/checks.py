"""What a table's CHECK constraints allow a column to hold, for `synthesize`:
a list of values, or a range of numbers. Generated rows that broke a check
were refused by the database, failing the run on the first one.

Read from the definitions each database's catalog gives, which spell the same
check seven ways -- PostgreSQL turns `status IN ('a', 'b')` into `status =
ANY (ARRAY['a'::text, 'b'::text])`, SQL Server into `[status]='a' OR
[status]='b'`, MySQL quotes its values with a character set. Only the shapes
that name one column are read: a list of values, and comparisons with numbers
joined by AND. Anything else -- `a <= b`, a function -- is returned unread,
for synthesize to say it may refuse rows.
"""
from __future__ import annotations

import decimal
import re
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from ..configuration.sqltext import codeOnly, sqlSpans


class ColumnCheck(NamedTuple):
    """What the checks on one column allow: `choices`, where they name the
    values, or a range between `low` and `high`, either open where None, each
    bound included or not.
    """

    choices: Optional[Tuple[Any, ...]] = None
    low: Optional[decimal.Decimal] = None
    lowIncluded: bool = True
    high: Optional[decimal.Decimal] = None
    highIncluded: bool = True


_NAME = r'(\w+)'
_NUMBER = r'(-?\d+(?:\.\d+)?)'
_LITERAL = r"'((?:[^']|'')*)'|(-?\d+(?:\.\d+)?)"


def _normalized(definition: str) -> str:
    """A definition with what only spells it differently taken out: the
    CHECK keyword, casts, character sets, MySQL's escaped quotes and every
    kind of identifier quote. Upper-cased outside quoted values.
    """

    text = definition.strip()
    text = re.sub(r'^CHECK\s*', '', text, flags=re.IGNORECASE)
    text = text.replace("\\'", "'")

    # Quoted names unquoted, comments dropped, and casts, character sets and
    # SQL Server's [brackets] taken out of the code, which is upper-cased;
    # quoted values are left as they are, quotes inside them included.
    pieces: List[str] = []
    position = 0
    for start, end, kind in sqlSpans(text):
        pieces.append(_normalizedCode(text[position:start]))
        if kind == 'identifier':
            pieces.append(text[start + 1:end - 1].upper())
        elif kind == 'literal':
            pieces.append(text[start:end])
        position = end
    pieces.append(_normalizedCode(text[position:]))
    text = ''.join(pieces)

    # Then a lone name or number unwrapped, which PostgreSQL's casts leave in
    # parentheses -- (kind) = 'x', ("Kind") = 'x' -- found in the code alone
    # and unwrapped where it is, so no value is touched.
    unwrapped: List[str] = []
    position = 0
    for match in re.finditer(r'\(\s*(-?[\w.]+)\s*\)', codeOnly(text)):
        unwrapped.extend((text[position:match.start()], match.group(1)))
        position = match.end()
    unwrapped.append(text[position:])

    return ''.join(unwrapped)


def _normalizedCode(code: str) -> str:

    code = re.sub(r'::[\w ]+(?:\[\])?', '', code)
    code = re.sub(r'\b_(?:utf8mb4|utf8mb3|utf8|latin1|binary|ascii)\b', '', code)
    code = re.sub(r'\[(\w+)\]', r'\1', code)

    return code.upper()



def _unwrapped(text: str) -> str:
    """`text` without parentheses that enclose all of it."""

    text = text.strip()
    while text.startswith('(') and text.endswith(')') and _balanced(text[1:-1]):
        text = text[1:-1].strip()

    return text


def _balanced(text: str) -> bool:

    depth = 0
    for character in codeOnly(text):
        depth += {'(': 1, ')': -1}.get(character, 0)
        if depth < 0:
            return False

    return depth == 0


def _topLevel(text: str, word: str) -> List[str]:
    """`text` split on `word` (AND, OR) where it is outside parentheses and
    quotes, and isn't BETWEEN's AND.
    """

    parts: List[str] = []
    depth = start = 0
    pattern = re.compile(r"\(|\)|\b{}\b|\bBETWEEN\b".format(word))
    betweens = 0
    for match in pattern.finditer(codeOnly(text)):
        token = match.group(0)
        if token == '(':
            depth += 1
        elif token == ')':
            depth -= 1
        elif token == 'BETWEEN' and depth == 0:
            betweens += 1
        elif token == word and depth == 0:
            if word == 'AND' and betweens:
                betweens -= 1
                continue
            parts.append(text[start:match.start()])
            start = match.end()

    parts.append(text[start:])

    return [_unwrapped(part) for part in parts]


def _value(quoted: Optional[str], number: Optional[str]) -> Any:

    if quoted is not None:
        return quoted.replace("''", "'")

    return decimal.Decimal(number) if number is not None and '.' in number else int(number)  # type: ignore[arg-type]


def _choices(text: str) -> Optional[Tuple[str, Tuple[Any, ...]]]:
    """(column, values) for `c IN (...)`, `c = ANY (ARRAY[...])`, or an OR of
    `c = value`; None for anything else.
    """

    match = re.match(r'^' + _NAME + r'\s+IN\s*\((.*)\)$', text) or re.match(r'^' + _NAME + r'\s*=\s*ANY\s*\(\s*\(?\s*ARRAY\s*\[(.*)\]\s*\)?\s*\)$', text)
    if match:
        # finditer, not findall, which gives '' for the group that didn't
        # take part and so read 1 in IN (1, 'x') as the empty string.
        values = tuple(_value(found.group(1), found.group(2)) for found in re.finditer(_LITERAL, match.group(2)))
        return (match.group(1), values) if values else None

    # One value, `status = 'x'`, as well as several joined by OR.
    parts = _topLevel(text, 'OR')
    column = None
    listed: List[Any] = []
    for part in parts:
        each = re.match(r'^' + _NAME + r'\s*=\s*\(?\s*(?:' + _LITERAL + r')\s*\)?$', part)
        if not each or (column is not None and each.group(1) != column):
            return None
        column = each.group(1)
        listed.append(_value(each.group(2), each.group(3)))

    return (column, tuple(listed)) if column else None


_FLIPPED = {'>': '<', '>=': '<=', '<': '>', '<=': '>='}


def _bounds(text: str) -> Optional[Tuple[str, ColumnCheck]]:
    """(column, range) for comparisons of one column with numbers joined by
    AND, BETWEEN included; None for anything else.
    """

    column = None
    check = ColumnCheck()
    for part in _topLevel(text, 'AND'):
        between = re.match(r'^' + _NAME + r'\s+BETWEEN\s+\(?\s*' + _NUMBER + r'\s*\)?\s+AND\s+\(?\s*' + _NUMBER + r'\s*\)?$', part)
        compared = re.match(r'^' + _NAME + r'\s*(>=|<=|>|<)\s*\(?\s*' + _NUMBER + r'\s*\)?$', part)
        reversed_ = re.match(r'^\(?\s*' + _NUMBER + r'\s*\)?\s*(>=|<=|>|<)\s*' + _NAME + r'$', part)
        if between:
            name, low, high = between.group(1), decimal.Decimal(between.group(2)), decimal.Decimal(between.group(3))
            comparisons = [('>=', low), ('<=', high)]
        elif compared:
            name, comparisons = compared.group(1), [(compared.group(2), decimal.Decimal(compared.group(3)))]
        elif reversed_:
            name, comparisons = reversed_.group(3), [(_FLIPPED[reversed_.group(2)], decimal.Decimal(reversed_.group(1)))]
        else:
            return None
        if column is not None and name != column:
            return None
        column = name
        for operator, bound in comparisons:
            if operator.startswith('>') and (check.low is None or bound > check.low):
                check = check._replace(low=bound, lowIncluded=operator == '>=')
            elif operator.startswith('<') and (check.high is None or bound < check.high):
                check = check._replace(high=bound, highIncluded=operator == '<=')

    return (column, check) if column else None


def columnChecks(definitions: Sequence[str], columns: Sequence[str]) -> Tuple[Dict[str, ColumnCheck], List[str]]:
    """What each column's checks allow, by its upper-cased name, and the
    definitions not read: on another column than the table has, or of a
    shape this doesn't follow.
    """

    known = {column.upper() for column in columns}
    checks: Dict[str, ColumnCheck] = {}
    unread: List[str] = []

    for definition in definitions:
        text = _unwrapped(_normalized(definition))
        found = _choices(text)
        if found is not None and found[0] in known:
            held = checks.get(found[0], ColumnCheck())
            # Two lists on one column allow what both do.
            choices = found[1] if held.choices is None else tuple(value for value in held.choices if value in found[1])
            checks[found[0]] = held._replace(choices=choices)
            continue
        ranged = _bounds(text)
        if ranged is not None and ranged[0] in known:
            column, check = ranged
            held = checks.get(column, ColumnCheck())
            low, lowIncluded = (check.low, check.lowIncluded) if held.low is None or (check.low is not None and check.low > held.low) \
                else (held.low, held.lowIncluded)
            high, highIncluded = (check.high, check.highIncluded) if held.high is None or (check.high is not None and check.high < held.high) \
                else (held.high, held.highIncluded)
            checks[column] = held._replace(low=low, lowIncluded=lowIncluded, high=high, highIncluded=highIncluded)
            continue
        unread.append(definition)

    return checks, unread

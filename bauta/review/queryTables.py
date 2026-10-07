"""Which tables a sourceQuery reads rows from, for `coverage`: the tables
after FROM and JOIN, through common table expressions, derived tables and
set operations, and not one named only in a comment, a string or a
subquery that filters.

Not a SQL parser. It reads just enough of the seven dialects' SELECT to tell
a table a query copies from one it mentions, and gives up -- None -- on what
it can't balance, so a caller can fall back to a looser reading.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from ..configuration.sqltext import codeOnly


# A quoted identifier in each style, a word, or one character of anything else.
_TOKEN = re.compile(r'"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]]*\]|[A-Za-z_À-￿][\w$#À-￿]*|\d[\w.]*|\S')

# Where a FROM list ends.
_CLAUSES = frozenset({'WHERE', 'GROUP', 'ORDER', 'HAVING', 'LIMIT', 'OFFSET', 'FETCH', 'WINDOW', 'QUALIFY', 'FOR', 'ON', 'USING',
                      'CONNECT', 'START', 'RETURNING', 'INTO', 'SELECT', 'PIVOT', 'UNPIVOT', 'MATCH_RECOGNIZE', 'TABLESAMPLE'})

# After which a parenthesized query is one of a set operation's operands.
_SET_OPERATIONS = frozenset({'UNION', 'INTERSECT', 'EXCEPT', 'MINUS', 'ALL', 'DISTINCT'})


def _unquoted(token: str) -> str:

    if token[:1] in ('"', '`') and len(token) > 1:
        return token[1:-1].replace(token[0] * 2, token[0])
    if token[:1] == '[':
        return token[1:-1]

    return token


def _isName(token: str) -> bool:

    return bool(token) and (token[0] in '"`[' or token[0].isalpha() or token[0] == '_' or ord(token[0]) >= 0xc0)


class _Reader:

    def __init__(self, tokens: List[str]) -> None:
        self.tokens = tokens
        self.matching = self._matchParentheses()


    def _matchParentheses(self) -> Dict[int, int]:

        matching: Dict[int, int] = {}
        open_: List[int] = []
        for index, token in enumerate(self.tokens):
            if token == '(':
                open_.append(index)
            elif token == ')':
                if not open_:
                    raise ValueError('unbalanced')
                matching[open_.pop()] = index
        if open_:
            raise ValueError('unbalanced')

        return matching


    def _word(self, index: int) -> str:

        return self.tokens[index].upper() if index < len(self.tokens) else ''


    def _startsQuery(self, index: int) -> bool:

        return self._word(index) in ('SELECT', 'WITH', '(')


    def _name(self, index: int) -> Tuple[str, int]:
        """A possibly qualified name starting at `index`: its last part,
        unquoted, and the index after it.
        """

        last = _unquoted(self.tokens[index])
        index += 1
        while index + 1 < len(self.tokens) and self.tokens[index] == '.' and _isName(self.tokens[index + 1]):
            last = _unquoted(self.tokens[index + 1])
            index += 2

        return last, index


    def read(self, start: int, end: int, expressions: Dict[str, Tuple[int, int]], expanding: Tuple[str, ...], fromList: bool = False) -> Set[str]:
        """The tables `tokens[start:end]`, a balanced span, reads rows from,
        upper-cased and unqualified. `fromList` reads it as the inside of a
        parenthesized join, `(users u JOIN roles r ON ...)`, which begins
        with a table rather than a query.
        """

        expressions = dict(expressions)
        index = self._readWith(start, end, expressions)
        scan = _Scan(inFrom=fromList, expecting=fromList)

        while index < end:
            token = self.tokens[index]
            word = token.upper()

            if token == '(':
                index = self._readParenthesized(index, start, scan, expressions, expanding)
                continue
            if scan.expecting and _isName(token) and word not in ('LATERAL', 'ONLY'):
                index = self._readNamed(index, scan, expressions, expanding)
                continue

            if word == 'FROM' or word == 'JOIN':
                scan.inFrom = scan.expecting = True
            elif token == ',' and scan.inFrom:
                scan.expecting = True
            elif word in _CLAUSES or word in _SET_OPERATIONS:
                scan.inFrom = scan.expecting = False
            scan.previous = word
            index += 1

        return scan.tables


    def _readWith(self, index: int, end: int, expressions: Dict[str, Tuple[int, int]]) -> int:
        """Records the common table expressions a WITH clause at `index`
        defines, name to body span, in `expressions`; the index after the
        clause, or `index` where there is none.
        """

        if self._word(index) != 'WITH':
            return index
        index += 1
        if self._word(index) == 'RECURSIVE':
            index += 1

        while index < end and _isName(self.tokens[index]):
            name, index = self._name(index)
            if self.tokens[index:index + 1] == ['(']:
                index = self.matching[index] + 1
            if self._word(index) != 'AS':
                break
            index += 1
            while self._word(index) in ('NOT', 'MATERIALIZED'):
                index += 1
            if self.tokens[index:index + 1] != ['(']:
                break
            closing = self.matching[index]
            expressions[name.upper()] = (index + 1, closing)
            index = closing + 1
            if self.tokens[index:index + 1] != [',']:
                break
            index += 1

        return index


    def _readParenthesized(self, index: int, start: int, scan: '_Scan', expressions: Dict[str, Tuple[int, int]], expanding: Tuple[str, ...]) -> int:
        """A parenthesized span at `index`: a query where a table or a set
        operation's operand may stand, a parenthesized join where only a table
        may, and nothing otherwise -- a subquery that filters, a scalar one,
        a list of values. The index after it.
        """

        closing = self.matching[index]
        if (scan.expecting or scan.previous in _SET_OPERATIONS or index == start) and self._startsQuery(index + 1):
            scan.tables |= self.read(index + 1, closing, expressions, expanding)
        elif scan.expecting:
            scan.tables |= self.read(index + 1, closing, expressions, expanding, fromList=True)
        scan.expecting = False
        scan.previous = ')'

        return closing + 1


    def _readNamed(self, index: int, scan: '_Scan', expressions: Dict[str, Tuple[int, int]], expanding: Tuple[str, ...]) -> int:
        """A name where a table may stand: a table, a common table expression
        -- read for what it reads, once on any path -- or a table function,
        generate_series(...), which is none. The index after it.
        """

        name, after = self._name(index)
        scan.expecting = False
        if self.tokens[after:after + 1] == ['(']:
            scan.previous = ')'
            return self.matching[after] + 1

        folded = name.upper()
        if folded in expressions and folded not in expanding:
            expressionStart, expressionEnd = expressions[folded]
            scan.tables |= self.read(expressionStart, expressionEnd, expressions, expanding + (folded,))
        elif folded not in expressions:
            scan.tables.add(folded)
        scan.previous = folded

        return after


class _Scan:
    """Where a scan of one span is: in a FROM list or not, expecting a table
    next or not, the word before, and the tables found so far.
    """

    def __init__(self, inFrom: bool, expecting: bool) -> None:
        self.inFrom = inFrom
        self.expecting = expecting
        self.previous = ''
        self.tables: Set[str] = set()


def tablesRead(query: str) -> Optional[Set[str]]:
    """The tables `query` reads rows from, each upper-cased and without its
    schema or quotes, or None where its parentheses don't balance.
    """

    # Comments and literals blanked first, so a table named in one is not read.
    tokens = _TOKEN.findall(codeOnly(query))
    try:
        reader = _Reader(tokens)
    except ValueError:
        return None

    return reader.read(0, len(tokens), {}, ())

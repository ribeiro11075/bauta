"""Telling the code of a SQL text from its comments, string literals and
quoted names, for everything that reads SQL a user or a catalog wrote: the
{{ watermark }} placeholder, the tables `coverage` finds a query reads, and
the CHECK constraints `synthesize` follows.

Not a parser: one pass that finds where each comment, literal and quoted
name begins and ends, so that a `--`, a quote, a parenthesis or a
placeholder inside one isn't read as code. A quoted name is found even
where it is kept as code, since `"O'Brien"` holds a quote that would
otherwise open a literal.
"""
from __future__ import annotations

import re
from typing import List, Tuple

# A comment (line or block), a string literal ('' escaping a quote, or
# PostgreSQL's dollar quotes, tag and all), or a name quoted with double
# quotes or backticks. Not [brackets], which PostgreSQL uses for arrays.
_SPANS = re.compile(r"(?P<comment>--[^\n]*|/\*.*?\*/)|(?P<literal>'(?:[^']|'')*'|\$(?P<tag>[A-Za-z_]*)\$.*?\$(?P=tag)\$)|"
                    r'(?P<identifier>"(?:[^"]|"")*"|`(?:[^`]|``)*`)', re.DOTALL)


def sqlSpans(text: str) -> List[Tuple[int, int, str]]:
    """(start, end, kind) of each comment, literal and quoted name in
    `text`, in order: `comment`, `literal` or `identifier`.
    """

    spans = []
    for match in _SPANS.finditer(text):
        kind = 'comment' if match.group('comment') is not None else 'literal' if match.group('literal') is not None else 'identifier'
        spans.append((match.start(), match.end(), kind))

    return spans


def codeOnly(text: str, identifiers: bool = False) -> str:
    """`text` with each comment and literal -- and each quoted name too,
    where `identifiers` says so -- blanked to spaces of its own length, so a
    position in one is the same position in the other.
    """

    blanked = list(text)
    for start, end, kind in sqlSpans(text):
        if kind != 'identifier' or identifiers:
            blanked[start:end] = ' ' * (end - start)

    return ''.join(blanked)

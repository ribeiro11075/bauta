"""Telling a SQL text's code from its comments, literals and quoted names."""
from bauta.configuration.sqltext import codeOnly, sqlSpans


def test_comments_and_literals_are_blanked_at_their_own_length():
    text = "SELECT 'a -- b' AS x -- trailing {{ watermark }}\nFROM t /* c */ WHERE y = $q$ it's $q$"

    blanked = codeOnly(text)

    assert len(blanked) == len(text)
    assert 'watermark' not in blanked and 'trailing' not in blanked and "it's" not in blanked and '-- b' not in blanked
    assert blanked.split() == ['SELECT', 'AS', 'x', 'FROM', 't', 'WHERE', 'y', '=']


def test_a_quote_inside_a_quoted_name_opens_no_literal():
    """`"O'Brien"` read as the start of a literal hid the rest of the query."""
    text = 'SELECT * FROM "O\'Brien" JOIN users u ON true'

    assert codeOnly(text) == text
    assert codeOnly(text, identifiers=True) == 'SELECT * FROM           JOIN users u ON true'
    assert [kind for _, _, kind in sqlSpans(text)] == ['identifier']


def test_dollar_quotes_end_only_at_their_own_tag():
    text = "SELECT $a$ x $b$ y $a$, 1"

    assert codeOnly(text).strip().split() == ['SELECT', ',', '1']

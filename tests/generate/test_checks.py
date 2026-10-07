"""What CHECK constraints allow a column, read from each database's own
spelling of them -- recorded from the seven, as their catalogs return them.
"""
import decimal

import pytest

from bauta.generate.checks import columnChecks

COLUMNS = ['id', 'status', 'amount', 'pct', 'a', 'b', 'kind']

DEFINITIONS = {
    'postgresql': ['CHECK (((amount >= (0)::numeric) AND (amount < (1000)::numeric)))', 'CHECK ((a <= b))',
                   "CHECK ((((kind)::text = 'x'::text) OR ((kind)::text = 'y'::text)))", 'CHECK (((pct >= 1) AND (pct <= 100)))',
                   "CHECK (((status)::text = ANY ((ARRAY['open'::character varying, 'closed'::character varying, 'held'::character varying])::text[])))"],
    'mysql': ["((`kind` = _utf8mb4\\'x\\') or (`kind` = _utf8mb4\\'y\\'))", '(`a` <= `b`)', '(`pct` between 1 and 100)',
              '((`amount` >= 0) and (`amount` < 1000))', "(`status` in (_utf8mb4\\'open\\',_utf8mb4\\'closed\\',_utf8mb4\\'held\\'))"],
    'mariadb': ["`status` in ('open','closed','held')", '`amount` >= 0 and `amount` < 1000', '`pct` between 1 and 100',
                "`kind` = 'x' or `kind` = 'y'", '`a` <= `b`'],
    'mssql': ["([status]='held' OR [status]='closed' OR [status]='open')", '([amount]>=(0) AND [amount]<(1000))', '([pct]>=(1) AND [pct]<=(100))',
              '([a]<=[b])', "([kind]='x' OR [kind]='y')"],
    'oracle': ["status IN ('open','closed','held')", 'amount >= 0 AND amount < 1000', 'pct BETWEEN 1 AND 100', 'a <= b', "kind = 'x' OR kind = 'y'"],
    'duckdb': ["(status IN ('open', 'closed', 'held'))", '((amount >= 0) AND (amount < 1000))', '(pct BETWEEN 1 AND 100)', '(a <= b)',
               "((kind = 'x') OR (kind = 'y'))"],
    'sqlite': ["(status IN ('open','closed','held'))", '(amount >= 0 AND amount < 1000)', '(pct BETWEEN 1 AND 100)', '(a <= b)',
               "(kind = 'x' OR kind = 'y')"],
    }


@pytest.mark.parametrize('database', sorted(DEFINITIONS))
def test_each_databases_spelling_of_a_check_reads_the_same(database):
    checks, unread = columnChecks(DEFINITIONS[database], COLUMNS)

    assert set(checks['STATUS'].choices) == {'open', 'closed', 'held'}
    assert set(checks['KIND'].choices) == {'x', 'y'}
    assert (checks['AMOUNT'].low, checks['AMOUNT'].lowIncluded, checks['AMOUNT'].high, checks['AMOUNT'].highIncluded) == (0, True, 1000, False)
    assert (checks['PCT'].low, checks['PCT'].high, checks['PCT'].highIncluded) == (1, 100, True)
    assert len(unread) == 1 and 'b' in unread[0].lower()


def test_what_names_two_columns_or_is_no_known_shape_is_returned_unread():
    checks, unread = columnChecks(["CHECK (note <> 'O''Brien (x)')", 'CHECK (length(code) = 4)', "CHECK (other IN ('a'))"], ['note', 'code'])

    assert checks == {} and len(unread) == 3


def test_bounds_from_several_checks_on_one_column_narrow_each_other():
    checks, _ = columnChecks(['(price > 0)', '(price <= 99.5)', '(5 < price)'], ['price'])

    assert (checks['PRICE'].low, checks['PRICE'].lowIncluded, checks['PRICE'].high) == (5, False, decimal.Decimal('99.5'))


def test_a_list_mixing_numbers_and_text_and_a_single_value_are_read():
    """findall gave '' for the group a number didn't fill, so IN (1, 'x')
    read 1 as the empty string.
    """
    checks, unread = columnChecks(["status IN (1, 2.5, 'x', '')", "kind = 'only'", 'level = 3'], ['status', 'kind', 'level'])

    assert checks['STATUS'].choices == (1, decimal.Decimal('2.5'), 'x', '')
    assert (checks['KIND'].choices, checks['LEVEL'].choices, unread) == (('only',), (3,), [])


def test_two_lists_on_one_column_allow_what_both_do():
    checks, _ = columnChecks(["status IN ('a', 'b', 'c')", "status IN ('b', 'c', 'd')"], ['status'])

    assert checks['STATUS'].choices == ('b', 'c')

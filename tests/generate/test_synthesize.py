"""Synthetic rows, checked against a real SQLite schema: keys unique, foreign
keys resolvable, values within their columns, and the same seed repeatable.
"""
import datetime
import decimal
import re
import sqlite3

import pytest

from bauta.configuration import connectionConfig
from bauta.database import Database
from bauta.generate import synthesize
from bauta.generate.synthesize import SynthesisError, planTable, synthesizeTable

SCHEMA = '''
CREATE TABLE customers (id INTEGER PRIMARY KEY, email VARCHAR(40) NOT NULL, first_name VARCHAR(8), phone VARCHAR(20),
                        birth_date DATE, balance DECIMAL(8,2), active BOOLEAN, notes TEXT, zip_code INTEGER, code CHAR(3),
                        created_at TIMESTAMP, token BLOB);
CREATE TABLE products (sku VARCHAR(10) PRIMARY KEY, name VARCHAR(30), price NUMERIC(8,2) NOT NULL);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INT NOT NULL REFERENCES customers(id), total REAL);
CREATE TABLE order_items (order_id INT REFERENCES orders(id), line INT, sku VARCHAR(10) NOT NULL REFERENCES products(sku),
                          PRIMARY KEY (order_id, line));
CREATE TABLE tags (order_id INT REFERENCES orders(id), sku VARCHAR(10) REFERENCES products(sku), PRIMARY KEY (order_id, sku));
CREATE TABLE employees (id INTEGER PRIMARY KEY, manager_id INT REFERENCES employees(id), full_name VARCHAR(40));
CREATE TABLE bosses (id INTEGER PRIMARY KEY, boss_id INT NOT NULL REFERENCES bosses(id));
CREATE TABLE labels (id INTEGER PRIMARY KEY, code VARCHAR(20) NOT NULL UNIQUE, label VARCHAR(40));
CREATE TABLE initials (code VARCHAR(1) PRIMARY KEY);
CREATE TABLE days (day DATE PRIMARY KEY, note TEXT);
'''


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'synthetic.db'
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.close()

    with Database(connectionConfig(type='sqlite', path=str(path))) as opened:
        yield opened


def _fill(database, *tables, seed=0):
    return [synthesizeTable(database, table, rows, seed=seed) for table, rows in tables]


def test_a_schema_fills_parents_first_with_every_foreign_key_resolvable(database):
    counts = _fill(database, ('customers', 50), ('products', 5), ('orders', 200), ('order_items', 300), ('employees', 10))

    assert counts == [50, 5, 200, 300, 10]
    database.cursor.execute('PRAGMA foreign_key_check')
    assert database.cursor.fetchall() == []
    assert database.query('SELECT count(*) FROM orders WHERE customer_id NOT IN (SELECT id FROM customers)') == [(0,)]


def test_values_fit_their_columns(database):
    _fill(database, ('customers', 200))

    rows = database.query('SELECT id, email, first_name, phone, birth_date, balance, active, zip_code, code, created_at, token FROM customers')

    assert [row[0] for row in rows] == list(range(1, 201))
    assert len({row[1] for row in rows}) == 200
    assert all(re.fullmatch(r'u[0-9a-f]+@example\.test', row[1]) and len(row[1]) <= 40 for row in rows)
    assert all(row[2] is None or len(row[2]) <= 8 for row in rows)
    assert all(row[3] is None or re.fullmatch(r'\+\d \d{3} \d{3} \d{4}', row[3]) for row in rows)
    assert all(row[4] is None or datetime.date(1940, 1, 1) <= datetime.date.fromisoformat(row[4]) <= datetime.date(2005, 1, 1) for row in rows)
    assert all(row[5] is None or abs(decimal.Decimal(str(row[5]))) < 10 ** 6 for row in rows)
    assert {row[6] for row in rows} <= {0, 1, None}
    assert all(row[8] is None or re.fullmatch('[A-Z]{3}', row[8]) for row in rows)
    assert any(row[9] is None for row in rows) and any(row[9] is not None for row in rows)
    assert all(row[10] is None or len(row[10]) == 16 for row in rows)


def test_a_second_run_continues_after_the_existing_keys(database):
    _fill(database, ('customers', 5), ('products', 3))
    _fill(database, ('customers', 5), ('products', 3))

    assert [row[0] for row in database.query('SELECT id FROM customers ORDER BY id')] == list(range(1, 11))
    assert sorted(row[0] for row in database.query('SELECT sku FROM products')) == ['S1', 'S2', 'S3', 'S4', 'S5', 'S6']


def test_a_unique_text_column_gets_a_different_value_in_every_row(database):
    """Words cut to twenty characters repeat within a few hundred rows, which
    a UNIQUE constraint refuses; each value ends in its row's number.
    """
    assert _fill(database, ('labels', 200)) == [200]

    codes = [row[0] for row in database.query('SELECT code FROM labels')]
    assert len(set(codes)) == 200
    assert all(len(code) <= 20 for code in codes)


def test_a_second_run_does_not_repeat_the_first_runs_values(database):
    """Every generator is indexed by the row's number, so a second run that
    started at row 0 again would generate the first run's values.
    """
    _fill(database, ('labels', 20), ('customers', 20))
    _fill(database, ('labels', 20), ('customers', 20))

    assert database.query('SELECT count(DISTINCT code) FROM labels') == [(40,)]
    assert database.query('SELECT count(DISTINCT email) FROM customers') == [(40,)]


def test_a_unique_column_with_too_few_distinct_values_is_refused_clearly(database):
    """A raw driver error tells no one what happened, and each chunk has
    already committed by then.
    """
    database.alter('CREATE TABLE codes (id INTEGER PRIMARY KEY, code CHAR(1) NOT NULL UNIQUE)')

    with pytest.raises(SynthesisError, match='codes refused a generated row after'):
        synthesizeTable(database, 'codes', 400)


def test_the_same_seed_makes_the_same_rows(tmp_path):
    def build(name, seed):
        path = tmp_path / name
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA)
        connection.close()
        with Database(connectionConfig(type='sqlite', path=str(path))) as opened:
            _fill(opened, ('customers', 20), ('orders', 30), seed=seed)
            return opened.query('SELECT * FROM customers'), opened.query('SELECT * FROM orders')

    assert build('a.db', 7) == build('b.db', 7)
    assert build('c.db', 7) != build('d.db', 8)


def test_a_table_keyed_only_by_foreign_keys_gets_no_more_rows_than_its_parents_allow(database):
    _fill(database, ('customers', 3), ('products', 2), ('orders', 3))

    assert synthesizeTable(database, 'tags', 100) == 6
    assert database.query('SELECT count(DISTINCT order_id || sku) FROM tags') == [(6,)]


def test_rows_are_inserted_a_chunk_at_a_time_with_the_last_chunk_partial(database, monkeypatch):
    inserts = []
    insert = database.insert
    monkeypatch.setattr(database, 'insert', lambda **arguments: inserts.append(len(arguments['data'])) or insert(**arguments))

    assert synthesizeTable(database, 'customers', 25, chunkSize=10) == 25
    assert inserts == [10, 10, 5]
    assert [row[0] for row in database.query('SELECT id FROM customers ORDER BY id')] == list(range(1, 26))


def test_a_key_made_of_foreign_keys_never_repeats_across_chunks(database):
    """Repeated combinations are skipped against every chunk so far, not only
    the one being built: a repeat in a later chunk would break the key."""
    _fill(database, ('customers', 3), ('products', 5), ('orders', 8))

    assert synthesizeTable(database, 'tags', 40, chunkSize=3) == 40
    assert database.query('SELECT count(DISTINCT order_id || sku) FROM tags') == [(40,)]


def test_only_a_key_made_of_foreign_keys_is_remembered(database, monkeypatch):
    """Any other key has a part generated unique, so remembering every one
    only held a key per row in memory -- 116 MiB a million rows -- to find
    no repeat. A key mixing a foreign key and a generated part stays unique."""
    remembered = {}
    chunks = synthesize._chunks

    def watched(makeRow, rows, available, keyIndexes, seen, chunkSize):
        yield from chunks(makeRow, rows, available, keyIndexes, seen, chunkSize)
        remembered[len(remembered)] = len(seen)

    monkeypatch.setattr(synthesize, '_chunks', watched)
    _fill(database, ('customers', 3), ('products', 5), ('orders', 8), ('order_items', 30), ('tags', 10))

    assert list(remembered.values()) == [0, 0, 0, 0, 10]
    assert database.query('SELECT count(DISTINCT order_id || \'-\' || line) FROM order_items') == [(30,)]


def test_a_nullable_self_reference_is_left_null(database):
    _fill(database, ('employees', 5))

    assert database.query('SELECT count(*) FROM employees WHERE manager_id IS NULL') == [(5,)]


@pytest.mark.parametrize('table,message', [
    ('orders', 'references customers, which has no rows; fill customers first'),
    ('bosses', 'references itself through NOT NULL'),
    ('nowhere', 'table nowhere was not found'),
    ('initials', 'initials.code holds only 1 characters, too few for 5 unique keys'),
    ('days', 'days.day is a date primary key, which synthesize can.t make unique'),
    ])
def test_what_cannot_be_filled_is_refused(database, table, message):
    with pytest.raises(SynthesisError, match=message):
        synthesizeTable(database, table, 5)


def test_the_plan_says_what_each_column_gets(database):
    _fill(database, ('customers', 1))

    _, makeRow, plans, available = planTable(database, 'orders', 10)
    described = {plan.column: (plan.source, plan.description) for plan in plans}

    assert available == 10
    assert described['id'] == ('primary key', 'sequential, from 1')
    assert described['customer_id'] == ('foreign key', 'an existing customers key')
    assert described['total'] == ('type', 'a number, sometimes NULL')
    assert makeRow(0)[1] == 1


def test_fixed_width_text_keys_stay_distinct_across_runs(database):
    """Padding after the number made row 0, 9 and 99 all S1000."""
    from bauta.generate.synthesize import _textKeys

    keys = _textKeys(0, 5, fixed=True)
    assert [keys(row) for row in (0, 9, 99, 999)] == ['S0001', 'S0010', 'S0100', 'S1000']

    database.alter('CREATE TABLE codes (code CHAR(5) PRIMARY KEY, "rank" INTEGER)')
    assert synthesizeTable(database, 'codes', 150) == 150
    assert synthesizeTable(database, 'codes', 150) == 150
    assert database.query('SELECT count(DISTINCT code), min(code), max(code) FROM codes') == [(300, 'S0001', 'S0300')]


def test_an_integer_key_named_with_a_reserved_word_continues(database):
    database.alter('CREATE TABLE ranks ("order" INTEGER PRIMARY KEY, label TEXT)')

    _fill(database, ('ranks', 3), ('ranks', 3))

    assert [row[0] for row in database.query('SELECT "order" FROM ranks ORDER BY 1')] == list(range(1, 7))


def test_your_own_rules_choose_realistic_values_too(tmp_path):
    from bauta.configuration import Configuration
    from bauta.generate.discovery import discoveryRules

    path = tmp_path / 'clientes.db'
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE clientes (id INTEGER PRIMARY KEY, nome VARCHAR(40), phone VARCHAR(20))')
    connection.close()
    rules = discoveryRules(Configuration.validateDiscoveryRules({'names': [{'words': ['nome'], 'policy': 'fakeName'},
                                                                           {'words': ['phone'], 'policy': 'keep'}]}))

    with Database(connectionConfig(type='sqlite', path=str(path))) as opened:
        _, _, builtIn, _ = planTable(opened, 'clientes', 5)
        _, _, yours, _ = planTable(opened, 'clientes', 5, rules=rules)

    assert {plan.column: plan.source for plan in builtIn} == {'id': 'primary key', 'nome': 'type', 'phone': 'name'}
    assert {plan.column: plan.source for plan in yours} == {'id': 'primary key', 'nome': 'name', 'phone': 'type'}


def test_generated_dates_reach_the_end_of_the_current_year(monkeypatch):
    """The span ended at 2026-12-31, fixed in the code, so from 2027 no
    generated date was recent.
    """
    import datetime as real

    from bauta.generate import synthesize

    class _Date(real.date):
        @classmethod
        def today(cls):
            return cls(2031, 6, 1)

    monkeypatch.setattr(synthesize.datetime, 'date', _Date)

    assert synthesize._EPOCH + real.timedelta(days=synthesize._recentDays()) == real.date(2031, 12, 31)


def test_synthesized_rows_keep_to_the_tables_check_constraints(tmp_path, caplog):
    """CHECK constraints were never read, so a status column got words and an
    amount any number, and the database refused the first row breaking one.
    """
    import logging
    import sqlite3

    from bauta.configuration import connectionConfig
    from bauta.database import Database
    from bauta.generate.synthesize import synthesizeTable

    path = tmp_path / 'checked.db'
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT NOT NULL CHECK (status IN ('open', 'closed')), "
                           "amount DECIMAL(10,2) CHECK (amount > 0 AND amount < 50), pct INT CHECK (pct BETWEEN 1 AND 5), "
                           "code TEXT CHECK (length(code) < 500))")
    connection.close()

    with Database(connectionSettings=connectionConfig(type='sqlite', path=str(path))) as database, caplog.at_level(logging.WARNING, logger='bauta'):
        assert synthesizeTable(database, 'orders', 400, nullShare=0.0) == 400
        statuses, low, high, pcts = database.query('SELECT count(DISTINCT status), min(amount), max(amount), count(DISTINCT pct) FROM orders')[0]

    assert (statuses, pcts) == (2, 5) and 0 < low and high < 50
    assert 'CHECK (length(code) < 500) is not one synthesize can follow' in caplog.text


def test_a_row_refused_by_a_check_it_could_not_follow_says_so(tmp_path):
    import sqlite3

    from bauta.configuration import connectionConfig
    from bauta.database import Database
    from bauta.generate.synthesize import SynthesisError, synthesizeTable

    path = tmp_path / 'checked.db'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE spans (id INTEGER PRIMARY KEY, a INT NOT NULL, b INT NOT NULL, CHECK (a <= b))')
    connection.close()

    with Database(connectionSettings=connectionConfig(type='sqlite', path=str(path))) as database:
        with pytest.raises(SynthesisError, match='synthesize said it could not follow'):
            synthesizeTable(database, 'spans', 200)


def test_a_profile_shapes_rows_like_the_source_without_copying_anyone(tmp_path):
    """--profile reads aggregates only: a label rarer than five rows, a column
    named like personal data and a key are never read.
    """
    import sqlite3

    from bauta.configuration import connectionConfig
    from bauta.database import Database
    from bauta.generate.synthesize import profileTable

    path = tmp_path / 'source.db'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT, total INT, email TEXT, note TEXT, reference TEXT)')
        statuses = ['open'] * 60 + ['closed'] * 36 + ['VIP ANN SMITH'] * 2 + [None] * 2
        connection.executemany('INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)',
                               [(index, status, 100 + index, 'p{}@corp.example'.format(index), 'note {}'.format(index), 'R-{}'.format(index))
                                for index, status in enumerate(statuses)])
    connection.close()

    with Database(connectionSettings=connectionConfig(type='sqlite', path=str(path))) as database:
        profile = profileTable(database, 'orders')

    assert profile['STATUS'].labels == (('open', 60), ('closed', 36)) and profile['STATUS'].nullShare == pytest.approx(0.02)
    assert (profile['TOTAL'].low, profile['TOTAL'].high) == (100, 199)
    # Too many values to be labels: none of them read.
    assert profile['REFERENCE'].labels == ()
    assert 'EMAIL' not in profile and 'NOTE' not in profile and 'ID' not in profile

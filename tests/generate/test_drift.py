"""discover --update: finding the columns a masked job's policy no longer
matches, and editing its file without disturbing anything else in it.
"""
import sqlite3

import pytest
import yaml

from bauta.cli import EXIT_BAD_CONFIGURATION, EXIT_JOBS_DID_NOT_SUCCEED, EXIT_SUCCESS, main
from bauta.configuration import Configuration, DataJobsFile, connectionConfig
from bauta.database import Database
from bauta.generate.discovery import Suggestion
from bauta.generate.drift import PROPOSED, DriftError, JobDrift, applyDrift, findDrift

JOBS = """workers: 1
# A comment above the jobs, kept.
jobs:
  maskCustomers:
    sourceQuery: SELECT * FROM customers
    targetTableFinal: customers_copy
    masking:
      key: ${MASKING_KEY}
      columns:
        id: keep  # the key
        'email': email
        notes:
          strategy: redact
          replace: '[x]'
        phone: digits
  maskOrders:
    sourceQuery: SELECT id FROM orders
    masking:
      columns:
        id: keep
"""


def added(*columns):
    return [Suggestion(column, {'strategy': 'hash'}, 'a reason') for column in columns]


def test_new_columns_are_appended_with_their_reasons_and_everything_else_is_kept():
    edited = applyDrift(JOBS, JobDrift(job='maskCustomers', added=added('ssn'), removed=[]))

    assert edited.replace("        ssn: {strategy: hash}  # a reason; " + PROPOSED + '\n', '') == JOBS
    assert edited.index('ssn:') < edited.index('maskOrders:')


def test_a_stale_column_is_removed_with_the_lines_its_policy_continues_onto():
    edited = applyDrift(JOBS, JobDrift(job='maskCustomers', added=[], removed=['NOTES', 'email']))

    columns = yaml.safe_load(edited)['jobs']['maskCustomers']['masking']['columns']
    assert list(columns) == ['id', 'phone']
    assert "replace: '[x]'" not in edited and '# the key' in edited and '# A comment above the jobs, kept.' in edited


def test_only_the_named_job_is_edited_even_when_another_has_the_same_column():
    edited = applyDrift(JOBS, JobDrift(job='maskOrders', added=added('customer_id'), removed=[]))

    document = yaml.safe_load(edited)
    assert list(document['jobs']['maskOrders']['masking']['columns']) == ['id', 'customer_id']
    assert 'customer_id' not in document['jobs']['maskCustomers']['masking']['columns']


def test_a_policy_in_flow_style_is_refused_rather_than_rewritten():
    text = JOBS.replace('      columns:\n        id: keep\n', '      columns: {id: keep}\n')

    with pytest.raises(DriftError, match='flow style'):
        applyDrift(text, JobDrift(job='maskOrders', added=added('customer_id'), removed=[]))


def test_a_job_the_file_does_not_hold_is_refused():
    with pytest.raises(DriftError, match='maskNothing'):
        applyDrift(JOBS, JobDrift(job='maskNothing', added=added('x'), removed=[]))


@pytest.fixture
def source(tmp_path):
    connection = sqlite3.connect(str(tmp_path / 'prod.db'))
    connection.executescript('''
        CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT, notes TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INT REFERENCES customers(id), placed_at TEXT);
        INSERT INTO customers VALUES (1, 'a@corp.com', 'hello');
        INSERT INTO orders VALUES (1, 1, '2026-01-01');
        ''')
    connection.commit()
    connection.close()

    with Database(connectionConfig(type='sqlite', path=str(tmp_path / 'prod.db'))) as database:
        yield database


def job(query, columns, **masking):
    raw = {'workers': 1, 'jobs': {'j': {'sourceConnection': 'prod', 'targetConnection': 'copy', 'insertStrategy': 'upsert',
                                        'sourceQuery': query, 'targetTableFinal': 't',
                                        'masking': {'key': 'k' * 32, 'columns': columns, **masking}}}}
    return Configuration.validateJobConfiguration(raw, DataJobsFile).jobs['j']


def test_drift_finds_new_and_stale_columns_and_proposes_as_discover_would(source):
    drift = findDrift(source, 'j', job('SELECT * FROM orders', {'id': 'keep', 'total': 'keep'}), foreignKeys=source.getForeignKeys())

    assert drift.removed == ['total']
    # From the table, so the foreign key gives the new column the domain it shares.
    assert [(suggestion.column, suggestion.policy) for suggestion in drift.added] == [
        ('customer_id', {'strategy': 'keep'}), ('placed_at', {'strategy': 'keep'})]
    assert 'numeric key (domain customers)' in drift.added[0].reason


def test_drift_classifies_a_new_column_of_any_query_from_its_values(source):
    drift = findDrift(source, 'j', job('SELECT c.id, c.email AS contact FROM customers c', {'id': 'keep'}))

    assert [(suggestion.column, suggestion.policy['strategy']) for suggestion in drift.added] == [('contact', 'email')]


def test_a_default_strategy_already_covers_every_new_column(source):
    drift = findDrift(source, 'j', job('SELECT * FROM customers', {'id': 'keep'}, defaultStrategy='null'))

    assert not drift.any()


@pytest.fixture
def workspace(tmp_path, monkeypatch, source):
    configuration = tmp_path / 'configuration'
    (configuration / 'jobs.d').mkdir(parents=True)
    (configuration / 'connections.yaml').write_text('prod:\n  type: sqlite\n  path: ../prod.db\ncopy:\n  type: sqlite\n  path: ../copy.db\n')
    (configuration / 'jobs.yaml').write_text('workers: 1\ndefaults:\n  sourceConnection: prod\n  targetConnection: copy\n'
                                             '  insertStrategy: upsert\n  masking:\n    key: ${MASKING_KEY}\ninclude: [jobs.d/*.yaml]\n')
    (configuration / 'jobs.d' / 'crm.yaml').write_text('jobs:\n  maskCustomers:\n    sourceQuery: SELECT * FROM customers\n'
                                                       '    targetTableFinal: customers\n    masking:\n      columns:\n'
                                                       '        id: keep\n        email: email\n')
    monkeypatch.setenv('MASKING_KEY', 'a-masking-key-for-the-tests-only')
    monkeypatch.chdir(tmp_path)
    return configuration


def test_update_reports_drift_and_exits_1_then_apply_edits_the_file_the_job_is_in(workspace, capsys):
    assert main(['discover', '--update', '--quiet']) == EXIT_JOBS_DID_NOT_SUCCEED
    report = capsys.readouterr().out
    assert 'maskCustomers (configuration/jobs.d/crm.yaml):' in report
    assert "+ notes: {strategy: 'null'}" in report
    assert 'hello' not in report

    before = (workspace / 'jobs.yaml').read_text()
    assert main(['discover', '--update', '--apply', '--quiet']) == EXIT_SUCCESS
    assert (workspace / 'jobs.yaml').read_text() == before
    assert "notes: {strategy: 'null'}" in (workspace / 'jobs.d' / 'crm.yaml').read_text()

    assert main(['discover', '--update', '--quiet']) == EXIT_SUCCESS
    assert main(['validate', '--quiet']) == EXIT_SUCCESS


@pytest.mark.parametrize('flags', [['--connection', 'prod'], ['--table', 'customers'], ['--all-tables'], ['--target', 'copy']])
def test_update_reads_the_jobs_file_rather_than_naming_tables(workspace, flags):
    assert main(['discover', '--update', '--quiet'] + flags) == EXIT_BAD_CONFIGURATION


def test_apply_without_update_is_a_usage_error(workspace):
    assert main(['discover', '--apply', '--connection', 'prod', '--table', 'customers', '--quiet']) == EXIT_BAD_CONFIGURATION


@pytest.mark.parametrize('query, table', [
    ('SELECT * FROM orders', 'orders'),
    ('select * from app.orders;', 'app.orders'),
    # As discover writes a table of another schema.
    ('SELECT * FROM "app"."orders"', '"app"."orders"'),
    ('SELECT * FROM [dbo].[orders]', '[dbo].[orders]'),
    ('SELECT * FROM `shop`.orders', '`shop`.orders'),
    ('SELECT * FROM orders WHERE id > 1', None),
    ('SELECT id FROM orders', None),
    ])
def test_a_whole_table_query_is_recognised_with_its_schema_quoted_or_not(query, table):
    from bauta.generate.drift import _wholeTable

    assert _wholeTable(query) == table

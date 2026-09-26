"""What only DuckDB does, against a real file, and what DuckDBDialect does
about it: a session that holds one transaction as the other drivers do,
chunks loaded through Arrow, swaps refused around foreign keys, one process
at a time per file, and the runner starting one job at a time per DuckDB
connection. What every database does is in test_integration_databases.py.

DuckDB runs in this process and needs no server, so this file isn't marked
`integration` and runs by default, as test_integration_sqlite.py does.
"""
import datetime
import decimal
import json
import os
import subprocess
import sys
import textwrap
import uuid

import pytest

from bauta.configuration import Configuration, ConfigurationError, DataJobsFile
from bauta.database import Database
from bauta.database.dialects import duckdb as duckdbModule
from bauta.generate.schema import clearTables
from bauta.jobs.memory import DatabaseMemory, FileMemory
from bauta.jobs.runner import runDataJobs

pytest.importorskip('duckdb')

DATABASE = 'duckdb'


def _people(database, name='people'):

    database.alter('CREATE TABLE {} (id INTEGER PRIMARY KEY, name VARCHAR, amount DECIMAL(12,2))'.format(name))

    return name


# Sessions -----------------------------------------------------------------------

def test_current_schema_resolves_unqualified_names_for_statements_and_streams(liveDatabase, connectionSettings):
    """The stream runs on a cursor of its own, which is a connection of its own
    in DuckDB, and started in `main` until given the session's schema.
    """
    liveDatabase.alter('CREATE SCHEMA sales')
    liveDatabase.alter('CREATE TABLE sales.orders (id INTEGER PRIMARY KEY)')
    liveDatabase.insert(table='sales.orders', data=[(1,), (2,)])
    liveDatabase.close()

    try:
        with Database(connectionSettings=connectionSettings.model_copy(update={'currentSchema': 'sales'})) as database:
            assert database.getPrimaryColumnNames('orders') == ['id']
            columns, chunks = database.stream('SELECT id FROM orders ORDER BY id', chunkSize=10)
            with chunks:
                assert (columns, list(chunks)) == (['id'], [[(1,), (2,)]])
    finally:
        liveDatabase.connect()


def test_a_commit_makes_rows_visible_to_another_connection(liveDatabase, connectionSettings):
    """DuckDB's cursor() is a second connection whose transaction the first's
    commit() doesn't commit. The session is its own cursor, so it does.
    """
    _people(liveDatabase)
    liveDatabase.insert(table='people', data=[(1, 'ana', decimal.Decimal('1.50'))])

    with Database(connectionSettings=connectionSettings) as other:
        assert other.query('SELECT count(*) FROM people') == [(1,)]


def test_statements_are_one_transaction_until_committed(liveDatabase):
    """DuckDB commits each statement on its own by default; the session opens
    a transaction as the other drivers do, so a rollback undoes both.
    """
    _people(liveDatabase)
    liveDatabase.cursor.execute("INSERT INTO people VALUES (1, 'a', 1)")
    liveDatabase.cursor.execute("INSERT INTO people VALUES (2, 'b', 2)")
    liveDatabase.connection.rollback()

    assert liveDatabase.query('SELECT count(*) FROM people') == [(0,)]


def test_a_rollback_with_nothing_to_roll_back_does_not_raise(liveDatabase):
    """DuckDB's own rollback() raises without a transaction, which in an
    except block would replace the error being handled.
    """
    liveDatabase.connection.rollback()
    liveDatabase.connection.rollback()


def test_a_delete_reports_the_rows_it_deleted(liveDatabase):
    """DuckDB returns the count as a row and leaves rowcount at -1; `clear`
    reports it.
    """
    _people(liveDatabase)
    liveDatabase.insert(table='people', data=[(1, 'a', 1), (2, 'b', 2)])

    liveDatabase.cursor.execute('DELETE FROM people')

    assert liveDatabase.cursor.rowcount == 2


def test_clear_empties_a_parent_and_its_child(liveDatabase):
    """DuckDB checks a foreign key against what is committed, so a parent's
    DELETE after its child's, both uncommitted, was refused as still referenced.
    """
    liveDatabase.alter('CREATE TABLE parent (id INTEGER PRIMARY KEY)')
    liveDatabase.alter('CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent (id))')
    liveDatabase.insert(table='parent', data=[(1,)])
    liveDatabase.insert(table='child', data=[(10, 1)])

    assert clearTables(liveDatabase, ['parent', 'child']) == [('child', 1), ('parent', 1)]


# Loads -------------------------------------------------------------------------

def test_a_chunk_loads_through_arrow_in_one_statement(liveDatabase, monkeypatch):
    """executemany runs once per row on DuckDB: 50,000 rows took 13 seconds."""
    _people(liveDatabase)
    monkeypatch.setattr(liveDatabase.cursor, 'executemany', lambda *arguments: pytest.fail('loaded row by row'))

    liveDatabase.insert(table='people', data=[(index, 'n{}'.format(index), decimal.Decimal('1.25')) for index in range(1000)], chunkSize=1000)

    assert liveDatabase.query('SELECT count(*), sum(amount) FROM people') == [(1000, decimal.Decimal('1250.00'))]


def test_a_column_of_numbers_and_text_still_loads_through_arrow(liveDatabase, monkeypatch):
    """SQLite hands back numbers and text together in one column, which Arrow
    can't type, and a Python integer can outgrow any Arrow integer. Such a
    chunk went row by row, 150 times slower: 2,600 rows a second. The column
    goes as text, which DuckDB casts to the column's type.
    """
    liveDatabase.alter('CREATE TABLE mixed (id INTEGER PRIMARY KEY, label VARCHAR, amount INTEGER, big HUGEINT)')
    monkeypatch.setattr(liveDatabase.cursor, 'executemany', lambda *arguments: pytest.fail('loaded row by row'))

    liveDatabase.insert(table='mixed', data=[(1, 5, 7, 2 ** 70), (2, 'five', '8', None)])

    assert liveDatabase.query('SELECT label, amount, big FROM mixed ORDER BY id') == [('5', 7, 2 ** 70), ('five', 8, None)]


def test_a_column_arrow_cannot_carry_as_text_is_loaded_row_by_row(liveDatabase):
    """Bytes beside text would become the text of their repr; that chunk goes
    through executemany, which binds each value as it is.
    """
    liveDatabase.alter('CREATE TABLE odd (id INTEGER PRIMARY KEY, value BLOB)')

    liveDatabase.insert(table='odd', data=[(1, b'\x00'), (2, 'text')])

    assert liveDatabase.query('SELECT value FROM odd ORDER BY id') == [(b'\x00',), (b'text',)]


def test_a_chunk_loads_without_pyarrow(liveDatabase, monkeypatch):
    monkeypatch.setitem(sys.modules, 'pyarrow', None)
    _people(liveDatabase)

    liveDatabase.insert(table='people', data=[(1, 'a', decimal.Decimal('1.00'))])

    assert liveDatabase.query('SELECT name FROM people') == [('a',)]


def test_upsert_updates_existing_rows_and_inserts_new_ones(liveDatabase):
    _people(liveDatabase)
    liveDatabase.insert(table='people', data=[(1, 'old', 1)])

    liveDatabase.upsert(table='people', data=[(1, 'new', 2), (2, 'added', 3), (1, 'newer', 4)])

    assert liveDatabase.query('SELECT id, name, amount FROM people ORDER BY id') == [(1, 'newer', 4), (2, 'added', 3)]


def test_an_aware_timestamp_is_stored_the_same_whatever_the_machines_time_zone(connectionSettings):
    """DuckDB converts through the machine's own zone when a time-zone-aware
    value meets a column without one: 12:00+02:00 was stored as 05:00 in New
    York and 03:00 in Tokyo. Run where the machine isn't UTC, as CI is.
    """
    script = textwrap.dedent('''
        import datetime
        from bauta.configuration import ConnectionConfig, connectionConfig
        from bauta.database import Database
        with Database(connectionConfig(type='duckdb', path={!r})) as database:
            database.alter('CREATE TABLE moments (id INTEGER, naive TIMESTAMP, aware TIMESTAMPTZ)')
            moment = datetime.datetime(2026, 1, 1, 12, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
            database.insert(table='moments', data=[(1, moment, moment)])
            naive, aware = database.query('SELECT naive, aware FROM moments')[0]
            print(naive.isoformat(), aware.astimezone(datetime.timezone.utc).isoformat())
        ''').format(connectionSettings.path)
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=60,
                            env=dict(os.environ, TZ='Asia/Tokyo'))

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ['2026-01-01T10:00:00', '2026-01-01T10:00:00+00:00']


def test_values_other_drivers_return_round_trip(liveDatabase):
    liveDatabase.alter('CREATE TABLE zoo (id INTEGER PRIMARY KEY, exact DECIMAL(38,10), moment TIMESTAMPTZ, key UUID, body JSON, blob BLOB, '
                       'span VARCHAR)')
    moment = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
    key = uuid.uuid4()
    row = (1, decimal.Decimal('123456789012345678.1234567890'), moment, key, {'a': 1}, b'\x00\x01', datetime.timedelta(hours=30))

    liveDatabase.insert(table='zoo', data=[row])

    exact, stored, storedKey, body, blob, span = liveDatabase.query('SELECT exact, moment, key, body, blob, span FROM zoo')[0]
    assert (exact, stored, storedKey, blob, span) == (row[1], moment, key, b'\x00\x01', '30:00:00')
    assert json.loads(body) == {'a': 1}


# Swaps -------------------------------------------------------------------------

def test_swap_exchanges_the_tables_and_views_follow_the_name(liveDatabase):
    _people(liveDatabase)
    liveDatabase.alter('CREATE TABLE people_stage (id INTEGER, name VARCHAR, amount DECIMAL(12,2))')
    liveDatabase.alter('CREATE VIEW everyone AS SELECT name FROM people')
    liveDatabase.insert(table='people', data=[(1, 'old', 1)])
    liveDatabase.insert(table='people_stage', data=[(2, 'new', 2)])

    liveDatabase.swap(targetTable='people', stageTable='people_stage')

    assert liveDatabase.query('SELECT name FROM everyone') == [('new',)]
    assert liveDatabase.query('SELECT name FROM people_stage') == [('old',)]


@pytest.mark.parametrize('obstacle', ['target references', 'target is referenced', 'target has an index'])
def test_swap_of_a_table_duckdb_cannot_rename_is_refused_before_anything_is_renamed(liveDatabase, obstacle):
    """DuckDB refuses to rename a table another references, or one with an
    index -- the swap failed with its bare "Dependency Error" -- and renaming
    one that references another left the other naming it by its old name:
    from then on it couldn't be dropped.
    """
    liveDatabase.alter('CREATE TABLE parent (id INTEGER PRIMARY KEY)')
    if obstacle == 'target references':
        liveDatabase.alter('CREATE TABLE target (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent (id))')
        liveDatabase.alter('CREATE TABLE target_stage (id INTEGER, parent_id INTEGER)')
    elif obstacle == 'target is referenced':
        liveDatabase.alter('CREATE TABLE target (id INTEGER PRIMARY KEY)')
        liveDatabase.alter('CREATE TABLE child (id INTEGER, target_id INTEGER REFERENCES target (id))')
        liveDatabase.alter('CREATE TABLE target_stage (id INTEGER)')
    else:
        liveDatabase.alter('CREATE TABLE target (id INTEGER PRIMARY KEY, v INTEGER)')
        liveDatabase.alter('CREATE INDEX target_v ON target (v)')
        liveDatabase.alter('CREATE TABLE target_stage (id INTEGER, v INTEGER)')

    with pytest.raises(ConfigurationError, match='DuckDB cannot swap such a table'):
        liveDatabase.swap(targetTable='target', stageTable='target_stage')

    liveDatabase.connection.rollback()
    assert liveDatabase.tableExists('target') and liveDatabase.tableExists('target_stage')
    assert not liveDatabase.tableExists('target_tmp')


# One process at a time -----------------------------------------------------------

def test_jobs_on_one_duckdb_connection_run_one_at_a_time_whatever_workers_allows(liveDatabase, connectionSettings, tmp_path):
    """DuckDB lets one process at a time open a file, and every job is a
    process of its own: with two workers, the second job waited a minute on
    the first's file and failed. The runner now starts one job at a time per
    DuckDB connection, and uses the other workers for everything else.
    """
    for table in ('source', 'first', 'second'):
        _people(liveDatabase, table)
    liveDatabase.insert(table='source', data=[(index, 'n{}'.format(index), index) for index in range(2000)])
    liveDatabase.close()

    job = {'sourceConnection': 'lake', 'targetConnection': 'lake', 'sourceQuery': 'SELECT * FROM source', 'insertStrategy': 'upsert',
           'chunkSize': 100}
    jobsFile = Configuration.validateJobConfiguration(
        {'workers': 2, 'jobs': {'first': dict(job, targetTableFinal='first'), 'second': dict(job, targetTableFinal='second')}}, DataJobsFile)

    try:
        result = runDataJobs(jobsFile=jobsFile, connectionConfiguration={'lake': connectionSettings}, memory=FileMemory(tmp_path / 'memory.yaml'),
                             logFile=tmp_path / 'runner.log')
    finally:
        liveDatabase.connect()

    assert [outcome.status.value for outcome in result.outcomes] == ['completed', 'completed']
    first, second = sorted(result.outcomes, key=lambda outcome: outcome.startedAt)
    assert first.finishedAt <= second.startedAt
    assert liveDatabase.query('SELECT (SELECT count(*) FROM first), (SELECT count(*) FROM second)') == [(2000, 2000)]


def test_run_state_is_refused_in_duckdb(connectionSettings):
    """The run holds its run-state connection for as long as it lasts, so a
    job recording its run in the same DuckDB file waited on the run, then
    failed -- a minute per job, and the load it had just finished reported as
    a failure.
    """
    with pytest.raises(ConfigurationError, match='run state cannot be kept in DuckDB'):
        DatabaseMemory(connectionSettings=connectionSettings)


def test_a_file_another_process_holds_is_waited_for_then_refused_saying_why(connectionSettings, monkeypatch):
    holder = subprocess.Popen([sys.executable, '-c', textwrap.dedent('''
        import sys, duckdb
        connection = duckdb.connect({!r})
        print('held', flush=True)
        sys.stdin.read()
        '''.format(connectionSettings.path))], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'held'
        monkeypatch.setattr(duckdbModule, 'LOCK_WAIT_SECONDS', 1.0)
        monkeypatch.setattr(duckdbModule, 'LOCK_POLL_SECONDS', 0.1)

        with pytest.raises(ConfigurationError, match='held open by another process'):
            Database(connectionSettings=connectionSettings)
    finally:
        holder.communicate('')


# Catalog -------------------------------------------------------------------------

def test_the_catalog_lookups_ignore_case_as_duckdb_does(liveDatabase):
    _people(liveDatabase, 'People')

    assert liveDatabase.tableExists('PEOPLE')
    assert liveDatabase.getPrimaryColumnNames('people') == ['id']
    assert [column.name for column in liveDatabase.getColumnDefinitions('people')] == ['id', 'name', 'amount']
    assert liveDatabase.listTables() == ['People']


def test_composite_foreign_keys_keep_their_column_order(liveDatabase):
    liveDatabase.alter('CREATE TABLE k (a INTEGER, b INTEGER, PRIMARY KEY (b, a))')
    liveDatabase.alter('CREATE TABLE ck (x INTEGER, y INTEGER, FOREIGN KEY (y, x) REFERENCES k (b, a))')

    [foreignKey] = liveDatabase.getForeignKeys()

    assert (foreignKey.table, foreignKey.columns, foreignKey.referencedTable, foreignKey.referencedColumns) == ('ck', ('y', 'x'), 'k', ('b', 'a'))


# What else a DuckDB connection is used for --------------------------------------------

def test_history_and_manifests_can_be_kept_in_duckdb(liveDatabase, connectionSettings):
    """Unlike run state, each opens its connection for one write after a cycle
    ends, when no job holds the file. The documented table definitions create
    on DuckDB as they do everywhere else.
    """
    from bauta.jobs.dependencyGraph import JobOutcome, JobStatus
    from bauta.jobs.reporting import DATABASE_HISTORY_SCHEMA, DATABASE_MANIFEST_SCHEMA, DatabaseHistory, DatabaseManifests
    from bauta.jobs.runner import RunResult

    liveDatabase.alter(DATABASE_HISTORY_SCHEMA)
    liveDatabase.alter(DATABASE_MANIFEST_SCHEMA)

    DatabaseHistory(connectionSettings).append(RunResult(outcomes=[JobOutcome(job='copy', status=JobStatus.COMPLETED, rowCount=3)]), 'run-1')
    DatabaseManifests(connectionSettings).write({'jobs': {}}, 'run-1')

    assert [entry['job'] for entry in DatabaseHistory(connectionSettings).read()] == ['copy']
    assert DatabaseManifests(connectionSettings).read('run-1')[1] == {'jobs': {}}


def test_a_subset_plans_and_selects_on_duckdb_as_on_sqlite(liveDatabase):
    """subset's queries are EXISTS over named selections, MATERIALIZED where
    the dialect allows, and DuckDB's foreign keys arrive as arrays rather than
    one row a column.
    """
    from bauta.database.dialects import quoteIdentifier, quoteTableName
    from bauta.generate.subset import planSubset
    from tests.generate.test_subset import SCHEMA

    for statement in filter(str.strip, SCHEMA.split(';')):
        liveDatabase.alter(statement)
    liveDatabase.insert(table='regions', data=[(1, 'eu'), (2, 'us')])
    liveDatabase.insert(table='customers', data=[(index, index % 2 + 1, 'gold' if index % 4 == 0 else 'basic') for index in range(1, 13)])
    liveDatabase.insert(table='orders', data=[(index, index % 12 + 1) for index in range(1, 25)])

    plan = planSubset(liveDatabase.getForeignKeys(), root='customers', where="tier = 'gold'",
                      materialize=liveDatabase.dialect.supportsMaterializedSelections(),
                      quote=lambda name: quoteIdentifier(liveDatabase.type, name),
                      quoteTable=lambda name: quoteTableName(liveDatabase.type, name))

    assert 'MATERIALIZED' in plan.queries['customers']
    assert sorted(row[0] for row in liveDatabase.query(plan.queries['customers'])) == [4, 8, 12]
    assert sorted(row[1] for row in liveDatabase.query(plan.queries['orders'])) == [4, 4, 8, 8, 12, 12]
    assert {row[0] for row in liveDatabase.query(plan.queries['regions'])} == {1}

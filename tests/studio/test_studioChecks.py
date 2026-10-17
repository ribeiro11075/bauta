"""Scheduled checks and what they found that is new, references, the subset
builder, benchmarks and comparing runs.
"""
import datetime
import sqlite3
import time

import pytest

from tests.studio.conftest import KEY
from tests.studio.test_studioFeatures import _run

UTC = datetime.timezone.utc


# The schedule -------------------------------------------------------------------

def test_the_checks_are_due_once_a_day_after_their_time_and_catch_up_after_downtime(app):
    from bauta.studio import checks

    with app.state.database.open() as session:
        assert not checks.due(session, datetime.datetime(2026, 10, 16, 4, 0, tzinfo=UTC))
        checks.setSchedule(session, True, '03:00', ['audit'])
        session.commit()

        assert not checks.due(session, datetime.datetime(2026, 10, 16, 2, 59, tzinfo=UTC))
        # Down at 03:00: due when it starts again.
        assert checks.due(session, datetime.datetime(2026, 10, 16, 9, 30, tzinfo=UTC))

        from bauta.studio.auth import record
        record(session, None, 'ran the scheduled checks')
        session.commit()
        assert not checks.due(session, datetime.datetime.now(UTC).replace(hour=23, minute=59))


def test_a_schedule_is_refused_with_a_bad_time_or_an_unknown_check(signIn):
    admin = signIn('admin')

    assert admin.put('/api/checks/schedule', json={'enabled': True, 'time': '25:00', 'kinds': ['audit']}).status_code == 400
    assert admin.put('/api/checks/schedule', json={'enabled': True, 'time': '03:00', 'kinds': ['astrology']}).status_code == 400
    assert admin.put('/api/checks/schedule', json={'enabled': True, 'time': '3:05', 'kinds': ['audit', 'coverage']}).json() == {
        'enabled': True, 'time': '03:05', 'kinds': ['coverage', 'audit']}
    assert signIn('operator').put('/api/checks/schedule', json={'enabled': False, 'time': '03:00', 'kinds': []}).status_code == 403


def test_a_scheduled_run_keeps_each_check_and_one_failing_stops_none(app, settings, monkeypatch):
    from bauta.studio import checks, workspace

    def broken(session, project, email):
        raise RuntimeError('discovery is down')

    monkeypatch.setitem(checks.RUNNERS, 'discovery', broken)
    with app.state.database.open() as session:
        checks.setSchedule(session, True, '03:00', checks.KINDS)
        outcomes = checks.runScheduled(session, workspace.loadProject(settings))

        assert outcomes['coverage'] == 'done' and outcomes['audit'] == 'done' and outcomes['references'] == 'done'
        assert outcomes['discovery'].startswith('failed: discovery is down')
        assert checks.latestDict(session, 'coverage')['by'] == 'schedule'


def test_a_table_that_appears_between_two_checks_is_new(signIn, settings):
    operator = signIn('operator')
    operator.post('/api/proof/coverage', json={})
    news = operator.get('/api/overview').json()['news']
    assert sorted(entry['table'] for entry in news if entry['kind'] == 'coverage') == ['orders_copy', 'people_copy']

    connection = sqlite3.connect(str(settings.configDirectory.parent / 'demo.db'))
    connection.execute('CREATE TABLE invoices (id INT PRIMARY KEY, billing_email TEXT)')
    connection.commit()
    connection.close()
    operator.post('/api/proof/coverage', json={})

    (new,) = [entry for entry in operator.get('/api/overview').json()['news'] if entry['kind'] == 'coverage']
    assert new['table'] == 'invoices' and new['personalDataColumns'] == ['billing_email'] and new['by'] == 'operator@example.com'


# References ---------------------------------------------------------------------

def test_references_are_checked_from_proof(signIn):
    client = signIn('operator')

    result = client.post('/api/proof/references').json()

    assert result['report']['summary']['orphaned'] == 0
    assert client.get('/api/proof').json()['references']['id'] == result['id']


# The subset builder --------------------------------------------------------------

@pytest.fixture
def shop(settings):
    """A source with a customers -> orders -> lines schema, and an empty copy."""
    source = sqlite3.connect(str(settings.configDirectory.parent / 'shop.db'))
    source.executescript('''
        CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT, country TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), total INTEGER);
        CREATE TABLE lines (id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders(id), sku TEXT);
    ''')
    source.executemany('INSERT INTO customers VALUES (?, ?, ?)', [(index, 'c{}@example.com'.format(index), 'PT' if index % 2 else 'US') for index in range(10)])
    source.executemany('INSERT INTO orders VALUES (?, ?, ?)', [(index, index % 10, index) for index in range(30)])
    source.executemany('INSERT INTO lines VALUES (?, ?, ?)', [(index, index % 30, 'sku') for index in range(60)])
    source.commit()
    source.close()
    target = sqlite3.connect(str(settings.configDirectory.parent / 'shopcopy.db'))
    target.execute('CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT, country TEXT)')
    target.commit()
    target.close()
    connections = settings.configDirectory / 'connections.yaml'
    connections.write_text(connections.read_text() + 'shop:\n  type: sqlite\n  path: ../shop.db\nshopcopy:\n  type: sqlite\n  path: ../shopcopy.db\n')

    return {'connection': 'shop', 'target': 'shopcopy', 'root': 'customers', 'where': "country = 'PT'", 'mask': True}


def test_a_subset_is_previewed_in_load_order_with_its_rows_counted(signIn, shop):
    admin = signIn('admin')

    assert admin.get('/api/subset/tables', params={'connection': 'shop'}).json()['tables'] == ['customers', 'lines', 'orders']
    preview = admin.post('/api/subset/preview', json=dict(shop, count=True)).json()

    assert [(entry['table'], entry['rows'], entry['targetExists']) for entry in preview['tables']] == [
        ('customers', 5, True), ('orders', 15, False), ('lines', 30, False)]
    assert preview['tables'][1]['parents'] == ['customers']
    assert preview['createMissing'].startswith('bauta schema --connection shop --target shopcopy')
    assert 'c1@example.com' not in preview['text'] and '${MASKING_KEY}' in preview['text']


def test_a_subsets_jobs_are_added_to_the_jobs_file_and_validate(signIn, shop, settings, monkeypatch):
    monkeypatch.setenv('MASKING_KEY', 'a-subset-builder-test-masking-key-123456')
    admin = signIn('admin')

    written = admin.post('/api/subset/apply', json=dict(shop, file='jobs.yaml'))

    assert written.status_code == 200, written.text
    jobs = {job['name']: job for job in admin.get('/api/jobs').json()['jobs']}
    assert {'copyPeople', 'copyOrders'} <= set(jobs) and len(jobs) == 5
    assert (settings.configDirectory / 'jobs.yaml').read_text().startswith('# The demo\'s jobs')
    assert admin.post('/api/subset/apply', json=dict(shop, file='jobs.yaml')).status_code == 422


def test_a_subset_says_which_tables_other_jobs_already_load(signIn, shop, settings):
    jobs = settings.configDirectory / 'jobs.yaml'
    jobs.write_text(jobs.read_text() + '''  loadShopCustomers:
    sourceConnection: shop
    sourceQuery: SELECT id, email, country FROM customers
    targetConnection: shopcopy
    targetTableFinal: customers
    insertStrategy: upsert
    unmasked: true
''')

    preview = signIn('admin').post('/api/subset/preview', json=shop).json()

    assert {entry['table']: entry['loadedBy'] for entry in preview['tables']} == {'customers': ['loadShopCustomers'], 'orders': [], 'lines': []}


def test_only_an_admin_builds_a_subset(signIn, shop):
    assert signIn('operator').post('/api/subset/preview', json=shop).status_code == 403


# Benchmarks -----------------------------------------------------------------------

def test_a_benchmark_runs_in_the_background_and_is_kept_with_its_job(signIn):
    client = signIn('operator')

    started = client.post('/api/jobs/copyPeople/bench', json={'rows': 1000}).json()
    assert started['status'] == 'running'
    deadline = time.time() + 60
    while time.time() < deadline:
        bench = client.get('/api/jobs/copyPeople').json()['bench']
        if bench['status'] != 'running':
            break
        time.sleep(0.3)

    assert bench['status'] == 'done', bench
    (measured,) = bench['report']['jobs']
    assert measured['job'] == 'copyPeople' and measured['rows'] == 5 and measured['complete'] is True
    assert KEY not in str(bench)


# Comparing runs ---------------------------------------------------------------

def test_two_runs_compare_job_by_job_with_their_masking(signIn, settings):
    client = signIn('operator')
    earlier = _run(client)
    connection = sqlite3.connect(str(settings.configDirectory.parent / 'demo.db'))
    connection.executemany('INSERT INTO people VALUES (?, ?)', [(index, 'p{}@example.com'.format(index)) for index in range(5, 8)])
    connection.commit()
    connection.close()
    later = _run(client)

    compared = client.get('/api/runs/compare', params={'earlier': earlier, 'later': later}).json()

    jobs = {job['job']: job for job in compared['jobs']}
    assert jobs['copyPeople']['rows'] == {'before': 5, 'after': 8}
    assert compared['manifestsCompared'] is True and jobs['copyPeople']['columns'] == {'added': [], 'removed': [], 'changed': []}
    assert client.get('/api/runs/compare', params={'earlier': earlier, 'later': 'nothing'}).status_code == 404

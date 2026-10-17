"""Studio's API: who may do what, what never leaves it, and that what it
starts and writes is what bauta itself would.
"""
import json
import time

from tests.studio.conftest import KEY, PASSWORD
from fastapi.testclient import TestClient


def _waitForRun(client, launchedId, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        launched = client.get('/api/launched/{}'.format(launchedId)).json()
        if launched['finishedAt'] is not None:
            return launched
        time.sleep(0.2)
    raise AssertionError('the run did not finish: {}'.format(launched))


# Signing in -------------------------------------------------------------------

def test_nothing_is_readable_signed_out_and_me_says_whether_accounts_exist(app):
    client = TestClient(app)

    assert client.get('/api/runs').status_code == 401
    response = client.get('/api/auth/me')
    assert response.status_code == 401 and response.json()['detail'] == {'signedIn': False, 'hasUsers': True}


def test_a_wrong_password_is_refused_and_repeated_failures_are_throttled(app):
    client = TestClient(app, headers={'X-Bauta-Studio': '1'})

    for _ in range(5):
        assert client.post('/api/auth/login', json={'email': 'viewer@example.com', 'password': 'wrong password!'}).status_code == 401
    assert client.post('/api/auth/login', json={'email': 'viewer@example.com', 'password': PASSWORD}).status_code == 429


def test_a_sign_in_without_studios_header_is_refused(app):
    assert TestClient(app).post('/api/auth/login', json={'email': 'viewer@example.com', 'password': PASSWORD}).status_code == 403


def test_the_session_cookie_is_http_only_and_strict(app):
    client = TestClient(app, headers={'X-Bauta-Studio': '1'})
    response = client.post('/api/auth/login', json={'email': 'viewer@example.com', 'password': PASSWORD})

    cookie = response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie


def test_signing_out_ends_the_session(signIn):
    client = signIn('viewer')

    client.post('/api/auth/logout')

    assert client.get('/api/runs').status_code == 401


# Roles and CSRF ---------------------------------------------------------------

def test_a_viewer_reads_every_screen_but_starts_nothing(signIn):
    client = signIn('viewer')

    for path in ['/api/overview', '/api/runs', '/api/jobs', '/api/jobs/copyPeople', '/api/proof', '/api/connections', '/api/configuration']:
        assert client.get(path).status_code == 200, path
    assert client.post('/api/runs', json={'jobs': []}).status_code == 403
    assert client.post('/api/proof/dry-run').status_code == 403
    assert client.get('/api/configuration/file', params={'path': 'jobs.yaml'}).status_code == 403
    assert client.get('/api/users').status_code == 403


def test_a_change_without_studios_header_is_refused_even_signed_in(signIn):
    client = signIn('admin')
    del client.headers['X-Bauta-Studio']

    assert client.post('/api/proof/dry-run').status_code == 403


# What never leaves --------------------------------------------------------------

def test_no_screen_holds_the_masking_key(signIn):
    client = signIn('admin')

    for path in ['/api/overview', '/api/jobs', '/api/jobs/copyPeople', '/api/proof', '/api/connections']:
        assert KEY not in client.get(path).text, path


def test_a_job_shows_its_policy_by_strategy_and_key_fingerprint(signIn):
    job = signIn('viewer').get('/api/jobs/copyPeople').json()

    assert job['masking']['columns'] == {'id': 'keep', 'email': 'email'}
    assert len(job['masking']['keyFingerprint']) == 12
    assert job['file'] == 'jobs.yaml'


# Runs -------------------------------------------------------------------------

def test_an_operator_starts_a_run_that_history_then_shows_as_theirs(signIn):
    client = signIn('operator')

    launched = client.post('/api/runs', json={'jobs': []}).json()
    finished = _waitForRun(client, launched['id'])

    assert finished['exitCode'] == 0, finished.get('log')
    runs = client.get('/api/runs').json()
    assert runs['historyKept'] is True
    (run,) = runs['runs']
    assert run['status'] == 'succeeded' and run['jobCount'] == 2 and run['rowCount'] == 10
    detail = client.get('/api/runs/{}'.format(run['runId'])).json()
    assert [job['job'] for job in detail['jobs']] == ['copyPeople', 'copyOrders']
    assert detail['launched']['email'] == 'operator@example.com' and 'Finished data job runner' in detail['log']
    assert run['startedBy'] == 'operator@example.com'

    proof = client.get('/api/proof').json()
    assert proof['manifest']['state'] == 'intact-unsigned' and [job['job'] for job in proof['manifest']['jobs']] == ['copyPeople']


def test_a_run_of_a_job_that_does_not_exist_is_refused(signIn):
    assert signIn('operator').post('/api/runs', json={'jobs': ['nobody']}).status_code == 400


def test_a_dry_run_finds_nothing_wrong_with_the_demo(signIn):
    assert signIn('operator').post('/api/proof/dry-run').json()['problems'] == []


def test_coverage_names_the_tables_no_job_reads(signIn):
    (coverage,) = signIn('operator').post('/api/proof/coverage', json={}).json()['checks']

    uncovered = sorted(entry['table'] for entry in coverage['report']['tables'] if entry['state'] == 'uncovered')
    assert uncovered == ['orders_copy', 'people_copy']


# Editing in place ---------------------------------------------------------------

def test_an_invalid_edit_is_refused_and_leaves_the_file_as_it_was(signIn, settings):
    client = signIn('admin')
    opened = client.get('/api/configuration/file', params={'path': 'jobs.yaml'}).json()

    response = client.put('/api/configuration/file', params={'path': 'jobs.yaml'},
                          json={'text': opened['text'].replace('insertStrategy: upsert', 'insertStrategy: sideways', 1), 'digest': opened['digest']})

    assert response.status_code == 422 and 'jobs.copyPeople.insertStrategy' in response.text
    assert (settings.configDirectory / 'jobs.yaml').read_text() == opened['text']


def test_a_valid_edit_keeps_comments_a_backup_and_a_record_of_who_and_what(signIn, settings):
    client = signIn('admin')
    opened = client.get('/api/configuration/file', params={'path': 'jobs.yaml'}).json()
    changed = opened['text'].replace('workers: 1', 'workers: 2')

    written = client.put('/api/configuration/file', params={'path': 'jobs.yaml'}, json={'text': changed, 'digest': opened['digest']}).json()

    assert (settings.configDirectory / 'jobs.yaml').read_text() == changed
    assert changed.startswith('# The demo\'s jobs')
    assert (settings.backupDirectory / written['backup']).read_text() == opened['text']
    (entry,) = [entry for entry in client.get('/api/activity').json()['activity'] if entry['action'] == 'edited a jobs file']
    assert entry['email'] == 'admin@example.com' and '-workers: 1' in entry['detail']['diff'] and '+workers: 2' in entry['detail']['diff']


def test_an_edit_of_a_file_changed_since_it_was_opened_is_refused(signIn, settings):
    client = signIn('admin')
    opened = client.get('/api/configuration/file', params={'path': 'jobs.yaml'}).json()
    (settings.configDirectory / 'jobs.yaml').write_text(opened['text'] + '\n# someone else\n')

    response = client.put('/api/configuration/file', params={'path': 'jobs.yaml'},
                          json={'text': opened['text'].replace('workers: 1', 'workers: 2'), 'digest': opened['digest']})

    assert response.status_code == 409


def test_only_the_jobs_files_can_be_read_or_written(signIn):
    client = signIn('admin')

    for path in ['connections.yaml', '../demo.db', '/etc/passwd']:
        assert client.get('/api/configuration/file', params={'path': path}).status_code == 404, path


# Users --------------------------------------------------------------------------

def test_an_admin_adds_a_user_who_can_then_sign_in(signIn, app):
    admin = signIn('admin')

    assert admin.post('/api/users', json={'email': 'New@Example.com', 'role': 'viewer', 'password': 'short'}).status_code == 400
    assert admin.post('/api/users', json={'email': 'New@Example.com', 'role': 'viewer', 'password': PASSWORD}).status_code == 200

    client = TestClient(app, headers={'X-Bauta-Studio': '1'})
    assert client.post('/api/auth/login', json={'email': 'new@example.com', 'password': PASSWORD}).status_code == 200


def test_the_last_admin_cannot_be_demoted_or_deactivated(signIn):
    admin = signIn('admin')
    me = admin.get('/api/auth/me').json()

    assert admin.patch('/api/users/{}'.format(me['id']), json={'role': 'viewer'}).status_code == 400
    assert admin.patch('/api/users/{}'.format(me['id']), json={'active': False}).status_code == 400


def test_a_deactivated_user_is_signed_out_everywhere(signIn):
    viewer = signIn('viewer')
    admin = signIn('admin')
    users = {user['email']: user for user in admin.get('/api/users').json()['users']}

    admin.patch('/api/users/{}'.format(users['viewer@example.com']['id']), json={'active': False})

    assert viewer.get('/api/runs').status_code == 401


def test_no_password_or_its_hash_is_ever_returned(signIn):
    admin = signIn('admin')

    text = json.dumps(admin.get('/api/users').json()) + json.dumps(admin.get('/api/activity').json())
    assert PASSWORD not in text and 'argon2' not in text

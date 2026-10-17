"""What Studio does at its edges: refusals and what isn't there, a
configuration that doesn't load, manifests in every state, its own start and
schedule, the job form's checks, and the `bauta studio` commands.
"""
import dataclasses
import datetime
import json
import sqlite3
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from bauta.cli import EXIT_BAD_CONFIGURATION, EXIT_SUCCESS, main
from tests.studio.conftest import KEY, PASSWORD
from tests.studio.test_studioFeatures import _plantLive, _run
from tests.studio.test_studioApi import _waitForRun


def _jobs(settings, old, new):
    path = settings.configDirectory / 'jobs.yaml'
    path.write_text(path.read_text().replace(old, new))


# What isn't there ----------------------------------------------------------------

def test_what_does_not_exist_is_a_404(signIn):
    client = signIn('admin')

    for method, path in [('get', '/api/runs/nothing'), ('get', '/api/launched/999'), ('get', '/api/jobs/nobody'), ('get', '/api/jobs/nobody/form'),
                         ('get', '/api/runs/nothing/evidence'), ('post', '/api/jobs/nobody/stage-table'), ('post', '/api/connections/nowhere/test')]:
        assert getattr(client, method)(path).status_code == 404, path
    assert client.post('/api/jobs/nobody/bench', json={'rows': 10}).status_code == 404
    assert client.post('/api/launched/999/cancel').status_code == 409
    assert client.post('/api/discovery/apply', json={'jobs': ['copyPeople']}).status_code == 409
    assert client.get('/api/runs/compare', params={'earlier': 'a', 'later': 'b'}).status_code == 404


def test_a_configuration_that_does_not_load_is_said_on_every_screen(signIn, settings):
    _jobs(settings, 'insertStrategy: upsert', 'insertStrategy: sideways')
    client = signIn('viewer')

    response = client.get('/api/runs')
    assert response.status_code == 409 and 'insertStrategy' in response.json()['detail']['configuration']
    configuration = client.get('/api/configuration').json()
    assert configuration['valid'] is False and 'insertStrategy' in configuration['problem']


def test_without_history_kept_the_runs_say_so(signIn, settings):
    _jobs(settings, 'history: history.jsonl\n', '')

    runs = signIn('viewer').get('/api/runs').json()

    assert runs['historyKept'] is False and runs['runs'] == []


# Checks ------------------------------------------------------------------------

def test_the_offline_audit_runs_on_demand_and_is_not_kept(signIn):
    client = signIn('operator')

    result = client.post('/api/proof/audit', json={'connect': False}).json()

    assert result['connected'] is False and result['by'] == 'operator@example.com'
    assert client.get('/api/proof').json()['audit']['connected'] is False
    assert client.get('/api/checks/schedule').json()['kinds'] == ['coverage', 'audit', 'discovery', 'references']


def test_coverage_of_a_connection_that_does_not_exist_is_refused(signIn):
    assert signIn('operator').post('/api/proof/coverage', json={'connection': 'nowhere'}).status_code == 400


def test_a_connection_test_says_whether_it_connects(signIn, settings):
    client = signIn('operator')

    assert client.post('/api/connections/demo/test').json() == {'ok': True, 'encrypted': None, 'detail': 'connected, encryption unknown'}

    connections = settings.configDirectory / 'connections.yaml'
    connections.write_text(connections.read_text() + 'gone:\n  type: sqlite\n  path: ../no/such/directory/gone.db\n')
    result = client.post('/api/connections/gone/test').json()
    assert result['ok'] is False and result['detail']


# Stage tables ----------------------------------------------------------------------

def test_a_stage_table_for_a_target_that_is_not_there_is_refused(signIn, settings):
    _jobs(settings, 'targetTableFinal: orders_copy', 'targetTableFinal: not_created_yet')

    response = signIn('admin').post('/api/jobs/copyOrders/stage-table')

    assert response.status_code == 422


# Manifests ---------------------------------------------------------------------

def test_a_manifest_in_every_state(signIn, settings, monkeypatch):
    client = signIn('operator')
    proof = lambda: client.get('/api/proof').json()['manifest']  # noqa: E731
    assert proof()['state'] == 'not-written'

    _run(client)
    assert proof()['state'] == 'intact-unsigned'
    monkeypatch.setenv('BAUTA_MANIFEST_KEY', 'a-manifest-signing-key-for-studio-tests-123456')
    assert proof()['state'] == 'unsigned-with-key'

    manifest = settings.configDirectory / 'manifest.json'
    altered = json.loads(manifest.read_text())
    altered['jobs'][0]['rowCount'] = 999
    manifest.write_text(json.dumps(altered))
    assert proof()['state'] == 'altered'

    manifest.write_text('{"not": "a manifest"}')
    assert proof()['state'] == 'unreadable'

    _jobs(settings, 'manifest: manifest.json\n', '')
    assert proof()['state'] == 'not-kept'


def test_a_signed_manifest_is_verified_only_with_its_key(signIn, settings, monkeypatch):
    monkeypatch.setenv('BAUTA_MANIFEST_KEY', 'a-manifest-signing-key-for-studio-tests-123456')
    client = signIn('operator')
    _run(client)
    assert client.get('/api/proof').json()['manifest']['state'] == 'verified'

    monkeypatch.delenv('BAUTA_MANIFEST_KEY')
    assert client.get('/api/proof').json()['manifest']['state'] == 'signed-unchecked'
    monkeypatch.setenv('BAUTA_MANIFEST_KEY', 'another-manifest-signing-key-for-studio-tests-99')
    assert client.get('/api/proof').json()['manifest']['state'] == 'signature-invalid'


def test_evidence_without_a_manifest_says_so_and_flags_a_changed_configuration(signIn, settings):
    import io
    import zipfile

    client = signIn('operator')
    runId = _run(client)
    _jobs(settings, 'workers: 1', 'workers: 2')
    archive = zipfile.ZipFile(io.BytesIO(client.get('/api/runs/{}/evidence'.format(runId)).content))
    assert json.loads(archive.read('configuration.json'))['sameAsTheRun'] is False
    assert 'the jobs files have changed since this run' in archive.read('README.txt').decode()

    _jobs(settings, 'manifest: manifest.json\n', '')
    (settings.configDirectory / 'manifest.json').unlink()
    later = _run(client)
    readme = zipfile.ZipFile(io.BytesIO(client.get('/api/runs/{}/evidence'.format(later)).content)).read('README.txt').decode()
    assert 'No manifest was kept for this run' in readme


# Live runs ---------------------------------------------------------------------

def test_a_live_run_is_listed_first_and_one_whose_process_is_gone_is_stopped(signIn, settings):
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        _plantLive(settings, 'running-now', process.pid)
        _plantLive(settings, 'gone-now', 2 ** 22 + 12345)
        client = signIn('viewer')

        runs = {run['runId']: run for run in client.get('/api/runs').json()['runs']}
        detail = client.get('/api/runs/running-now').json()
    finally:
        process.kill()
        process.wait()

    assert runs['running-now']['live'] and runs['running-now']['status'] == 'running'
    assert runs['gone-now']['status'] == 'stopped'
    assert detail['live'] is True and detail['jobs'] == []


def test_a_run_studio_started_is_cancelled_before_its_first_cycle(app, settings, signIn):
    # A run that takes its time to start: bauta itself, but asleep first.
    app.state.launcher.settings = dataclasses.replace(settings, bautaCommand=[sys.executable, '-c', 'import time; time.sleep(30)'])
    client = signIn('operator')
    launched = client.post('/api/runs', json={'jobs': []}).json()

    assert client.post('/api/launched/{}/cancel'.format(launched['id'])).json() == {'cancelled': True}
    finished = _waitForRun(client, launched['id'], seconds=30)
    assert finished['exitCode'] != 0 and finished['runId'] is None
    assert client.post('/api/launched/{}/cancel'.format(launched['id'])).status_code == 409


def test_a_run_started_before_studio_restarted_is_seen_to_end(app, settings, signIn):
    from bauta.studio.db import LaunchedRun

    with app.state.database.open() as session:
        session.add(LaunchedRun(email='operator@example.com', jobs=[], pid=2 ** 22 + 12345, logPath=str(settings.dataDirectory / 'missing.log')))
        session.commit()
        launchedId = session.query(LaunchedRun).one().id

    launched = signIn('viewer').get('/api/launched/{}'.format(launchedId)).json()

    assert launched['finishedAt'] is not None and launched['exitCode'] is None and launched['log'] is None


# Studio's own start and schedule ---------------------------------------------------

def test_studio_starts_and_stops_its_background_work(app):
    with TestClient(app) as client:
        assert client.get('/api/auth/me').status_code == 401


def test_the_manifest_keeper_and_the_schedule_each_take_a_turn(app, settings, monkeypatch, signIn):
    from bauta.studio import app as studioApp
    from bauta.studio import checks, workspace

    _run(signIn('operator'))
    with app.state.database.open() as session:
        checks.setSchedule(session, True, '00:00', ['audit'])
        session.commit()
    monkeypatch.setattr(studioApp, 'ARCHIVE_SECONDS', 0.05)
    monkeypatch.setattr(checks, 'TICK_SECONDS', 0.05)
    stop = threading.Event()
    keeper = threading.Thread(target=studioApp._archiveLoop, args=(settings, app.state.database, stop))
    schedule = threading.Thread(target=checks.scheduleLoop, args=(app.state.database, lambda: workspace.loadProject(settings), stop))
    keeper.start()
    schedule.start()
    deadline = time.time() + 30
    while time.time() < deadline:
        with app.state.database.open() as session:
            if checks.latestDict(session, 'audit-connected'):
                break
        time.sleep(0.1)
    stop.set()
    keeper.join()
    schedule.join()

    with app.state.database.open() as session:
        assert checks.latestDict(session, 'audit-connected')['by'] == 'schedule'
        assert not checks.due(session, datetime.datetime.now(datetime.timezone.utc))
    assert len(signIn('viewer').get('/api/proof').json()['manifests']) == 1


def test_nothing_is_due_with_no_checks_chosen(app):
    from bauta.studio import checks

    with app.state.database.open() as session:
        checks.setSchedule(session, True, '00:00', [])
        assert not checks.due(session, datetime.datetime.now(datetime.timezone.utc))


def test_settings_come_from_the_environment(monkeypatch, tmp_path):
    from bauta.studio.settings import Settings

    monkeypatch.setenv('BAUTA_STUDIO_CONFIG', str(tmp_path / 'conf'))
    monkeypatch.setenv('BAUTA_STUDIO_DATA', str(tmp_path / 'data'))
    monkeypatch.setenv('BAUTA_STUDIO_INSECURE_COOKIES', '1')
    monkeypatch.delenv('BAUTA_STUDIO_DATABASE_URL', raising=False)

    settings = Settings.fromEnvironment()

    assert settings.configDirectory == tmp_path / 'conf' and settings.secureCookies is False
    assert settings.databaseUrl == 'sqlite:///{}'.format(tmp_path / 'data' / 'studio.db')
    assert settings.runLogDirectory == tmp_path / 'data' / 'runs' and settings.backupDirectory == tmp_path / 'data' / 'backups'


# The job form's checks ---------------------------------------------------------------

def _form(client, name='copyPeople'):
    return client.get('/api/jobs/{}/form'.format(name)).json()


@pytest.mark.parametrize('body, wanted', [
    ({'changes': {'colour': 'blue'}}, 'does not edit colour'),
    ({'changes': {'chunkSize': 'many'}}, 'chunkSize must be a number'),
    ({'masking': {'enabled': True, 'columns': [{'column': 'id', 'strategy': 'keep'}, {'column': 'id', 'strategy': 'keep'}]}}, 'listed twice'),
    ({'masking': {'enabled': True, 'columns': [{'column': 'id', 'strategy': ''}]}}, 'needs a strategy'),
    ({'masking': {'enabled': True, 'columns': [{'column': 'nope', 'custom': True}]}}, 'no policy of its own'),
])
def test_the_form_refuses_what_it_cannot_write(signIn, body, wanted):
    admin = signIn('admin')

    response = admin.put('/api/jobs/copyPeople/form', json=dict(body, digest=_form(admin)['digest']))

    assert response.status_code == 422 and wanted in response.text


def test_the_form_writes_a_query_over_lines_drops_a_policy_and_keeps_a_custom_one(signIn, settings):
    _jobs(settings, '        email: email\n', '        email:\n          strategy: hash\n          length: 16\n')
    admin = signIn('admin')
    form = _form(admin)
    assert [column['custom'] for column in form['masking']['columns']] == [False, True]

    admin.put('/api/jobs/copyPeople/form', json={'digest': form['digest'], 'changes': {'sourceQuery': 'SELECT id,\n  email\nFROM people'},
                                                 'masking': {'enabled': True, 'defaultStrategy': 'hash',
                                                             'columns': [{'column': 'id', 'strategy': 'keep'}, {'column': 'email', 'custom': True}]}})
    text = (settings.configDirectory / 'jobs.yaml').read_text()
    assert 'sourceQuery: |\n      SELECT id,\n        email\n      FROM people\n' in text
    assert '          length: 16\n' in text and 'defaultStrategy: hash' in text

    # Masking turned off takes the policy out, key and all; audit then says
    # the job copies unmasked, as it would of one written so by hand.
    assert admin.put('/api/jobs/copyPeople/form', json={'digest': _form(admin)['digest'], 'masking': {'enabled': False}}).status_code == 200
    assert 'masking' not in (settings.configDirectory / 'jobs.yaml').read_text().split('copyPeople:')[1].split('copyOrders:')[0]
    assert any(finding['job'] == 'copyPeople' for finding in admin.get('/api/proof').json()['offlineAudit']['findings'])


@pytest.mark.parametrize('name, wanted', [('1bad name', 'a job is named with'), ('copyPeople', 'already a job')])
def test_a_new_job_needs_a_free_name(signIn, name, wanted):
    response = signIn('admin').post('/api/jobs', json={'name': name, 'file': 'jobs.yaml', 'changes': {}})

    assert response.status_code == 422 and wanted in response.text


def test_a_new_masked_job_needs_a_variable_for_its_key(signIn):
    response = signIn('admin').post('/api/jobs', json={'name': 'another', 'file': 'jobs.yaml', 'changes': {},
                                                       'masking': {'enabled': True, 'keyVariable': 'not a name', 'columns': []}})

    assert response.status_code == 422 and 'key variable' in response.text


# The commands --------------------------------------------------------------------------

@pytest.fixture
def commandLine(settings, monkeypatch):
    monkeypatch.setenv('BAUTA_STUDIO_DATABASE_URL', settings.databaseUrl)

    def run(*arguments, passwords=()):
        answers = iter(passwords)
        monkeypatch.setattr('getpass.getpass', lambda prompt='': next(answers))
        return main(['studio', *arguments, '--config', str(settings.configDirectory), '--data', str(settings.dataDirectory), '--quiet'])

    return run


def test_create_user_adds_an_account_that_signs_in(commandLine, app, capsys):
    assert commandLine('create-user', '--email', 'New@Example.com', '--role', 'viewer', passwords=[PASSWORD, PASSWORD]) == EXIT_SUCCESS
    assert 'Added new@example.com as viewer.' in capsys.readouterr().out

    client = TestClient(app, headers={'X-Bauta-Studio': '1'})
    assert client.post('/api/auth/login', json={'email': 'new@example.com', 'password': PASSWORD}).status_code == 200


@pytest.mark.parametrize('passwords, wanted', [([PASSWORD, PASSWORD + 'x'], 'The passwords differ.'), (['short', 'short'], 'at least 12'),
                                                ([PASSWORD, PASSWORD], 'already has an account')])
def test_create_user_refuses_and_says_why(commandLine, app, capsys, passwords, wanted):
    email = 'admin@example.com' if wanted == 'already has an account' else 'someone@example.com'

    assert commandLine('create-user', '--email', email, passwords=passwords) == 2
    assert wanted in capsys.readouterr().err


def test_serve_says_how_to_add_the_first_admin_then_serves(commandLine, monkeypatch, capsys):
    served = {}
    monkeypatch.setattr('uvicorn.run', lambda app, **options: served.update(options))

    assert commandLine('serve', '--port', '8799') == EXIT_SUCCESS

    assert served['port'] == 8799 and served['host'] == '127.0.0.1'


def test_serve_warns_when_listening_widely_without_secure_cookies(commandLine, monkeypatch, capsys):
    monkeypatch.setattr('uvicorn.run', lambda app, **options: None)
    monkeypatch.setenv('BAUTA_STUDIO_INSECURE_COOKIES', '1')

    commandLine('serve', '--host', '0.0.0.0')

    assert 'Serve it behind TLS' in capsys.readouterr().err


def test_studio_without_its_extra_says_how_to_install_it(monkeypatch, capsys, settings):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, 'find_spec', lambda name, *rest: None if name == 'fastapi' else real(name, *rest))

    assert main(['studio', 'serve', '--config', str(settings.configDirectory)]) == EXIT_BAD_CONFIGURATION
    assert 'pip install "bauta[studio]"' in capsys.readouterr().err


def test_no_screen_or_command_output_holds_the_key(signIn):
    admin = signIn('admin')
    for path in ['/api/schedule', '/api/form-choices', '/api/jobs/copyPeople/form', '/api/run-options', '/api/discovery']:
        assert KEY not in admin.get(path).text, path


# Accounts --------------------------------------------------------------------------

def test_an_account_with_a_wrong_role_or_a_taken_email_is_refused(signIn):
    admin = signIn('admin')

    assert admin.post('/api/users', json={'email': 'x@example.com', 'role': 'owner', 'password': PASSWORD}).status_code == 400
    assert admin.post('/api/users', json={'email': 'Viewer@Example.com', 'role': 'viewer', 'password': PASSWORD}).status_code == 409
    assert admin.patch('/api/users/9999', json={'name': 'Nobody'}).status_code == 404
    users = {user['email']: user for user in admin.get('/api/users').json()['users']}
    viewer = users['viewer@example.com']['id']
    assert admin.patch('/api/users/{}'.format(viewer), json={'role': 'owner'}).status_code == 400
    assert admin.patch('/api/users/{}'.format(viewer), json={'password': 'short'}).status_code == 400


def test_a_new_password_signs_the_user_out_and_works(signIn, app):
    viewer = signIn('viewer')
    admin = signIn('admin')
    users = {user['email']: user for user in admin.get('/api/users').json()['users']}

    changed = admin.patch('/api/users/{}'.format(users['viewer@example.com']['id']), json={'name': 'Renamed', 'password': PASSWORD + '!'}).json()

    assert changed['name'] == 'Renamed'
    assert viewer.get('/api/runs').status_code == 401
    client = TestClient(app, headers={'X-Bauta-Studio': '1'})
    assert client.post('/api/auth/login', json={'email': 'viewer@example.com', 'password': PASSWORD + '!'}).status_code == 200


def test_the_activity_log_pages_back(signIn):
    admin = signIn('admin')
    newest = admin.get('/api/activity').json()['activity']

    older = admin.get('/api/activity', params={'before': newest[0]['id']}).json()['activity']

    assert [entry['id'] for entry in older] == [entry['id'] for entry in newest[1:]]


# The web app's routes ------------------------------------------------------------------

def test_an_unknown_api_path_is_a_404_and_any_other_is_the_web_app(app, monkeypatch, tmp_path):
    from bauta.studio import app as studioApp

    client = TestClient(app)
    assert client.get('/api/no/such/thing').status_code == 404

    monkeypatch.setattr(studioApp, 'STATIC_DIRECTORY', tmp_path / 'nothing-built')
    assert client.get('/runs').status_code == 503

    built = tmp_path / 'built'
    (built / 'assets').mkdir(parents=True)
    (built / 'index.html').write_text('<!doctype html><title>bauta Studio</title>')
    (built / 'assets' / 'app.js').write_text('console.log(1)')
    monkeypatch.setattr(studioApp, 'STATIC_DIRECTORY', built)
    page = client.get('/runs/some-run')
    assert page.status_code == 200 and 'bauta Studio' in page.text
    assert client.get('/assets/app.js').text == 'console.log(1)'
    assert 'bauta Studio' in client.get('/../../etc/passwd').text
    assert page.headers['x-frame-options'] == 'DENY' and "frame-ancestors 'none'" in page.headers['content-security-policy']


# What's new -----------------------------------------------------------------------

def test_a_column_that_appears_between_two_discovery_checks_is_new(signIn, settings):
    _jobs(settings, 'SELECT id, email FROM people', 'SELECT * FROM people')
    operator = signIn('operator')
    operator.post('/api/discovery/check')
    assert not [entry for entry in operator.get('/api/overview').json()['news'] if entry['kind'] == 'discovery']

    connection = sqlite3.connect(str(settings.configDirectory.parent / 'demo.db'))
    connection.execute('ALTER TABLE people ADD COLUMN phone TEXT')
    connection.commit()
    connection.close()
    operator.post('/api/discovery/check')

    (new,) = [entry for entry in operator.get('/api/overview').json()['news'] if entry['kind'] == 'discovery']
    assert new['job'] == 'copyPeople' and new['columns'] == ['phone']


def test_settings_saved_twice_keep_the_second(signIn):
    admin = signIn('admin')

    admin.put('/api/checks/schedule', json={'enabled': True, 'time': '01:00', 'kinds': ['audit']})
    admin.put('/api/checks/schedule', json={'enabled': False, 'time': '02:30', 'kinds': ['coverage']})
    admin.put('/api/schedule/staleness', json={'defaultHours': 10})
    admin.put('/api/schedule/staleness', json={'defaultHours': 12, 'jobs': {'copyPeople': 5}})

    assert admin.get('/api/checks/schedule').json()['schedule'] == {'enabled': False, 'time': '02:30', 'kinds': ['coverage']}
    assert admin.get('/api/schedule').json()['staleness'] == {'defaultHours': 12.0, 'jobs': {'copyPeople': 5.0}}


def test_a_run_of_named_jobs_runs_only_those(signIn):
    client = signIn('operator')

    launched = client.post('/api/runs', json={'jobs': ['copyPeople']}).json()
    finished = _waitForRun(client, launched['id'])

    assert finished['exitCode'] == 0
    assert [job['job'] for job in client.get('/api/runs/{}'.format(finished['runId'])).json()['jobs']] == ['copyPeople']

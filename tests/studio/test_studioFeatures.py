"""Cancelling a run, a run's evidence, the schedule and staleness, discovery
review, the job form and a job's trend.
"""
import hashlib
import io
import json
import socket
import sqlite3
import subprocess
import sys
import time
import zipfile

import pytest

from tests.studio.conftest import KEY
from tests.studio.test_studioApi import _waitForRun


def _run(client):
    launched = client.post('/api/runs', json={'jobs': []}).json()
    finished = _waitForRun(client, launched['id'])
    assert finished['exitCode'] == 0, finished.get('log')

    return finished['runId']


# Full refresh -------------------------------------------------------------------

def test_a_full_refresh_is_offered_only_where_every_incremental_job_can_take_one(signIn, settings):
    client = signIn('viewer')
    assert client.get('/api/run-options').json()['fullRefresh'] == {'possible': True, 'problems': []}

    jobs = settings.configDirectory / 'jobs.yaml'
    jobs.write_text(jobs.read_text().replace('    targetTableFinal: orders_copy\n',
                                             '    targetTableFinal: orders_copy\n    watermarkColumn: id\n    watermarkInitial: 0\n')
                    .replace('SELECT id, total FROM orders', 'SELECT id, total FROM orders WHERE id > {{ watermark }}'))

    options = client.get('/api/run-options').json()['fullRefresh']
    (problem,) = options['problems']
    assert options['possible'] is False and problem['job'] == 'copyOrders' and 'orders_copy' in problem['message']
    assert problem['fix'] == {'stageTable': 'orders_copy_stage',
                              'command': 'bauta schema --connection demo --target demo --table orders_copy --stage-suffix _stage --apply'}


def _makeIncremental(settings):
    jobs = settings.configDirectory / 'jobs.yaml'
    jobs.write_text(jobs.read_text().replace('    targetTableFinal: orders_copy\n',
                                             '    targetTableFinal: orders_copy\n    watermarkColumn: id\n    watermarkInitial: -1\n')
                    .replace('SELECT id, total FROM orders', 'SELECT id, total FROM orders WHERE id > {{ watermark }}'))


def test_an_admin_fixes_it_with_a_stage_table_and_a_full_refresh_then_runs(signIn, settings):
    _makeIncremental(settings)
    admin = signIn('admin')

    assert signIn('operator').post('/api/jobs/copyOrders/stage-table').status_code == 403
    made = admin.post('/api/jobs/copyOrders/stage-table').json()

    assert made['stageTable'] == 'orders_copy_stage' and made['created'] is True
    assert '+    targetTableStage: orders_copy_stage' in made['diff']
    assert admin.get('/api/run-options').json()['fullRefresh'] == {'possible': True, 'problems': []}
    assert admin.post('/api/jobs/copyOrders/stage-table').status_code == 409

    launched = admin.post('/api/runs', json={'jobs': [], 'fullRefresh': True}).json()
    finished = _waitForRun(admin, launched['id'])
    assert finished['exitCode'] == 0, finished.get('log')


# Cancelling -------------------------------------------------------------------

def _plantLive(settings, runId, pid, host=None):
    directory = settings.configDirectory / 'memory.yaml.running'
    directory.mkdir(exist_ok=True)
    (directory / (runId + '.json')).write_text(json.dumps({'runId': runId, 'pid': pid, 'host': host or socket.gethostname(),
                                                           'startedAt': '2026-10-16T02:00:00+00:00', 'jobs': []}))


def test_cancelling_a_live_run_sends_it_sigterm_and_is_recorded(signIn, settings):
    # Standing in for `bauta run`: a process whose command line names bauta.
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', 'bauta'])
    try:
        _plantLive(settings, 'live-run', process.pid)
        client = signIn('operator')

        assert client.post('/api/runs/live-run/cancel').json() == {'cancelled': True}
        assert process.wait(timeout=10) != 0
    finally:
        process.kill()
        process.wait()

    activity = signIn('admin').get('/api/activity').json()['activity']
    assert any(entry['action'] == 'cancelled a run' and entry['target'] == 'live-run' for entry in activity)


def test_a_run_elsewhere_or_already_over_is_not_cancelled(signIn, settings):
    client = signIn('operator')
    _plantLive(settings, 'elsewhere', 1, host='another-host')
    _plantLive(settings, 'gone', 2 ** 22 + 12345)

    assert 'another-host' in client.post('/api/runs/elsewhere/cancel').json()['detail']
    assert client.post('/api/runs/gone/cancel').status_code == 409
    assert client.post('/api/runs/nothing/cancel').status_code == 409
    assert signIn('viewer').post('/api/runs/gone/cancel').status_code == 403


# Evidence ---------------------------------------------------------------------

def test_a_runs_evidence_holds_its_manifest_verified_with_checksums_and_no_key(signIn):
    client = signIn('operator')
    runId = _run(client)

    response = client.get('/api/runs/{}/evidence'.format(runId))

    assert response.headers['content-type'] == 'application/zip'
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(archive.namelist())
    assert {'README.txt', 'run.json', 'manifest.json', 'manifest-verification.json', 'configuration.json', 'audit.json', 'audit.html',
            'SHA256SUMS.txt'} <= names
    assert json.loads(archive.read('manifest.json'))['runId'] == runId
    assert json.loads(archive.read('manifest-verification.json'))['verified'] is True
    assert json.loads(archive.read('configuration.json'))['sameAsTheRun'] is True
    for line in archive.read('SHA256SUMS.txt').decode().splitlines():
        digest, name = line.split('  ')
        assert hashlib.sha256(archive.read(name)).hexdigest() == digest
    assert not any(KEY.encode() in archive.read(name) for name in names)


def test_each_runs_manifest_is_kept_though_the_file_is_replaced(signIn):
    client = signIn('operator')
    first, second = _run(client), _run(client)

    for runId in (first, second):
        archive = zipfile.ZipFile(io.BytesIO(client.get('/api/runs/{}/evidence'.format(runId)).content))
        assert json.loads(archive.read('manifest.json'))['runId'] == runId
    assert len(client.get('/api/proof').json()['manifests']) == 2


# Schedule and staleness -------------------------------------------------------------

def test_jobs_that_never_completed_are_stale_until_a_run_completes_them(signIn):
    client = signIn('operator')
    before = {entry['job']: entry for entry in client.get('/api/schedule').json()['jobs']}
    assert before['copyPeople']['stale'] and before['copyPeople']['neverCompleted']

    _run(client)

    after = {entry['job']: entry for entry in client.get('/api/schedule').json()['jobs']}
    assert not after['copyPeople']['stale'] and after['copyPeople']['lastCompletedAt'] and after['copyPeople']['state'] == 'due'
    assert 'watermark' not in after['copyPeople']


def test_a_job_goes_stale_past_its_own_threshold(signIn):
    _run(signIn('operator'))
    admin = signIn('admin')

    assert admin.put('/api/schedule/staleness', json={'defaultHours': 0}).status_code == 400
    admin.put('/api/schedule/staleness', json={'defaultHours': 26, 'jobs': {'copyOrders': 0.0002}})
    time.sleep(1.5)

    jobs = {entry['job']: entry for entry in admin.get('/api/schedule').json()['jobs']}
    assert jobs['copyOrders']['stale'] and jobs['copyOrders']['ownThreshold']
    assert not jobs['copyPeople']['stale']
    assert [entry['job'] for entry in admin.get('/api/overview').json()['stale']] == ['copyOrders']


# Discovery --------------------------------------------------------------------

def test_a_new_column_is_proposed_then_accepted_into_the_jobs_file(signIn, settings):
    connection = sqlite3.connect(str(settings.configDirectory.parent / 'demo.db'))
    connection.execute('ALTER TABLE people ADD COLUMN phone TEXT')
    connection.execute("UPDATE people SET phone = '+1 555 0100'")
    connection.commit()
    connection.close()
    jobs = settings.configDirectory / 'jobs.yaml'
    jobs.write_text(jobs.read_text().replace('SELECT id, email FROM people', 'SELECT * FROM people'))

    check = signIn('operator').post('/api/discovery/check').json()['check']

    (drift,) = check['jobs']
    assert drift['job'] == 'copyPeople' and drift['file'] == 'jobs.yaml'
    assert [added['column'] for added in drift['added']] == ['phone'] and drift['added'][0]['reason']
    assert signIn('operator').post('/api/discovery/apply', json={'jobs': ['copyPeople']}).status_code == 403

    admin = signIn('admin')
    written = admin.post('/api/discovery/apply', json={'jobs': ['copyPeople']}).json()['written']

    assert '+        phone:' in written[0]['diff']
    job = admin.get('/api/jobs/copyPeople').json()
    assert 'phone' in job['masking']['columns']


# The job form ------------------------------------------------------------------

def test_the_form_reads_a_job_without_its_key(signIn):
    response = signIn('admin').get('/api/jobs/copyPeople/form')
    form = response.json()

    assert KEY not in response.text
    assert form['masking']['key'] == {'source': 'file'}
    assert form['fields']['sourceQuery'] == 'SELECT id, email FROM people'
    assert form['inherited']['chunkSize'] == 5000 and form['inherited']['active'] is True
    assert [column['strategy'] for column in form['masking']['columns']] == ['keep', 'email']


def test_the_form_writes_only_what_changed_and_keeps_the_rest_of_the_file(signIn, settings):
    admin = signIn('admin')
    form = admin.get('/api/jobs/copyPeople/form').json()

    written = admin.put('/api/jobs/copyPeople/form', json={'digest': form['digest'], 'changes': {'chunkSize': 250},
                                                           'masking': {'enabled': True, 'columns': [{'column': 'id', 'strategy': 'keep'},
                                                                                                    {'column': 'email', 'strategy': 'hash'}]}}).json()

    text = (settings.configDirectory / 'jobs.yaml').read_text()
    assert text.startswith('# The demo\'s jobs: a comment that an edit must keep.')
    assert '    chunkSize: 250\n' in text and '        email: hash\n' in text and 'key: {}'.format(KEY) in text
    changed = [line for line in written['diff'].splitlines() if line[:1] in '+-' and line[:3] not in ('+++', '---')]
    assert changed == ['-        email: email', '+        email: hash', '+    chunkSize: 250']


def test_an_invalid_form_is_refused_and_a_stale_one_conflicts(signIn, settings):
    admin = signIn('admin')
    form = admin.get('/api/jobs/copyPeople/form').json()

    refused = admin.put('/api/jobs/copyPeople/form', json={'digest': form['digest'], 'changes': {'insertStrategy': 'sideways'}})
    assert refused.status_code == 422
    assert admin.put('/api/jobs/copyPeople/form', json={'digest': 'stale', 'changes': {'chunkSize': 9}}).status_code == 409


def test_a_new_job_starts_from_what_it_would_inherit(signIn, settings):
    jobs = settings.configDirectory / 'jobs.yaml'
    jobs.write_text(jobs.read_text().replace('workers: 1\n', 'workers: 1\ndefaults:\n  sourceConnection: demo\n  chunkSize: 250\n'))

    inherited = signIn('admin').get('/api/form-choices').json()['inherited']

    assert inherited['active'] is True and inherited['unmasked'] is False
    assert inherited['sourceConnection'] == 'demo' and inherited['chunkSize'] == 250 and 'targetConnection' not in inherited


def test_a_new_job_is_added_with_a_key_named_by_variable(signIn, settings, monkeypatch):
    monkeypatch.setenv('NEW_JOB_KEY', 'a-new-job-masking-key-for-the-form-test')
    admin = signIn('admin')

    response = admin.post('/api/jobs', json={'name': 'copyOrdersMasked', 'file': 'jobs.yaml', 'changes': {
        'sourceConnection': 'demo', 'sourceQuery': 'SELECT id, total FROM orders', 'targetConnection': 'demo', 'targetTableFinal': 'orders_copy',
        'insertStrategy': 'upsert'}, 'masking': {'enabled': True, 'keyVariable': 'NEW_JOB_KEY', 'columns': [{'column': 'id', 'strategy': 'keep'},
                                                                                                          {'column': 'total', 'strategy': 'keep'}]}})

    assert response.status_code == 200, response.text
    assert 'key: ${NEW_JOB_KEY}' in (settings.configDirectory / 'jobs.yaml').read_text()
    assert admin.get('/api/jobs/copyOrdersMasked').json()['masking']['columns'] == {'id': 'keep', 'total': 'keep'}


# Trends -----------------------------------------------------------------------

def test_a_jobs_page_holds_its_recent_runs_for_its_trend(signIn):
    client = signIn('operator')
    _run(client)
    _run(client)

    history = client.get('/api/jobs/copyPeople').json()['history']

    assert [record['status'] for record in history] == ['completed', 'completed'] and history[0]['rowCount'] == 5


@pytest.fixture(autouse=True)
def _noArchiveWait(monkeypatch):
    """The manifest archive is also kept on each read, which is what these
    tests rely on; the background loop needn't wait its interval.
    """
    from bauta.studio import app

    monkeypatch.setattr(app, 'ARCHIVE_SECONDS', 3600.0)

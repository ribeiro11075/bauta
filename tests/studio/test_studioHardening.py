"""What Studio does when it is pushed: failures that quote data, sign-ins
nobody should be making, requests that race, and restarts."""
import datetime
import logging
import time

from fastapi.testclient import TestClient
from sqlalchemy import select

from bauta.studio import auth, checks, evidence
from bauta.studio.app import createApp
from bauta.studio.auth import LoginThrottle
from bauta.studio.db import Activity, ArchivedManifest, Check, User, UserSession
from tests.studio.conftest import PASSWORD

VALUE = 'ann@corp.example'
QUOTING = 'invalid input syntax for type integer: "{}"'.format(VALUE)


class DriverError(Exception):
    """As a driver raises it: from outside bauta, quoting the value."""


DriverError.__module__ = 'psycopg.errors'


def _raises(*arguments, **keywords):
    raise DriverError(QUOTING)


def _forgiving(app, client):
    """`client`'s sign-in, on a client that returns a server's 500 rather than
    raising it, as a browser would see it."""

    return TestClient(app, headers=dict(client.headers), cookies=client.cookies, raise_server_exceptions=False)


def test_a_scheduled_check_keeps_no_value_a_driver_quoted(app, monkeypatch):
    """Its str(error) went into the activity log whole."""

    monkeypatch.setitem(checks.RUNNERS, 'audit', _raises)
    with app.state.database.open() as session:
        checks.setSchedule(session, True, '03:00', ['audit'])
        session.commit()
        checks.runScheduled(session, None)
        detail = session.scalars(select(Activity).order_by(Activity.id.desc())).first().detail

    assert VALUE not in str(detail) and 'failed' in detail['audit']


def test_a_check_that_cannot_reach_a_database_says_why_without_the_value(app, signIn, monkeypatch):
    """It was an unhandled 500, its message written out by the server."""

    monkeypatch.setattr(checks, 'runDiscovery', _raises)
    response = _forgiving(app, signIn('operator')).post('/api/discovery/check')

    assert response.status_code == 422 and 'could not check for new columns' in response.text and VALUE not in response.text


def test_an_unexpected_failure_is_answered_and_logged_without_the_value(app, signIn, monkeypatch, caplog):
    """Raised to the server, uvicorn wrote the driver's message as it stood."""

    monkeypatch.setattr(evidence, 'archiveManifest', _raises)
    with caplog.at_level(logging.ERROR, logger='bauta.studio.app'):
        response = _forgiving(app, signIn('viewer')).get('/api/overview')

    assert response.status_code == 500 and VALUE not in response.text
    assert response.headers['X-Frame-Options'] == 'DENY'
    assert 'GET /api/overview failed' in caplog.text and VALUE not in caplog.text


def test_a_manifest_archived_twice_at_once_is_kept_once(app):
    """Two requests both finding it missing failed the second on its key."""

    class Project:
        def readManifest(self):
            return 'manifest.json', {'runId': 'run-1'}

    first, second = app.state.database.open(), app.state.database.open()
    get = first.get

    def getThenLoseTheRace(*arguments, **keywords):
        found = get(*arguments, **keywords)
        evidence.archiveManifest(second, Project())
        return found

    first.get = getThenLoseTheRace
    try:
        assert evidence.archiveManifest(first, Project()) == 'run-1'
        assert len(list(second.scalars(select(ArchivedManifest)))) == 1
    finally:
        first.close()
        second.close()


def test_a_benchmark_interrupted_by_a_restart_does_not_stay_running(app, settings, signIn):
    """Its job's Measure button stayed disabled for good."""

    with app.state.database.open() as session:
        session.add(Check(kind='bench', email='a@example.com', result={'job': 'copyPeople', 'rows': 1, 'status': 'running'}))
        session.commit()
    app.state.database.close()

    restarted = createApp(settings)
    client = TestClient(restarted, headers={'X-Bauta-Studio': '1'})
    assert client.post('/api/auth/login', json={'email': 'viewer@example.com', 'password': PASSWORD}).status_code == 200
    bench = client.get('/api/jobs/copyPeople').json()['bench']
    restarted.state.database.close()

    assert bench['status'] == 'failed' and 'restarted' in bench['problem']


def test_a_job_is_measured_once_at_a_time_and_only_so_many_at_all(app, signIn):
    client = signIn('operator')
    with app.state.database.open() as session:
        session.add(Check(kind='bench', email='a@example.com', result={'job': 'copyPeople', 'rows': 1, 'status': 'running'}))
        session.commit()
    assert client.post('/api/jobs/copyPeople/bench', json={'rows': 10}).status_code == 409

    with app.state.database.open() as session:
        session.add(Check(kind='bench', email='a@example.com', result={'job': 'other', 'rows': 1, 'status': 'running'}))
        session.commit()
    response = client.post('/api/jobs/copyOrders/bench', json={'rows': 10})
    assert response.status_code == 409 and 'already running' in response.text


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_failures_from_one_address_do_not_lock_out_another():
    """An email alone was the key, so anyone could lock an admin out."""

    throttle = LoginThrottle(clock=Clock())
    for _ in range(auth.LOGIN_ATTEMPTS):
        throttle.failed('admin@example.com', 'attacker')

    assert not throttle.allowed('admin@example.com', 'attacker')
    assert throttle.allowed('admin@example.com', 'admin')


def test_one_address_trying_many_accounts_is_stopped():
    throttle = LoginThrottle(clock=Clock())
    for index in range(auth.LOGIN_ATTEMPTS_PER_ADDRESS):
        throttle.failed('{}@example.com'.format(index), 'attacker')

    assert not throttle.allowed('new@example.com', 'attacker')
    assert throttle.allowed('new@example.com', 'someone-else')


def test_many_addresses_trying_one_account_are_stopped():
    throttle = LoginThrottle(clock=Clock())
    for index in range(auth.LOGIN_ATTEMPTS_PER_EMAIL):
        throttle.failed('admin@example.com', str(index))

    assert not throttle.allowed('admin@example.com', 'anyone')


def test_signing_in_clears_only_that_persons_own_failures():
    throttle = LoginThrottle(clock=Clock())
    for index in range(auth.LOGIN_ATTEMPTS_PER_ADDRESS):
        throttle.failed('{}@example.com'.format(index), 'attacker')
    throttle.succeeded('own@example.com', 'attacker')

    assert not throttle.allowed('new@example.com', 'attacker')


def test_the_throttle_remembers_nothing_for_who_only_asks_and_forgets_failures():
    """Every email asked about stayed in memory for good."""

    clock = Clock()
    throttle = LoginThrottle(clock=clock)
    for index in range(1000):
        throttle.allowed('{}@example.com'.format(index), 'somewhere')
    assert not throttle._failures

    throttle.failed('gone@example.com', 'somewhere')
    clock.now += auth.LOGIN_WINDOW_SECONDS + 1
    throttle.failed('new@example.com', 'elsewhere')

    assert not any('gone@example.com' in key for key in throttle._failures)


def test_too_many_password_checks_at_once_are_refused(app, monkeypatch):
    monkeypatch.setattr(auth, 'PASSWORD_CHECK_WAIT_SECONDS', 0.01)
    held = 0
    try:
        while auth._passwordChecks.acquire(blocking=False):
            held += 1
        started = time.monotonic()
        response = TestClient(app, headers={'X-Bauta-Studio': '1'}).post('/api/auth/login', json={'email': 'viewer@example.com', 'password': PASSWORD})
    finally:
        for _ in range(held):
            auth._passwordChecks.release()

    assert response.status_code == 503 and response.headers['Retry-After'] and time.monotonic() - started < 5


def test_sessions_past_their_time_are_removed_as_studio_starts(app, settings):
    """One was removed only if its browser came back, so the table only grew."""

    now = datetime.datetime.now(datetime.timezone.utc)
    with app.state.database.open() as session:
        userId = session.scalar(select(User.id).limit(1))
        session.add(UserSession(tokenHash='expired', userId=userId, expiresAt=now - datetime.timedelta(hours=1)))
        session.add(UserSession(tokenHash='current', userId=userId, expiresAt=now + datetime.timedelta(hours=1)))
        session.commit()

    restarted = createApp(settings)
    with restarted.state.database.open() as session:
        kept = list(session.scalars(select(UserSession.tokenHash)))
    restarted.state.database.close()

    assert kept == ['current']


def test_a_read_only_files_connection_is_tested_for_reading(settings, tmp_path, signIn):
    """Asked to be writable, a read-only bucket failed its connection test."""

    lake = tmp_path / 'lake'
    lake.mkdir()
    lake.chmod(0o555)
    connections = settings.configDirectory / 'connections.yaml'
    connections.write_text(connections.read_text() + 'lake:\n  type: files\n  root: {}\n  readOnly: true\n'.format(lake))
    try:
        result = signIn('operator').post('/api/connections/lake/test').json()
    finally:
        lake.chmod(0o755)

    assert result == {'ok': True, 'detail': 'readable'}


def _edit(client, path, change):
    opened = client.get('/api/configuration/file', params={'path': path}).json()
    return client.put('/api/configuration/file', params={'path': path}, json={'text': change(opened['text']), 'digest': opened['digest']})


def test_jobs_files_outside_the_configuration_directory_can_be_edited(settings, signIn):
    """Checked in a copy of the directory, an include reaching out of it
    matched nothing, and every edit was refused."""

    shared = settings.configDirectory.parent / 'shared'
    shared.mkdir()
    (shared / 'extra.yaml').write_text('jobs:\n  extra:\n    sourceConnection: demo\n    sourceQuery: SELECT id, total FROM orders\n'
                                       '    targetConnection: demo\n    targetTableFinal: orders_extra\n    insertStrategy: upsert\n    unmasked: true\n')
    jobs = settings.configDirectory / 'jobs.yaml'
    jobs.write_text('include: ["../shared/*.yaml"]\n' + jobs.read_text())
    admin = signIn('admin')

    assert _edit(admin, 'jobs.yaml', lambda text: text + '# edited\n').status_code == 200
    assert _edit(admin, '../shared/extra.yaml', lambda text: text + '# edited\n').status_code == 200
    assert (shared / 'extra.yaml').read_text().endswith('# edited\n')


def test_an_edit_checks_without_copying_what_else_is_in_the_directory(settings, signIn):
    """A directory holding run state or a database was copied on every edit,
    and a file a run removed meanwhile failed the copy."""

    (settings.configDirectory / 'unreadable').mkdir(mode=0)
    try:
        assert _edit(signIn('admin'), 'jobs.yaml', lambda text: text + '# edited\n').status_code == 200
    finally:
        (settings.configDirectory / 'unreadable').chmod(0o755)


def test_an_invalid_edit_leaves_the_file_as_it_was(settings, signIn):
    jobs = settings.configDirectory / 'jobs.yaml'
    before = jobs.read_text()

    assert _edit(signIn('admin'), 'jobs.yaml', lambda text: text + 'jobs: [not, a, mapping\n').status_code == 422
    assert jobs.read_text() == before


TRANSFORM = '    sourceQuery: SELECT id, total FROM orders\n    sourceQueryColumnTransforms:\n      total: ["{}"]\n'


def _withTransform(reference):
    return lambda text: text.replace('    sourceQuery: SELECT id, total FROM orders\n', TRANSFORM.format(reference))


def test_an_edit_cannot_start_calling_any_module_it_likes(signIn):
    """A transform is any callable in any installed module, so `os:system`
    saved here would have run each value as a command."""

    response = _edit(signIn('admin'), 'jobs.yaml', _withTransform('os:system'))

    assert response.status_code == 422 and 'BAUTA_STUDIO_TRANSFORM_MODULES' in response.text


def test_an_edit_may_name_bautas_own_transforms(signIn):
    assert _edit(signIn('admin'), 'jobs.yaml', _withTransform('bauta.transform.builtinTransforms:collapseWhitespace')).status_code == 200


def test_a_module_the_files_already_use_stays_usable(settings, signIn):
    jobs = settings.configDirectory / 'jobs.yaml'
    jobs.write_text(_withTransform('math:floor')(jobs.read_text()))

    response = _edit(signIn('admin'), 'jobs.yaml', lambda text: text + '# edited\n')

    assert response.status_code == 200, response.text


def test_a_module_can_be_allowed_with_what_is_under_it(app, settings, signIn):
    from dataclasses import replace

    allowed = createApp(replace(settings, transformModules=('xml',)))
    client = TestClient(allowed, headers={'X-Bauta-Studio': '1'})
    assert client.post('/api/auth/login', json={'email': 'admin@example.com', 'password': PASSWORD}).status_code == 200

    response = _edit(client, 'jobs.yaml', _withTransform('xml.sax.saxutils:escape'))
    allowed.state.database.close()

    assert response.status_code == 200, response.text

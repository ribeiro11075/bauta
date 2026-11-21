"""Two-factor sign-in with an authenticator app, after a password."""
import dataclasses
import json
import stat
import time

import pytest

pytest.importorskip('fastapi', reason='bauta Studio needs its extra: pip install "bauta[studio]"')

import pyotp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from bauta.project import UsageError  # noqa: E402
from bauta.studio.app import createApp  # noqa: E402
from bauta.studio.auth import SESSION_COOKIE, hashPassword  # noqa: E402
from bauta.studio.db import Activity, User  # noqa: E402
from bauta.studio.settings import Settings, SsoSettings  # noqa: E402
from bauta.studio.twofactor import PENDING_COOKIE, SecretBox  # noqa: E402
from tests.studio.conftest import PASSWORD  # noqa: E402


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """The time, held ten seconds into a 30-second step, so no test's codes
    straddle two; a test moves it on itself."""

    now = [(time.time() // 30) * 30 + 10]
    monkeypatch.setattr(time, 'time', lambda: now[0])
    return now


@pytest.fixture
def studio(settings):
    """A Studio with `requireTwoFactor` as given, and a user of each role."""

    made = []

    def make(requireTwoFactor='off', **changes):
        application = createApp(dataclasses.replace(settings, requireTwoFactor=requireTwoFactor, **changes))
        with application.state.database.open() as session:
            for role in ('viewer', 'operator', 'admin'):
                session.add(User(email='{}@example.com'.format(role), role=role, passwordHash=hashPassword(PASSWORD)))
            session.commit()
        made.append(application)
        return application

    yield make
    for application in made:
        application.state.database.close()


def _client(application):
    return TestClient(application, headers={'X-Bauta-Studio': '1'})


def _password(application, role):
    client = _client(application)
    response = client.post('/api/auth/login', json={'email': '{}@example.com'.format(role), 'password': PASSWORD})
    assert response.status_code == 200, response.text
    return client, response.json()


def _code(secret, offset=0):
    return pyotp.TOTP(secret).at(time.time() + offset)


def _fresh(secret):
    """A code no sign-in has taken yet: setting up took the current step's."""

    return _code(secret, 30)


def _turnOn(application, role='viewer'):
    """Sets two-factor sign-in up for `role`, signed in; its secret and codes."""

    client, _ = _password(application, role)
    details = client.post('/api/auth/2fa/setup').json()
    confirmed = client.post('/api/auth/2fa/confirm', json={'code': _code(details['secret'])})
    assert confirmed.status_code == 200, confirmed.text
    return details['secret'], confirmed.json()['recoveryCodes']


def _user(application, email):
    with application.state.database.open() as session:
        return session.scalar(select(User).where(User.email == email))


def _actions(application):
    with application.state.database.open() as session:
        return [(entry.action, entry.target, entry.detail) for entry in session.scalars(select(Activity).order_by(Activity.id))]


# Setting it up ----------------------------------------------------------------

def test_an_app_is_set_up_from_a_qr_code_and_on_once_a_code_proves_it(studio):
    application = studio()
    client, _ = _password(application, 'viewer')

    details = client.post('/api/auth/2fa/setup').json()
    assert details['qr'].startswith('data:image/svg+xml') and details['uri'].startswith('otpauth://totp/')
    assert 'issuer=bauta%20Studio' in details['uri'] and 'viewer%40example.com' in details['uri']
    assert client.post('/api/auth/2fa/confirm', json={'code': '000000'}).status_code == 401
    assert client.get('/api/auth/me').json()['twoFactor'] is False

    confirmed = client.post('/api/auth/2fa/confirm', json={'code': _code(details['secret'])})

    assert confirmed.status_code == 200 and len(confirmed.json()['recoveryCodes']) == 10
    assert client.get('/api/auth/me').json()['twoFactor'] is True


def test_turning_it_on_signs_the_account_out_everywhere_else(studio):
    application = studio()
    elsewhere, _ = _password(application, 'viewer')

    _turnOn(application)

    assert elsewhere.get('/api/auth/me').status_code == 401


def test_the_secret_is_kept_encrypted_with_a_key_only_studio_reads(studio, settings):
    application = studio()
    secret, _ = _turnOn(application)

    stored = _user(application, 'viewer@example.com').totpSecret
    assert secret not in stored and SecretBox.load(settings.keyFile).decrypt(stored) == secret
    assert stat.S_IMODE(settings.keyFile.stat().st_mode) == 0o600


def test_an_account_that_signs_in_only_through_the_provider_sets_no_app_up(studio, settings):
    application = studio()
    with application.state.database.open() as session:
        session.scalar(select(User).where(User.email == 'viewer@example.com')).passwordHash = None
        session.commit()
    client = _client(application)
    with application.state.database.open() as session:
        from bauta.studio.auth import startSession
        from starlette.responses import Response
        response = Response()
        startSession(session, session.scalar(select(User).where(User.email == 'viewer@example.com')), response, 1, False)
        client.cookies.set(SESSION_COOKIE, response.headers['set-cookie'].split('=', 1)[1].split(';', 1)[0])

    assert client.post('/api/auth/2fa/setup').status_code == 409


# Signing in with it -----------------------------------------------------------

def test_a_password_alone_no_longer_signs_in(studio):
    application = studio()
    secret, _ = _turnOn(application)

    client, answer = _password(application, 'viewer')

    assert answer == {'twoFactorStep': 'code'} and client.get('/api/auth/me').status_code == 401
    assert client.get('/api/runs').status_code == 401
    wrong = next(code for code in ('123456', '654321', '111111') if code not in {_code(secret, offset) for offset in (-30, 0, 30)})
    assert client.post('/api/auth/2fa/verify', json={'code': wrong}).status_code == 401

    signedIn = client.post('/api/auth/2fa/verify', json={'code': _fresh(secret)})
    assert signedIn.status_code == 200 and client.get('/api/auth/me').json()['email'] == 'viewer@example.com'
    assert PENDING_COOKIE not in client.cookies
    assert ('signed in', '', {'method': 'password', 'secondFactor': 'authenticator'}) in _actions(application)


def test_a_code_is_taken_once(studio):
    application = studio()
    secret, _ = _turnOn(application)
    code = _code(secret, 30)
    first, _ = _password(application, 'viewer')
    assert first.post('/api/auth/2fa/verify', json={'code': code}).status_code == 200

    second, _ = _password(application, 'viewer')

    assert second.post('/api/auth/2fa/verify', json={'code': code}).status_code == 401


def test_a_code_from_a_phone_a_step_ahead_is_taken_but_not_the_step_already_taken(studio):
    application = studio()
    secret, _ = _turnOn(application)
    client, _ = _password(application, 'viewer')

    assert client.post('/api/auth/2fa/verify', json={'code': _code(secret)}).status_code == 401
    assert client.post('/api/auth/2fa/verify', json={'code': _code(secret, 30)}).status_code == 200


def test_a_recovery_code_signs_in_once(studio):
    application = studio()
    _, codes = _turnOn(application)
    client, _ = _password(application, 'viewer')

    answer = client.post('/api/auth/2fa/verify', json={'code': codes[0].lower().replace('-', ' ')})

    assert answer.status_code == 200 and answer.json()['recoveryCodesLeft'] == 9
    again, _ = _password(application, 'viewer')
    assert again.post('/api/auth/2fa/verify', json={'code': codes[0]}).status_code == 401
    assert ('signed in', '', {'method': 'password', 'secondFactor': 'recovery code'}) in _actions(application)


def test_another_accounts_recovery_code_is_refused(studio):
    application = studio()
    _turnOn(application, 'viewer')
    _, operatorCodes = _turnOn(application, 'operator')
    client, _ = _password(application, 'viewer')

    assert client.post('/api/auth/2fa/verify', json={'code': operatorCodes[0]}).status_code == 401


def test_a_code_needs_the_right_password_first(studio):
    application = studio()
    secret, _ = _turnOn(application)

    assert _client(application).post('/api/auth/2fa/verify', json={'code': _fresh(secret)}).status_code == 401


def test_a_pending_sign_in_ends_when_the_password_changes(studio):
    application = studio()
    secret, _ = _turnOn(application)
    client, _ = _password(application, 'viewer')
    with application.state.database.open() as session:
        session.scalar(select(User).where(User.email == 'viewer@example.com')).passwordHash = hashPassword('another long password')
        session.commit()

    assert client.post('/api/auth/2fa/verify', json={'code': _fresh(secret)}).status_code == 401


def test_a_pending_sign_in_studio_did_not_sign_is_refused(studio):
    application = studio()
    secret, _ = _turnOn(application)
    client = _client(application)
    client.cookies.set(PENDING_COOKIE, 'eyJkIjp7InUiOjEsIm4iOiJjb2RlIn0sImUiOjk5OTk5OTk5OTl9.forged', path='/api/auth/2fa')

    assert client.post('/api/auth/2fa/verify', json={'code': _fresh(secret)}).status_code == 401


def test_the_pending_cookie_is_strict_short_lived_and_sent_only_to_two_factor(studio):
    application = studio()
    _turnOn(application)
    response = _client(application).post('/api/auth/login', json={'email': 'viewer@example.com', 'password': PASSWORD})

    cookie = response.headers['set-cookie']
    assert cookie.startswith(PENDING_COOKIE + '=') and 'HttpOnly' in cookie and 'SameSite=strict' in cookie
    assert 'Path=/api/auth/2fa' in cookie and 'Max-Age=300' in cookie and SESSION_COOKIE not in cookie


def test_wrong_codes_for_an_account_are_throttled(studio):
    application = studio()
    secret, _ = _turnOn(application)
    wrong = '000000' if _code(secret) != '000000' else '111111'
    statuses = []
    for _ in range(11):
        client, _ = _password(application, 'viewer')
        statuses.append(client.post('/api/auth/2fa/verify', json={'code': wrong}).status_code)

    assert statuses[:5] == [401] * 5 and statuses[-1] == 429
    client, _ = _password(application, 'viewer')
    assert client.post('/api/auth/2fa/verify', json={'code': _fresh(secret)}).status_code == 429


def test_a_secret_the_key_no_longer_decrypts_is_said_so(studio, settings):
    application = studio()
    secret, _ = _turnOn(application)
    application.state.database.close()
    settings.keyFile.unlink()
    restarted = createApp(settings)
    client, _ = _password(restarted, 'viewer')

    refused = client.post('/api/auth/2fa/verify', json={'code': _code(secret)})

    assert refused.status_code == 500 and 'reset two-factor sign-in' in refused.json()['detail']
    restarted.state.database.close()


# Required for a role ----------------------------------------------------------

def test_an_admin_sets_an_app_up_before_signing_in_when_admins_need_one(studio):
    application = studio(requireTwoFactor='admins')
    client, answer = _password(application, 'admin')
    assert answer == {'twoFactorStep': 'enrol'} and client.get('/api/auth/me').status_code == 401
    assert client.get('/api/users').status_code == 401

    details = client.post('/api/auth/2fa/setup').json()
    confirmed = client.post('/api/auth/2fa/confirm', json={'code': _code(details['secret'])})

    assert confirmed.status_code == 200 and client.get('/api/auth/me').json()['twoFactor'] is True
    assert _password(application, 'viewer')[1]['email'] == 'viewer@example.com'


def test_a_sign_in_waiting_for_a_code_cannot_set_up_another_app(studio):
    application = studio()
    _turnOn(application)
    client, _ = _password(application, 'viewer')

    assert client.post('/api/auth/2fa/setup').status_code == 401


def test_everyone_sets_an_app_up_when_all_need_one(studio):
    assert _password(studio(requireTwoFactor='all'), 'viewer')[1] == {'twoFactorStep': 'enrol'}


# Turning it off ---------------------------------------------------------------

def test_turning_it_off_takes_the_password_and_a_code(studio):
    application = studio()
    secret, codes = _turnOn(application)
    client, _ = _password(application, 'viewer')
    assert client.post('/api/auth/2fa/verify', json={'code': _fresh(secret)}).status_code == 200

    assert client.post('/api/auth/2fa/off', json={'password': 'wrong password!!', 'code': codes[0]}).status_code == 403
    assert client.post('/api/auth/2fa/off', json={'password': PASSWORD, 'code': codes[0]}).status_code == 200
    assert _password(application, 'viewer')[1]['email'] == 'viewer@example.com'


def test_a_role_that_needs_it_cannot_turn_it_off(studio):
    application = studio(requireTwoFactor='admins')
    client, _ = _password(application, 'admin')
    details = client.post('/api/auth/2fa/setup').json()
    assert client.post('/api/auth/2fa/confirm', json={'code': _code(details['secret'])}).status_code == 200

    assert client.post('/api/auth/2fa/off', json={'password': PASSWORD, 'code': _fresh(details['secret'])}).status_code == 409


def test_new_recovery_codes_replace_the_old(studio):
    application = studio()
    secret, old = _turnOn(application)
    client, _ = _password(application, 'viewer')
    assert client.post('/api/auth/2fa/verify', json={'code': _fresh(secret)}).status_code == 200

    new = client.post('/api/auth/2fa/recovery-codes', json={'code': old[0]}).json()['recoveryCodes']

    assert set(new).isdisjoint(old)
    again, _ = _password(application, 'viewer')
    assert again.post('/api/auth/2fa/verify', json={'code': old[1]}).status_code == 401
    assert again.post('/api/auth/2fa/verify', json={'code': new[0]}).status_code == 200


def test_an_admin_resets_it_for_someone_who_lost_their_app(studio):
    application = studio()
    secret, _ = _turnOn(application)
    viewer, _ = _password(application, 'viewer')
    assert viewer.post('/api/auth/2fa/verify', json={'code': _fresh(secret)}).status_code == 200
    admin, _ = _password(application, 'admin')

    reset = admin.delete('/api/users/{}/2fa'.format(_user(application, 'viewer@example.com').id))

    assert reset.status_code == 200 and reset.json()['twoFactor'] is False
    assert viewer.get('/api/auth/me').status_code == 401
    assert _password(application, 'viewer')[1]['email'] == 'viewer@example.com'
    assert ('reset two-factor sign-in', 'viewer@example.com', {}) in _actions(application)


def test_no_secret_or_code_reaches_the_activity_log_or_the_users_page(studio):
    application = studio()
    secret, codes = _turnOn(application)
    admin, _ = _password(application, 'admin')
    text = json.dumps(_actions(application)) + json.dumps(admin.get('/api/users').json())

    assert secret not in text and not any(code in text for code in codes) and 'gAAAA' not in text


# Settings ---------------------------------------------------------------------

def test_the_policy_must_be_one_studio_knows(settings):
    with pytest.raises(UsageError, match='BAUTA_STUDIO_REQUIRE_2FA'):
        dataclasses.replace(settings, requireTwoFactor='sometimes')


def test_admins_need_it_by_default_once_single_sign_on_is_on(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ('BAUTA_STUDIO_REQUIRE_2FA', 'BAUTA_STUDIO_OIDC_ISSUER'):
        monkeypatch.delenv(name, raising=False)
    assert Settings.fromEnvironment().requireTwoFactor == 'off'

    monkeypatch.setenv('BAUTA_STUDIO_OIDC_ISSUER', 'https://idp.example.com')
    monkeypatch.setenv('BAUTA_STUDIO_OIDC_CLIENT_ID', 'studio')
    monkeypatch.setenv('BAUTA_STUDIO_PUBLIC_URL', 'https://studio.example.com')
    assert Settings.fromEnvironment().requireTwoFactor == 'admins'
    assert isinstance(Settings.fromEnvironment().sso, SsoSettings)


def test_a_code_from_two_steps_ago_is_refused(studio, clock):
    application = studio()
    secret, _ = _turnOn(application)
    old = _fresh(secret)
    clock[0] += 90
    client, _ = _password(application, 'viewer')

    assert client.post('/api/auth/2fa/verify', json={'code': old}).status_code == 401
    assert client.post('/api/auth/2fa/verify', json={'code': _code(secret)}).status_code == 200


def test_a_pending_sign_in_expires_after_five_minutes(studio, clock):
    application = studio()
    secret, _ = _turnOn(application)
    client, _ = _password(application, 'viewer')
    clock[0] += 300

    assert client.post('/api/auth/2fa/verify', json={'code': _code(secret)}).status_code == 401

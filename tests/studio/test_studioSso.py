"""Single sign-on through OpenID Connect, against a stand-in identity provider
that checks what a real one would: the client's credentials, the redirect
URI, and the PKCE verifier against the challenge it was sent.
"""
import base64
import dataclasses
import hashlib
import json
import logging
import secrets
import time
import urllib.parse

import pytest

pytest.importorskip('fastapi', reason='bauta Studio needs its extra: pip install "bauta[studio]"')

import httpx  # noqa: E402
import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from bauta.project import UsageError  # noqa: E402
from bauta.studio.app import createApp  # noqa: E402
from bauta.studio.auth import SESSION_COOKIE, hashPassword  # noqa: E402
from bauta.studio.db import Activity, SsoIdentity, User  # noqa: E402
from bauta.studio.settings import Settings, SsoSettings  # noqa: E402
from bauta.studio import sso as ssoModule  # noqa: E402
from bauta.studio.routes.sso import CallbackQueryFilter  # noqa: E402
from bauta.studio.sso import PENDING_COOKIE, pendingCookie  # noqa: E402

from tests.studio.conftest import PASSWORD  # noqa: E402

ISSUER = 'https://idp.example.com'
CLIENT_ID = 'bauta-studio'
# A secret that HTTP Basic must form-encode before joining it to the ID.
CLIENT_SECRET = 'a:secret with/odd+chars'
PUBLIC_URL = 'https://studio.example.com'


def _newKey(keyId):
    """A new RSA signing key, and its public half as a JWK named `keyId`."""

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update({'kid': keyId, 'use': 'sig', 'alg': 'RS256'})
    return key, public


class Provider:
    """An identity provider: issues a code for whatever claims a test says
    someone has, and an ID token for the code.
    """

    def __init__(self):
        """A provider with one signing key published, no codes issued yet, and
        every request recorded.
        """

        self.key, self.jwk = _newKey('one')
        self.published = [self.jwk]
        self.codes = {}
        self.userinfo = {}
        self.discovery = {}
        self.down = False
        self.tokenOverrides = {}
        self.signWith = None
        self.requests = []

    def transport(self):
        """An HTTP transport that answers every request from this provider."""

        return httpx.MockTransport(self.handle)

    def handle(self, request):
        """Answers one request as an OpenID Connect provider would: discovery,
        keys, the token endpoint and userinfo; refusing every one while `down`.
        """

        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError('connection refused', request=request)
        path = request.url.path
        if path == '/.well-known/openid-configuration':
            return httpx.Response(200, json={'issuer': ISSUER, 'authorization_endpoint': ISSUER + '/authorize', 'token_endpoint': ISSUER + '/token',
                                             'jwks_uri': ISSUER + '/keys', 'userinfo_endpoint': ISSUER + '/userinfo',
                                             'id_token_signing_alg_values_supported': ['RS256'], **self.discovery})
        if path == '/keys':
            return httpx.Response(200, json={'keys': self.published})
        if path == '/token':
            return self.token(request)
        if path == '/userinfo':
            token = request.headers.get('Authorization', '').removeprefix('Bearer ')
            return httpx.Response(200, json=self.userinfo[token]) if token in self.userinfo else httpx.Response(401)
        return httpx.Response(404)

    def authorize(self, location, claims):
        """What the provider does once its user signs in: a code, for the
        callback, bound to what /authorize was sent.
        """

        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))
        assert location.startswith(ISSUER + '/authorize?')
        assert query['response_type'] == 'code' and query['client_id'] == CLIENT_ID and query['code_challenge_method'] == 'S256'
        assert query['redirect_uri'] == PUBLIC_URL + '/api/auth/sso/callback' and 'openid' in query['scope'].split()
        code = secrets.token_urlsafe(16)
        self.codes[code] = {'claims': claims, 'nonce': query['nonce'], 'challenge': query['code_challenge'], 'redirect': query['redirect_uri']}
        return code, query['state']

    def token(self, request):
        """The token endpoint: checks the client's credentials and the PKCE
        verifier, then answers with an ID token for the code's claims, signed
        with the key.
        """

        expected = 'Basic ' + base64.b64encode('{}:{}'.format(urllib.parse.quote(CLIENT_ID, safe=''),
                                                              urllib.parse.quote(CLIENT_SECRET, safe='')).encode()).decode()
        if request.headers.get('Authorization') != expected:
            return httpx.Response(401, json={'error': 'invalid_client'})
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        grant = self.codes.pop(form.get('code'), None)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(form.get('code_verifier', '').encode()).digest()).rstrip(b'=').decode()
        if grant is None or form.get('grant_type') != 'authorization_code' or form.get('redirect_uri') != grant['redirect'] \
                or challenge != grant['challenge']:
            return httpx.Response(400, json={'error': 'invalid_grant'})
        now = int(time.time())
        claims = {'iss': ISSUER, 'aud': CLIENT_ID, 'iat': now, 'exp': now + 300, 'nonce': grant['nonce'], **grant['claims'], **self.tokenOverrides}
        claims = {name: value for name, value in claims.items() if value is not None}
        accessToken = secrets.token_urlsafe(16)
        if 'userinfo' in grant['claims']:
            self.userinfo[accessToken] = {'sub': claims['sub'], **claims.pop('userinfo')}
        if self.signWith is not None:
            idToken = self.signWith(claims)
        else:
            idToken = jwt.encode(claims, self.key, algorithm='RS256', headers={'kid': self.jwk['kid']})
        return httpx.Response(200, json={'access_token': accessToken, 'token_type': 'Bearer', 'id_token': idToken})


@pytest.fixture
def provider():
    """A fake identity provider."""

    return Provider()


@pytest.fixture
def ssoApp(settings, provider):
    """A Studio signing in through `provider`, with SsoSettings as given, and
    an admin and a viewer whose accounts an admin made with their emails.
    """

    made = []

    def make(passwordLogin='all', **options):
        sso = SsoSettings(issuer=ISSUER, clientId=CLIENT_ID, clientSecret=CLIENT_SECRET, publicUrl=PUBLIC_URL, **options)
        application = createApp(dataclasses.replace(settings, sso=sso, passwordLogin=passwordLogin), ssoTransport=provider.transport())
        with application.state.database.open() as session:
            session.add(User(email='admin@example.com', role='admin', passwordHash=hashPassword(PASSWORD)))
            session.add(User(email='ann@example.com', role='viewer', passwordHash=None))
            session.commit()
        made.append(application)
        return application

    yield make
    for application in made:
        application.state.database.close()


def _person(sub='ann-at-idp', email='ann@example.com', **claims):
    """The claims of a person with a verified email, with `claims` added."""

    return {'sub': sub, 'email': email, 'email_verified': True, 'name': 'Ann', **claims}


def _signIn(application, provider, claims, client=None):
    """Goes through the whole sign-in: start, the provider, the callback."""

    client = client or TestClient(application, headers={'X-Bauta-Studio': '1'})
    start = client.get('/api/auth/sso/start', follow_redirects=False)
    assert start.status_code == 303, start.text
    code, state = provider.authorize(start.headers['location'], claims)
    landing = client.get('/api/auth/sso/callback', params={'code': code, 'state': state}, follow_redirects=False)
    return client, landing


def _failure(landing):
    """The error code a refused sign-in redirects to the sign-in page with."""

    assert landing.status_code == 303 and landing.headers['location'].startswith('/'), landing.text
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(landing.headers['location']).query)).get('sso_error')


def _user(application, email):
    """The account with `email`, or None."""

    with application.state.database.open() as session:
        return session.scalar(select(User).where(User.email == email))


def _actions(application):
    """Every activity log entry, as (action, target, detail), oldest first."""

    with application.state.database.open() as session:
        return [(entry.action, entry.target, entry.detail) for entry in session.scalars(select(Activity).order_by(Activity.id))]


# The flow ---------------------------------------------------------------------

def test_a_person_an_admin_added_signs_in_and_is_linked_by_the_providers_subject(ssoApp, provider):
    application = ssoApp()
    client, landing = _signIn(application, provider, _person())

    assert _failure(landing) is None
    me = client.get('/api/auth/me').json()
    assert me['email'] == 'ann@example.com' and me['sso'] is True and me['passwordSet'] is False
    with application.state.database.open() as session:
        assert session.get(SsoIdentity, SsoIdentity.key(ISSUER, 'ann-at-idp')).user.email == 'ann@example.com'
    assert ('signed in', '', {'method': 'sso'}) in _actions(application)


def test_the_callback_sets_a_strict_session_and_moves_on_to_the_app(ssoApp, provider):
    _, landing = _signIn(ssoApp(), provider, _person())

    assert landing.headers['location'] == '/' and landing.headers['cache-control'] == 'no-store'
    session = [header for header in landing.headers.get_list('set-cookie') if header.startswith(SESSION_COOKIE + '=')][0]
    assert 'HttpOnly' in session and 'SameSite=strict' in session


def test_the_authorization_code_is_kept_out_of_the_access_log():
    record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0, '%s - "%s %s HTTP/%s" %d', None, None)
    record.args = ('127.0.0.1:1', 'GET', '/api/auth/sso/callback?state=s&code=secret-code', '1.1', 303)

    CallbackQueryFilter().filter(record)

    assert 'secret-code' not in record.getMessage() and '/api/auth/sso/callback' in record.getMessage()


def test_the_pending_cookie_is_lax_short_lived_and_sent_only_to_the_callback(ssoApp):
    start = TestClient(ssoApp()).get('/api/auth/sso/start', follow_redirects=False)

    cookie = start.headers['set-cookie']
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(start.headers['location']).query))['state']
    assert cookie.startswith(pendingCookie(state) + '=') and 'HttpOnly' in cookie and 'SameSite=lax' in cookie and 'Path=/api/auth/sso' in cookie
    assert 'Max-Age=600' in cookie


def test_the_pending_cookie_is_forgotten_once_the_provider_answers(ssoApp, provider):
    client, _ = _signIn(ssoApp(), provider, _person())

    assert not any(name.startswith(PENDING_COOKIE) for name in client.cookies)


def test_a_sign_in_is_offered_on_the_signed_out_page(ssoApp):
    detail = TestClient(ssoApp(label='Sign in with Okta')).get('/api/auth/me').json()['detail']

    assert detail['sso'] == {'label': 'Sign in with Okta'} and detail['passwordLogin'] == 'all'


def test_single_sign_on_is_not_there_when_it_is_off(app):
    assert TestClient(app).get('/api/auth/sso/start', follow_redirects=False).status_code == 404


# What the callback refuses ----------------------------------------------------

def test_a_callback_without_the_state_it_left_with_is_refused(ssoApp, provider):
    """Someone else's code, sent to this browser, would sign it in as them."""

    application = ssoApp()
    attacker = TestClient(application)
    start = attacker.get('/api/auth/sso/start', follow_redirects=False)
    code, state = provider.authorize(start.headers['location'], _person(sub='mallory', email='admin@example.com'))

    victim = TestClient(application)
    landing = victim.get('/api/auth/sso/callback', params={'code': code, 'state': state}, follow_redirects=False)

    assert _failure(landing) == 'state' and SESSION_COOKIE not in landing.headers.get('set-cookie', '')


def test_a_state_that_differs_from_the_cookies_is_refused(ssoApp, provider):
    application = ssoApp()
    client = TestClient(application)
    start = client.get('/api/auth/sso/start', follow_redirects=False)
    code, _ = provider.authorize(start.headers['location'], _person())

    assert _failure(client.get('/api/auth/sso/callback', params={'code': code, 'state': 'guessed'}, follow_redirects=False)) == 'state'


def test_a_pending_cookie_studio_did_not_sign_is_refused(ssoApp, provider):
    application = ssoApp()
    client = TestClient(application)
    start = client.get('/api/auth/sso/start', follow_redirects=False)
    code, state = provider.authorize(start.headers['location'], _person())
    payload = base64.urlsafe_b64encode(json.dumps({'s': state, 'n': 'n', 'v': 'v', 'e': 2 ** 40}).encode()).rstrip(b'=').decode()
    # Without the genuine cookie: left in the jar beside the forged one, under
    # the server's domain, which of the two is sent first depends on the
    # versions installed.
    client.cookies.delete(pendingCookie(state))
    client.cookies.set(pendingCookie(state), payload + '.forged', path='/api/auth/sso')

    assert _failure(client.get('/api/auth/sso/callback', params={'code': code, 'state': state}, follow_redirects=False)) == 'state'


def test_the_providers_refusal_is_shown_as_such(ssoApp):
    application = ssoApp()
    client = TestClient(application)
    start = client.get('/api/auth/sso/start', follow_redirects=False)
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(start.headers['location']).query))['state']

    landing = client.get('/api/auth/sso/callback', params={'error': 'access_denied', 'state': state}, follow_redirects=False)

    assert _failure(landing) == 'provider'


def test_a_token_signed_with_a_key_the_provider_does_not_publish_is_refused(ssoApp, provider):
    stranger, _ = _newKey('one')
    provider.signWith = lambda claims: jwt.encode(claims, stranger, algorithm='RS256', headers={'kid': 'one'})

    assert _failure(_signIn(ssoApp(), provider, _person())[1]) == 'token'


@pytest.mark.parametrize('algorithm', ['HS256', 'none'])
def test_a_token_signed_with_the_client_secret_or_not_at_all_is_refused(ssoApp, provider, algorithm):
    provider.discovery = {'id_token_signing_alg_values_supported': ['RS256', 'HS256', 'none']}
    provider.signWith = lambda claims: jwt.encode(claims, CLIENT_SECRET if algorithm == 'HS256' else None, algorithm=algorithm)

    assert _failure(_signIn(ssoApp(), provider, _person())[1]) == 'token'


@pytest.mark.parametrize('override', [{'aud': 'another-client'}, {'iss': 'https://evil.example.com'}, {'exp': 1}, {'nonce': 'replayed'},
                                      {'aud': [CLIENT_ID, 'another-client']}, {'azp': 'another-client'}, {'sub': None}])
def test_a_token_for_another_client_issuer_time_or_sign_in_is_refused(ssoApp, provider, override):
    provider.tokenOverrides = override

    application = ssoApp()
    assert _failure(_signIn(application, provider, _person())[1]) == 'token'
    assert _user(application, 'ann@example.com').lastLoginAt is None


def test_a_token_naming_a_new_key_has_the_keys_read_again(ssoApp, provider, monkeypatch):
    """How a provider rotating its keys is followed."""

    monkeypatch.setattr(ssoModule, 'KEYS_REFRESH_SECONDS', 0)
    application = ssoApp()
    _signIn(application, provider, _person())
    provider.key, provider.jwk = _newKey('two')
    provider.published = [provider.jwk]

    assert _failure(_signIn(application, provider, _person())[1]) is None


def test_tokens_naming_made_up_keys_have_the_keys_read_only_so_often(ssoApp, provider):
    application = ssoApp()
    _signIn(application, provider, _person())
    stranger, _ = _newKey('made-up')
    provider.signWith = lambda claims: jwt.encode(claims, stranger, algorithm='RS256', headers={'kid': 'made-up'})
    for _ in range(3):
        assert _failure(_signIn(application, provider, _person())[1]) == 'token'

    assert sum(request.url.path == '/keys' for request in provider.requests) == 1


def test_a_key_the_provider_withdraws_is_trusted_no_longer_than_an_hour(ssoApp, provider):
    clock = [time.time()]
    application = ssoApp()
    application.state.oidc._clock = lambda: clock[0]
    _signIn(application, provider, _person())
    _, provider.published = _newKey('two')
    provider.published = [provider.published]
    clock[0] += ssoModule.DISCOVERY_SECONDS - 1
    assert _failure(_signIn(application, provider, _person())[1]) is None

    clock[0] += 1
    assert _failure(_signIn(application, provider, _person())[1]) == 'token'


def test_a_provider_that_cannot_be_reached_is_said_so(ssoApp, provider):
    provider.down = True
    landing = TestClient(ssoApp()).get('/api/auth/sso/start', follow_redirects=False)

    assert _failure(landing) == 'unreachable'


def test_a_provider_naming_another_issuer_is_refused(ssoApp, provider):
    provider.discovery = {'issuer': 'https://idp.example.com/other'}

    assert _failure(TestClient(ssoApp()).get('/api/auth/sso/start', follow_redirects=False)) == 'unreachable'


# Who it signs in --------------------------------------------------------------

def test_an_email_the_provider_does_not_vouch_for_links_nothing(ssoApp, provider):
    """A provider that lets people set their own email would otherwise let
    anyone take over the account with it."""

    application = ssoApp()
    landing = _signIn(application, provider, _person(email='admin@example.com', email_verified=False))[1]

    assert _failure(landing) == 'email' and _user(application, 'admin@example.com').ssoIdentity is None
    assert ('refused a single sign-on', 'admin@example.com', {'reason': 'email'}) in _actions(application)


def test_a_provider_trusted_with_emails_links_without_saying_they_are_verified(ssoApp, provider):
    """As Entra ID, which sends no email_verified."""

    assert _failure(_signIn(ssoApp(trustEmail=True), provider, _person(email_verified=None))[1]) is None


def test_someone_with_no_account_is_refused_unless_accounts_are_made(ssoApp, provider):
    application = ssoApp()

    assert _failure(_signIn(application, provider, _person(sub='bob', email='bob@example.com'))[1]) == 'no_account'
    assert _user(application, 'bob@example.com') is None


def test_an_account_is_made_at_the_first_sign_in_only_for_the_allowed_domains(ssoApp, provider):
    application = ssoApp(autoCreate=True, allowedDomains=('example.com',))

    assert _failure(_signIn(application, provider, _person(sub='bob', email='bob@example.com'))[1]) is None
    assert _user(application, 'bob@example.com').role == 'viewer'
    assert _failure(_signIn(application, provider, _person(sub='eve', email='eve@elsewhere.com'))[1]) == 'no_account'
    # A domain only ends the email: one of its subdomains is another domain.
    assert _failure(_signIn(application, provider, _person(sub='sub', email='eve@evil.example.com'))[1]) == 'no_account'


def test_a_linked_identity_signs_in_after_its_email_changes_and_the_email_follows(ssoApp, provider):
    application = ssoApp()
    _signIn(application, provider, _person())

    client, landing = _signIn(application, provider, _person(email='ann.smith@example.com'))

    assert _failure(landing) is None and client.get('/api/auth/me').json()['email'] == 'ann.smith@example.com'


def test_another_identity_with_a_linked_accounts_email_is_refused(ssoApp, provider):
    application = ssoApp()
    _signIn(application, provider, _person())

    assert _failure(_signIn(application, provider, _person(sub='someone-else'))[1]) == 'linked'


def test_a_deactivated_account_cannot_sign_in_through_the_provider(ssoApp, provider):
    application = ssoApp()
    _signIn(application, provider, _person())
    admin = TestClient(application, headers={'X-Bauta-Studio': '1'})
    admin.post('/api/auth/login', json={'email': 'admin@example.com', 'password': PASSWORD})
    admin.patch('/api/users/{}'.format(_user(application, 'ann@example.com').id), json={'active': False})

    assert _failure(_signIn(application, provider, _person())[1]) == 'disabled'


def test_an_admin_unlinks_an_identity_so_a_new_one_can_link(ssoApp, provider):
    application = ssoApp()
    _signIn(application, provider, _person())
    admin = TestClient(application, headers={'X-Bauta-Studio': '1'})
    admin.post('/api/auth/login', json={'email': 'admin@example.com', 'password': PASSWORD})

    unlinked = admin.delete('/api/users/{}/sso'.format(_user(application, 'ann@example.com').id))

    assert unlinked.status_code == 200 and unlinked.json()['sso'] is False
    assert _failure(_signIn(application, provider, _person(sub='ann-made-again'))[1]) is None


def test_an_sso_session_lasts_the_sso_sessions_hours(ssoApp, provider):
    _, landing = _signIn(ssoApp(sessionHours=2), provider, _person())

    assert 'Max-Age=7200' in [header for header in landing.headers.get_list('set-cookie') if header.startswith(SESSION_COOKIE)][0]


# Roles from the provider ------------------------------------------------------

def test_the_providers_groups_set_the_role_at_every_sign_in(ssoApp, provider):
    application = ssoApp(roles='provider', adminGroups=('studio-admins',), viewerGroups=('everyone',))

    client, _ = _signIn(application, provider, _person(groups=['everyone', 'studio-admins']))
    assert client.get('/api/auth/me').json()['role'] == 'admin'

    client, _ = _signIn(application, provider, _person(groups=['everyone']))
    assert client.get('/api/auth/me').json()['role'] == 'viewer'
    assert ('role set by the identity provider', 'ann@example.com', {'role': 'viewer', 'was': 'admin'}) in _actions(application)


def test_someone_none_of_whose_groups_gives_a_role_is_refused(ssoApp, provider):
    application = ssoApp(roles='provider', adminGroups=('studio-admins',))

    assert _failure(_signIn(application, provider, _person(groups=['sales']))[1]) == 'no_role'


def test_groups_the_id_token_leaves_out_are_read_from_userinfo(ssoApp, provider):
    application = ssoApp(roles='provider', operatorGroups=('ops',))

    client, landing = _signIn(application, provider, _person(userinfo={'groups': ['ops'], 'iss': 'https://evil.example.com'}))

    assert _failure(landing) is None and client.get('/api/auth/me').json()['role'] == 'operator'
    with application.state.database.open() as session:
        assert session.get(SsoIdentity, SsoIdentity.key(ISSUER, 'ann-at-idp')) is not None


def test_a_userinfo_answer_for_another_subject_is_refused(ssoApp, provider):
    application = ssoApp(roles='provider', operatorGroups=('ops',))

    assert _failure(_signIn(application, provider, _person(userinfo={'groups': ['ops'], 'sub': 'someone-else'}))[1]) == 'token'


def test_a_nested_role_claim_is_read(ssoApp, provider):
    """As Keycloak puts a realm's roles."""

    application = ssoApp(roles='provider', roleClaim='realm_access.roles', adminGroups=('admin',))

    client, _ = _signIn(application, provider, _person(realm_access={'roles': ['admin']}))

    assert client.get('/api/auth/me').json()['role'] == 'admin'


def test_the_users_page_cannot_change_a_role_the_provider_sets(ssoApp, provider):
    application = ssoApp(roles='provider', viewerGroups=('everyone',))
    _signIn(application, provider, _person(groups=['everyone']))
    admin = TestClient(application, headers={'X-Bauta-Studio': '1'})
    admin.post('/api/auth/login', json={'email': 'admin@example.com', 'password': PASSWORD})

    changed = admin.patch('/api/users/{}'.format(_user(application, 'ann@example.com').id), json={'role': 'admin'})

    assert changed.status_code == 409 and admin.get('/api/users').json()['sso'] == {'roles': 'provider'}
    # The break-glass admin, who has no identity at the provider, stays Studio's to change.
    assert admin.patch('/api/users/{}'.format(_user(application, 'admin@example.com').id), json={'name': 'Break glass'}).status_code == 200


# Passwords alongside ----------------------------------------------------------

def test_an_account_without_a_password_cannot_sign_in_with_one(ssoApp):
    client = TestClient(ssoApp(), headers={'X-Bauta-Studio': '1'})

    assert client.post('/api/auth/login', json={'email': 'ann@example.com', 'password': PASSWORD}).status_code == 401


def test_with_passwords_for_admins_only_the_break_glass_admin_signs_in_with_one(ssoApp):
    application = ssoApp(passwordLogin='admins')
    with application.state.database.open() as session:
        session.scalar(select(User).where(User.email == 'ann@example.com')).passwordHash = hashPassword(PASSWORD)
        session.commit()
    client = TestClient(application, headers={'X-Bauta-Studio': '1'})

    refused = client.post('/api/auth/login', json={'email': 'ann@example.com', 'password': PASSWORD})
    assert refused.status_code == 403 and 'single sign-on' in refused.json()['detail']
    assert client.post('/api/auth/login', json={'email': 'admin@example.com', 'password': PASSWORD}).status_code == 200


def test_with_passwords_off_no_one_signs_in_with_one(ssoApp):
    client = TestClient(ssoApp(passwordLogin='off'), headers={'X-Bauta-Studio': '1'})

    assert client.post('/api/auth/login', json={'email': 'admin@example.com', 'password': PASSWORD}).status_code == 403


def test_an_admin_adds_someone_who_will_sign_in_only_through_the_provider(ssoApp, provider):
    application = ssoApp()
    admin = TestClient(application, headers={'X-Bauta-Studio': '1'})
    admin.post('/api/auth/login', json={'email': 'admin@example.com', 'password': PASSWORD})

    added = admin.post('/api/users', json={'email': 'carl@example.com', 'role': 'operator'})

    assert added.status_code == 200 and added.json()['passwordSet'] is False
    assert _failure(_signIn(application, provider, _person(sub='carl', email='carl@example.com'))[1]) is None


def test_without_single_sign_on_a_new_account_needs_a_password(signIn):
    added = signIn('admin').post('/api/users', json={'email': 'carl@example.com', 'role': 'operator'})

    assert added.status_code == 400 and 'password' in added.json()['detail']


# Settings ---------------------------------------------------------------------

BASE = {'BAUTA_STUDIO_OIDC_ISSUER': ISSUER, 'BAUTA_STUDIO_OIDC_CLIENT_ID': CLIENT_ID, 'BAUTA_STUDIO_PUBLIC_URL': PUBLIC_URL}


def test_single_sign_on_is_off_without_an_issuer():
    assert SsoSettings.fromEnvironment({}) is None


def test_the_settings_read_from_the_environment(tmp_path):
    secret = tmp_path / 'secret'
    secret.write_text(CLIENT_SECRET + '\n')

    sso = SsoSettings.fromEnvironment({**BASE, 'BAUTA_STUDIO_OIDC_CLIENT_SECRET_FILE': str(secret), 'BAUTA_STUDIO_PUBLIC_URL': PUBLIC_URL + '/',
                                       'BAUTA_STUDIO_OIDC_ROLES': 'provider', 'BAUTA_STUDIO_OIDC_ADMIN_GROUPS': 'a, b',
                                       'BAUTA_STUDIO_OIDC_AUTO_CREATE': '1', 'BAUTA_STUDIO_OIDC_ALLOWED_DOMAINS': '@Example.com'})

    assert sso.clientSecret == CLIENT_SECRET and sso.redirectUri == PUBLIC_URL + '/api/auth/sso/callback'
    assert sso.adminGroups == ('a', 'b') and sso.allowedDomains == ('example.com',) and sso.autoCreate


@pytest.mark.parametrize('environment, says', [
    ({'BAUTA_STUDIO_OIDC_CLIENT_ID': CLIENT_ID}, 'ISSUER is not'),
    ({**BASE, 'BAUTA_STUDIO_OIDC_ISSUER': 'http://idp.example.com'}, 'https'),
    ({**BASE, 'BAUTA_STUDIO_OIDC_CLIENT_ID': ''}, 'CLIENT_ID'),
    ({**BASE, 'BAUTA_STUDIO_PUBLIC_URL': ''}, 'PUBLIC_URL'),
    ({**BASE, 'BAUTA_STUDIO_PUBLIC_URL': 'http://studio.example.com'}, 'https'),
    ({**BASE, 'BAUTA_STUDIO_PUBLIC_URL': PUBLIC_URL + '/studio'}, 'no path'),
    ({**BASE, 'BAUTA_STUDIO_OIDC_AUTO_CREATE': '1'}, 'ALLOWED_DOMAINS'),
    ({**BASE, 'BAUTA_STUDIO_OIDC_ROLES': 'provider'}, '_GROUPS'),
    ({**BASE, 'BAUTA_STUDIO_OIDC_ROLES': 'groups'}, 'one of studio, provider'),
    ({**BASE, 'BAUTA_STUDIO_OIDC_SCOPES': 'email'}, 'openid'),
    ({**BASE, 'BAUTA_STUDIO_OIDC_SESSION_HOURS': '0'}, 'SESSION_HOURS'),
    ({**BASE, 'BAUTA_STUDIO_OIDC_CLIENT_SECRET': 'x', 'BAUTA_STUDIO_OIDC_CLIENT_SECRET_FILE': '/x'}, 'not both'),
])
def test_a_setting_missing_or_wrong_stops_studio_starting(environment, says):
    with pytest.raises(UsageError, match=says):
        SsoSettings.fromEnvironment(environment)


def test_a_local_provider_may_be_plain_http_for_development():
    assert SsoSettings.fromEnvironment({**BASE, 'BAUTA_STUDIO_OIDC_ISSUER': 'http://localhost:8080/realms/dev',
                                        'BAUTA_STUDIO_PUBLIC_URL': 'http://127.0.0.1:8710'}) is not None


def test_passwords_cannot_be_turned_off_without_single_sign_on(settings):
    with pytest.raises(UsageError, match='needs single sign-on'):
        dataclasses.replace(settings, passwordLogin='admins')
    with pytest.raises(UsageError, match='must be one of'):
        dataclasses.replace(settings, passwordLogin='never', sso=SsoSettings.fromEnvironment(BASE))


def test_no_token_secret_or_code_reaches_the_activity_log_or_studios_answers(ssoApp, provider):
    application = ssoApp()
    client, landing = _signIn(application, provider, _person())
    text = json.dumps(_actions(application)) + landing.headers['location'] + json.dumps(client.get('/api/auth/me').json())

    assert CLIENT_SECRET not in text and 'eyJ' not in text


# What the review found -------------------------------------------------------

def _admin(application):
    """A client signed in as the admin with a password."""

    admin = TestClient(application, headers={'X-Bauta-Studio': '1'})
    assert admin.post('/api/auth/login', json={'email': 'admin@example.com', 'password': PASSWORD}).status_code == 200
    return admin


def test_two_sign_ins_begun_in_two_tabs_both_finish(ssoApp, provider):
    application = ssoApp()
    client = TestClient(application)
    first = client.get('/api/auth/sso/start', follow_redirects=False)
    second = client.get('/api/auth/sso/start', follow_redirects=False)
    firstCode, firstState = provider.authorize(first.headers['location'], _person())
    provider.authorize(second.headers['location'], _person())

    assert _failure(client.get('/api/auth/sso/callback', params={'code': firstCode, 'state': firstState}, follow_redirects=False)) is None


def test_a_sign_in_brought_back_twice_is_refused_the_second_time(ssoApp, provider):
    application = ssoApp()
    client = TestClient(application)
    start = client.get('/api/auth/sso/start', follow_redirects=False)
    code, state = provider.authorize(start.headers['location'], _person())
    cookie = client.cookies.get(pendingCookie(state))
    assert _failure(client.get('/api/auth/sso/callback', params={'code': code, 'state': state}, follow_redirects=False)) is None

    replay = TestClient(application)
    replay.cookies.set(pendingCookie(state), cookie, path='/api/auth/sso')
    again, _ = provider.authorize(start.headers['location'], _person())

    assert _failure(replay.get('/api/auth/sso/callback', params={'code': again, 'state': state}, follow_redirects=False)) == 'state'


def test_failed_callbacks_from_one_address_are_throttled_before_reaching_the_provider(ssoApp, provider):
    application = ssoApp()
    client = TestClient(application)
    for _ in range(30):
        start = client.get('/api/auth/sso/start', follow_redirects=False)
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(start.headers['location']).query))['state']
        client.get('/api/auth/sso/callback', params={'code': 'junk', 'state': state}, follow_redirects=False)
    tokenRequests = sum(request.url.path == '/token' for request in provider.requests)

    _, landing = _signIn(application, provider, _person(), client)

    assert _failure(landing) == 'busy' and sum(request.url.path == '/token' for request in provider.requests) == tokenRequests


def test_a_provider_that_is_down_is_asked_once_in_a_while_not_at_every_sign_in(ssoApp, provider):
    provider.down = True
    client = TestClient(ssoApp())
    for _ in range(5):
        assert _failure(client.get('/api/auth/sso/start', follow_redirects=False)) == 'unreachable'

    assert len(provider.requests) == 1


def test_a_key_of_another_type_than_the_tokens_algorithm_is_a_token_failure(ssoApp, provider):
    from cryptography.hazmat.primitives.asymmetric import ec
    provider.discovery = {'id_token_signing_alg_values_supported': ['RS256', 'ES256']}
    stranger = ec.generate_private_key(ec.SECP256R1())
    provider.signWith = lambda claims: jwt.encode(claims, stranger, algorithm='ES256', headers={'kid': 'one'})

    assert _failure(_signIn(ssoApp(), provider, _person())[1]) == 'token'


def test_the_break_glass_admin_is_not_linked_when_only_admins_have_passwords(ssoApp, provider):
    application = ssoApp(passwordLogin='admins', roles='provider', viewerGroups=('everyone',))

    landing = _signIn(application, provider, _person(sub='admin-at-idp', email='admin@example.com', groups=['everyone']))[1]

    assert _failure(landing) == 'break_glass'
    admin = _user(application, 'admin@example.com')
    assert admin.role == 'admin' and admin.ssoIdentity is None and admin.passwordHash is not None


def test_the_provider_cannot_demote_the_last_admin(ssoApp, provider):
    application = ssoApp(roles='provider', adminGroups=('admins',), viewerGroups=('everyone',))
    with application.state.database.open() as session:
        session.scalar(select(User).where(User.email == 'admin@example.com')).active = False
        session.commit()
    _signIn(application, provider, _person(groups=['admins']))

    assert _failure(_signIn(application, provider, _person(groups=['everyone']))[1]) == 'last_admin'
    assert _user(application, 'ann@example.com').role == 'admin'


def test_linking_drops_a_password_the_policy_would_not_let_the_account_use(ssoApp, provider):
    application = ssoApp(passwordLogin='admins')
    with application.state.database.open() as session:
        session.scalar(select(User).where(User.email == 'ann@example.com')).passwordHash = hashPassword(PASSWORD)
        session.commit()

    assert _failure(_signIn(application, provider, _person())[1]) is None
    assert _user(application, 'ann@example.com').passwordHash is None


def test_with_passwords_for_all_linking_keeps_the_password_and_an_admin_can_remove_it(ssoApp, provider):
    application = ssoApp()
    with application.state.database.open() as session:
        session.scalar(select(User).where(User.email == 'ann@example.com')).passwordHash = hashPassword(PASSWORD)
        session.commit()
    _signIn(application, provider, _person())
    assert _user(application, 'ann@example.com').passwordHash is not None
    admin = _admin(application)

    removed = admin.patch('/api/users/{}'.format(_user(application, 'ann@example.com').id), json={'removePassword': True})

    assert removed.status_code == 200 and removed.json()['passwordSet'] is False
    assert admin.patch('/api/users/{}'.format(_user(application, 'admin@example.com').id), json={'removePassword': True}).status_code == 409


def test_a_new_issuer_links_the_account_anew(ssoApp, provider, settings):
    application = ssoApp()
    with application.state.database.open() as session:
        user = session.scalar(select(User).where(User.email == 'ann@example.com'))
        user.ssoIdentity = SsoIdentity.of('https://old-idp.example.com', 'ann-old')
        session.commit()

    assert _failure(_signIn(application, provider, _person())[1]) is None
    with application.state.database.open() as session:
        assert session.get(SsoIdentity, SsoIdentity.key(ISSUER, 'ann-at-idp')) is not None and session.get(SsoIdentity, SsoIdentity.key('https://old-idp.example.com', 'ann-old')) is None


def test_a_google_account_outside_the_workspace_is_refused(ssoApp, provider):
    application = ssoApp(hostedDomains=('example.com',))

    assert _failure(_signIn(application, provider, _person())[1]) == 'domain'
    assert _failure(_signIn(application, provider, _person(hd='example.com'))[1]) is None


@pytest.mark.parametrize('vouching', [{'email_verified': 'true'}, {'email_verified': None, 'xms_edov': True}])
def test_a_provider_vouching_for_an_email_its_own_way_is_taken(ssoApp, provider, vouching):
    assert _failure(_signIn(ssoApp(), provider, _person(**vouching))[1]) is None


def test_an_email_follows_from_the_provider_only_if_it_vouches_for_it(ssoApp, provider):
    application = ssoApp(trustEmail=True)
    _signIn(application, provider, _person())

    client, _ = _signIn(application, provider, _person(email='ceo@example.com', email_verified=False))

    assert client.get('/api/auth/me').json()['email'] == 'ann@example.com'


def test_a_person_made_at_their_first_sign_in_is_recorded_as_who_signed_in(ssoApp, provider):
    application = ssoApp(autoCreate=True, allowedDomains=('example.com',))
    _signIn(application, provider, _person(sub='bob', email='bob@example.com'))

    with application.state.database.open() as session:
        entry = session.scalar(select(Activity).where(Activity.action == 'signed in'))
        assert entry.userId == session.scalar(select(User.id).where(User.email == 'bob@example.com'))


def test_someone_with_no_role_is_made_no_account(ssoApp, provider):
    application = ssoApp(autoCreate=True, allowedDomains=('example.com',), roles='provider', adminGroups=('admins',))

    assert _failure(_signIn(application, provider, _person(sub='bob', email='bob@example.com', groups=[]))[1]) == 'no_role'
    assert _user(application, 'bob@example.com') is None


def test_signing_in_again_ends_the_browsers_previous_session(ssoApp, provider):
    application = ssoApp()
    client, _ = _signIn(application, provider, _person())
    old = client.cookies.get(SESSION_COOKIE)

    _signIn(application, provider, _person(), client)

    stale = TestClient(application)
    stale.cookies.set(SESSION_COOKIE, old)
    assert stale.get('/api/auth/me').status_code == 401 and client.get('/api/auth/me').status_code == 200


def test_unlinking_answers_for_who_is_not_there_or_not_linked_and_only_to_admins(ssoApp, provider):
    application = ssoApp()
    admin = _admin(application)
    client, _ = _signIn(application, provider, _person())

    assert admin.delete('/api/users/999/sso').status_code == 404
    assert admin.delete('/api/users/{}/sso'.format(_user(application, 'admin@example.com').id)).status_code == 409
    assert client.delete('/api/users/{}/sso'.format(_user(application, 'ann@example.com').id), headers={'X-Bauta-Studio': '1'}).status_code == 403


def test_a_password_sessions_hours_are_a_setting_too(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('BAUTA_STUDIO_OIDC_ISSUER', raising=False)
    monkeypatch.setenv('BAUTA_STUDIO_SESSION_HOURS', '4')
    assert Settings.fromEnvironment().sessionHours == 4

    monkeypatch.setenv('BAUTA_STUDIO_SESSION_HOURS', 'a day')
    with pytest.raises(UsageError, match='BAUTA_STUDIO_SESSION_HOURS'):
        Settings.fromEnvironment()


def test_subjects_differing_only_in_case_are_two_people_and_kept_as_digests(ssoApp, provider):
    """MySQL's and SQL Server's usual collations would take them as one."""

    application = ssoApp()
    _signIn(application, provider, _person(sub='00uAbC'))

    assert _failure(_signIn(application, provider, _person(sub='00uabc'))[1]) == 'linked'
    with application.state.database.open() as session:
        assert [identity.subjectHash for identity in session.scalars(select(SsoIdentity))] == [SsoIdentity.key(ISSUER, '00uAbC')[1]]

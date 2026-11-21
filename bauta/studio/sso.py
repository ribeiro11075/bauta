"""Single sign-on through OpenID Connect: the authorization code flow, with
PKCE, as a confidential client, as the OAuth 2.0 security best current
practice (RFC 9700) has it.

Signing in starts at /api/auth/sso/start, which redirects the browser to the
identity provider with a fresh state, nonce and PKCE challenge, and keeps the
three in a cookie only Studio can have signed. The provider redirects back to
/api/auth/sso/callback with a code. The callback checks the state against the
cookie, which is what stops someone else's sign-in being completed in this
browser, exchanges the code for an ID token, and verifies the token: signed by
one of the provider's published keys with an asymmetric algorithm, issued by
the configured issuer, for this client, unexpired, and carrying the nonce.

The person is then matched to an account by the issuer and subject the token
names, never by email once linked: see signInWith.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, NamedTuple, Optional

import httpx
import jwt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..project import UsageError
from .auth import record
from .db import ADMIN, VIEWER, SsoIdentity, User
from .sealing import b64, seal, unseal
from .settings import PasswordLogin, RoleSource, Settings, SsoSettings, httpsUrl

# The cookie holding a sign-in on its way through the identity provider, sent
# only to the callback. SameSite=Lax, where the session's is Strict: the
# provider sends the browser back with a cross-site navigation, on which a
# browser sends no Strict cookie. One per sign-in, named by the start of its
# state, so a sign-in begun in a second tab doesn't undo the first's.
PENDING_COOKIE = 'bauta_studio_sso_'
PENDING_COOKIE_STATE = 16
PENDING_PATH = '/api/auth/sso'
# How long someone has at the identity provider before starting again.
PENDING_SECONDS = 600
# How long the provider's discovery document and keys are kept before they
# are read again: a key the provider withdraws is trusted no longer than this.
DISCOVERY_SECONDS = 3600
# How long a failure to read them is remembered: a provider that is down or
# slow is asked once in this time, not on every sign-in started meanwhile.
FAILURE_SECONDS = 30
# The provider's keys are read again for a key they don't hold, which is how a
# provider rotating its keys is followed; no more often than this, so tokens
# naming made-up keys can't have Studio read them on every request.
KEYS_REFRESH_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 10.0
# How far apart Studio's clock and the provider's may be.
CLOCK_LEEWAY_SECONDS = 60
# The algorithms an ID token may be signed with: asymmetric ones only. 'none'
# signs nothing, and an HMAC one would take the client secret, which the
# provider also holds, as its key.
SIGNING_ALGORITHMS = ('RS256', 'RS384', 'RS512', 'PS256', 'PS384', 'PS512', 'ES256', 'ES384', 'ES512', 'EdDSA')
# The most of what the provider says of a refusal that goes to the log.
PROVIDER_TEXT_LENGTH = 300
# The longest issuer an identity can be kept with.
IDENTITY_LENGTH = 255
# What a provider says to vouch for an email: OpenID Connect's email_verified,
# which some send as a string, or Entra ID's xms_edov, that the email's domain
# is one the tenant has verified.
VERIFIED = (True, 'true', 1, '1')

# What the sign-in page is told went wrong, by code; what happened in full is
# in Studio's log.
STATE = 'state'
PROVIDER = 'provider'
UNREACHABLE = 'unreachable'
TOKEN = 'token'
EMAIL = 'email'
NO_ACCOUNT = 'no_account'
DISABLED = 'disabled'
NO_ROLE = 'no_role'
LINKED = 'linked'
BREAK_GLASS = 'break_glass'
LAST_ADMIN = 'last_admin'
DOMAIN = 'domain'
BUSY = 'busy'

logger = logging.getLogger(__name__)


class SsoFailure(Exception):
    """A sign-in refused, with `code` for the sign-in page and the reason for
    the log. `who` is the email or subject the provider vouched for, once it
    has: a refusal after that is recorded in the activity log too.
    """

    def __init__(self, code: str, reason: str, who: str = '') -> None:
        super().__init__(reason)
        self.code = code
        self.who = who


class PendingLogin(NamedTuple):
    state: str
    nonce: str
    verifier: str


def pendingCookie(state: str) -> str:
    """The name of the cookie holding the sign-in with `state`."""

    return PENDING_COOKIE + state[:PENDING_COOKIE_STATE]


def codeChallenge(verifier: str) -> str:
    """PKCE's S256 challenge for `verifier` (RFC 7636)."""

    return b64(hashlib.sha256(verifier.encode('ascii')).digest())


def _email(claims: Dict[str, Any]) -> str:

    email = claims.get('email')

    return email.strip().lower() if isinstance(email, str) else ''


def _claim(claims: Dict[str, Any], path: str) -> Any:
    """The claim at `path`, which may be dotted to reach inside an object, as
    Keycloak's realm_access.roles is.
    """

    if path in claims:
        return claims[path]
    value: Any = claims
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]

    return value


def roleFromClaims(settings: SsoSettings, claims: Dict[str, Any]) -> Optional[str]:
    """The highest role a group in the role claim gives, or None."""

    value = _claim(claims, settings.roleClaim)
    if isinstance(value, str):
        groups = {value}
    elif isinstance(value, (list, tuple)):
        groups = {str(item) for item in value}
    else:
        groups = set()
    for role, names in settings.roleGroups.items():
        if groups.intersection(names):
            return role

    return None


class OidcClient:
    """Studio as a client of one identity provider. The provider's discovery
    document and keys are read when first needed and kept, so Studio starts
    even when the provider can't be reached, and says so at sign-in.
    """

    def __init__(self, settings: SsoSettings, transport: Optional[httpx.BaseTransport] = None, clock: Optional[Callable[[], float]] = None) -> None:
        self.settings = settings
        self._clock = clock or (lambda: time.time())
        self._http = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, transport=transport, follow_redirects=False,
                                  headers={'Accept': 'application/json', 'User-Agent': 'bauta-studio'})
        # Signs the pending-login cookie. Made at each start: a restart only
        # asks whoever was at the provider just then to sign in again.
        self._cookieKey = secrets.token_bytes(32)
        self._lock = threading.Lock()
        # One read from the provider at a time: many sign-ins started at once
        # as Studio starts read its discovery document once.
        self._fetching = threading.RLock()
        self._metadata: Optional[Dict[str, Any]] = None
        self._metadataAt = 0.0
        self._keys: Optional[jwt.PyJWKSet] = None
        self._keysAt = 0.0
        self._failure: Optional[SsoFailure] = None
        self._failedAt = 0.0
        # The states of sign-ins already brought back, until their cookies
        # expire: a cookie and its state are taken once.
        self._usedStates: Dict[str, float] = {}


    def close(self) -> None:

        self._http.close()


    # The pending-login cookie.

    def newLogin(self) -> PendingLogin:

        return PendingLogin(state=secrets.token_urlsafe(32), nonce=secrets.token_urlsafe(32), verifier=secrets.token_urlsafe(64))


    def seal(self, login: PendingLogin) -> str:

        return seal(self._cookieKey, login._asdict(), self._clock() + PENDING_SECONDS)


    def unseal(self, value: Optional[str]) -> Optional[PendingLogin]:
        """The login `value` holds, if Studio sealed it and it hasn't expired."""

        data = unseal(self._cookieKey, value, self._clock())
        try:
            return PendingLogin(**{name: str(data[name]) for name in PendingLogin._fields}) if data is not None else None
        except KeyError:
            return None


    def takeState(self, state: str) -> bool:
        """Whether `state` is brought back for the first time; it won't be again."""

        now = self._clock()
        with self._lock:
            self._usedStates = {used: expires for used, expires in self._usedStates.items() if expires > now}
            if state in self._usedStates:
                return False
            self._usedStates[state] = now + PENDING_SECONDS

        return True


    # Talking to the provider.

    def _getJson(self, url: str, what: str, **options: Any) -> Dict[str, Any]:

        try:
            response = self._http.get(url, **options)
        except httpx.HTTPError as error:
            raise SsoFailure(UNREACHABLE, 'could not read the identity provider\'s {}: {}'.format(what, error)) from error
        if response.status_code != 200:
            raise SsoFailure(UNREACHABLE, 'the identity provider\'s {} answered {}'.format(what, response.status_code))
        try:
            data = response.json()
        except ValueError as error:
            raise SsoFailure(UNREACHABLE, 'the identity provider\'s {} is not JSON'.format(what)) from error
        if not isinstance(data, dict):
            raise SsoFailure(UNREACHABLE, 'the identity provider\'s {} is not a JSON object'.format(what))

        return data


    def _fetch(self, fetch: Callable[[], Any]) -> Any:
        """What `fetch` reads from the provider, one read at a time; a failure
        is given again to whoever asks within FAILURE_SECONDS, unread.
        """

        with self._fetching:
            if self._failure is not None and self._clock() - self._failedAt < FAILURE_SECONDS:
                raise self._failure
            try:
                return fetch()
            except SsoFailure as failure:
                if failure.code == UNREACHABLE:
                    self._failure, self._failedAt = failure, self._clock()
                raise


    def metadata(self) -> Dict[str, Any]:
        """The provider's discovery document, checked: it must name the issuer
        Studio was configured with exactly, as OpenID Connect Discovery says,
        and reach its endpoints over TLS.
        """

        with self._lock:
            if self._metadata is not None and self._clock() - self._metadataAt < DISCOVERY_SECONDS:
                return self._metadata

        return self._fetch(self._readMetadata)


    def _readMetadata(self) -> Dict[str, Any]:

        with self._lock:
            if self._metadata is not None and self._clock() - self._metadataAt < DISCOVERY_SECONDS:
                return self._metadata
        issuer = self.settings.issuer
        data = self._getJson(issuer.rstrip('/') + '/.well-known/openid-configuration', 'discovery document')
        if data.get('issuer') != issuer:
            raise SsoFailure(UNREACHABLE, 'the identity provider names its issuer {!r}, not {!r}: set BAUTA_STUDIO_OIDC_ISSUER to exactly what it names'
                             .format(data.get('issuer'), issuer))
        for name in ('authorization_endpoint', 'token_endpoint', 'jwks_uri') + (('userinfo_endpoint',) if data.get('userinfo_endpoint') else ()):
            value = data.get(name)
            if not isinstance(value, str):
                raise SsoFailure(UNREACHABLE, 'the identity provider\'s discovery document has no {}'.format(name))
            try:
                httpsUrl(name, value.split('?', 1)[0])
            except UsageError as error:
                raise SsoFailure(UNREACHABLE, 'the identity provider\'s {}'.format(error)) from error

        with self._lock:
            self._metadata, self._metadataAt = data, self._clock()

        return data


    def authorizationUrl(self, login: PendingLogin) -> str:

        endpoint = self.metadata()['authorization_endpoint']
        parameters = {'response_type': 'code', 'client_id': self.settings.clientId, 'redirect_uri': self.settings.redirectUri,
                      'scope': ' '.join(self.settings.scopes), 'state': login.state, 'nonce': login.nonce,
                      'code_challenge': codeChallenge(login.verifier), 'code_challenge_method': 'S256'}
        # Some endpoints carry a query of their own, as Azure AD B2C's policy.
        parts = urllib.parse.urlsplit(endpoint)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True) + list(parameters.items())

        return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


    def _exchange(self, code: str, login: PendingLogin) -> Dict[str, Any]:
        """The provider's tokens for `code`, authenticating as RFC 6749 says:
        with HTTP Basic unless the provider only takes the secret in the body.
        """

        metadata = self.metadata()
        form = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': self.settings.redirectUri, 'code_verifier': login.verifier}
        headers: Dict[str, str] = {}
        secret = self.settings.clientSecret
        methods = metadata.get('token_endpoint_auth_methods_supported') or ['client_secret_basic']
        if secret is not None and 'client_secret_basic' in methods:
            # Each part form-encoded before joining, as RFC 6749 2.3.1 says.
            pair = '{}:{}'.format(urllib.parse.quote(self.settings.clientId, safe=''), urllib.parse.quote(secret, safe=''))
            headers['Authorization'] = 'Basic ' + base64.b64encode(pair.encode()).decode('ascii')
        else:
            form['client_id'] = self.settings.clientId
            if secret is not None:
                form['client_secret'] = secret
        try:
            response = self._http.post(metadata['token_endpoint'], data=form, headers=headers)
        except httpx.HTTPError as error:
            raise SsoFailure(UNREACHABLE, 'could not reach the identity provider\'s token endpoint: {}'.format(error)) from error
        try:
            tokens = response.json()
        except ValueError:
            tokens = {}
        if response.status_code != 200 or not isinstance(tokens, dict):
            # The error and its description are the provider's words for what
            # it refused; nothing in them is a secret.
            said = tokens if isinstance(tokens, dict) else {}
            refusal = ' '.join(str(said.get(name, '')) for name in ('error', 'error_description')).strip()[:PROVIDER_TEXT_LENGTH]
            raise SsoFailure(TOKEN, 'the identity provider refused the code ({}): {}'.format(response.status_code, refusal))
        if not isinstance(tokens.get('id_token'), str):
            raise SsoFailure(TOKEN, 'the identity provider gave no ID token: does Studio ask for the openid scope?')

        return tokens


    def _keySet(self, refresh: bool) -> jwt.PyJWKSet:

        def fresh() -> Optional[jwt.PyJWKSet]:
            with self._lock:
                age = self._clock() - self._keysAt
                if self._keys is not None and age < DISCOVERY_SECONDS and (not refresh or age < KEYS_REFRESH_SECONDS):
                    return self._keys
            return None

        cached = fresh()

        return cached if cached is not None else self._fetch(lambda: fresh() or self._readKeys())


    def _readKeys(self) -> jwt.PyJWKSet:

        data = self._getJson(self.metadata()['jwks_uri'], 'keys')
        # Keys for encryption are left out: an ID token is checked only with
        # a key published for signing.
        keys = [key for key in data.get('keys', []) if isinstance(key, dict) and key.get('use', 'sig') == 'sig']
        try:
            keySet = jwt.PyJWKSet.from_dict({'keys': keys})
        except jwt.PyJWTError as error:
            raise SsoFailure(UNREACHABLE, 'the identity provider publishes no key Studio can use: {}'.format(error)) from error

        with self._lock:
            self._keys, self._keysAt = keySet, self._clock()

        return keySet


    def _signingKey(self, keyId: Optional[str]) -> Optional[jwt.PyJWK]:

        def find(keySet: jwt.PyJWKSet) -> Optional[jwt.PyJWK]:
            if keyId is None:
                return keySet.keys[0] if len(keySet.keys) == 1 else None
            return next((key for key in keySet.keys if key.key_id == keyId), None)

        return find(self._keySet(refresh=False)) or find(self._keySet(refresh=True))


    def verifyIdToken(self, token: str, nonce: str) -> Dict[str, Any]:
        """The ID token's claims, once OpenID Connect Core 3.1.3.7 is satisfied."""

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as error:
            raise SsoFailure(TOKEN, 'the ID token is malformed: {}'.format(error)) from error
        algorithm = header.get('alg')
        allowed = [name for name in self.metadata().get('id_token_signing_alg_values_supported') or ['RS256'] if name in SIGNING_ALGORITHMS]
        if algorithm not in allowed:
            raise SsoFailure(TOKEN, 'the ID token is signed with {!r}; Studio takes {}'.format(algorithm, ', '.join(allowed) or 'no algorithm it offers'))
        key = self._signingKey(header.get('kid'))
        if key is None:
            raise SsoFailure(TOKEN, 'the ID token is signed with a key the identity provider doesn\'t publish')
        try:
            claims = jwt.decode(token, key.key, algorithms=[str(algorithm)], audience=self.settings.clientId, issuer=self.settings.issuer,
                                leeway=CLOCK_LEEWAY_SECONDS, options={'require': ['exp', 'iat', 'iss', 'aud', 'sub']})
        except (jwt.PyJWTError, TypeError, ValueError) as error:
            # A key of one type for an algorithm of another is a TypeError.
            raise SsoFailure(TOKEN, 'the ID token was refused: {}'.format(error)) from error

        audience = claims['aud']
        if isinstance(audience, list) and len(audience) > 1 and claims.get('azp') != self.settings.clientId:
            raise SsoFailure(TOKEN, 'the ID token is for several clients and not authorized for this one')
        if 'azp' in claims and claims['azp'] != self.settings.clientId:
            raise SsoFailure(TOKEN, 'the ID token was issued to another client')
        if not isinstance(claims.get('nonce'), str) or not hmac.compare_digest(claims['nonce'].encode(), nonce.encode()):
            raise SsoFailure(TOKEN, 'the ID token is not the one this sign-in asked for: its nonce differs')
        if not isinstance(claims['sub'], str) or not claims['sub'] or len(claims['iss']) > IDENTITY_LENGTH:
            raise SsoFailure(TOKEN, 'the ID token names its subject or issuer in a way Studio can\'t keep')

        return claims


    def _userinfo(self, accessToken: str, subject: str) -> Dict[str, Any]:

        endpoint = self.metadata().get('userinfo_endpoint')
        if not endpoint:
            return {}
        data = self._getJson(endpoint, 'userinfo endpoint', headers={'Authorization': 'Bearer ' + accessToken})
        if data.get('sub') != subject:
            raise SsoFailure(TOKEN, 'the userinfo endpoint answered for another subject')

        return data


    def claimsFor(self, code: str, login: PendingLogin) -> Dict[str, Any]:
        """Who the provider says signed in. A claim the ID token lacks -- the
        email, or the groups, which some providers leave out of it -- is read
        from the userinfo endpoint, for the same subject.
        """

        tokens = self._exchange(code, login)
        claims = self.verifyIdToken(tokens['id_token'], login.nonce)
        needed = ['email'] + ([self.settings.roleClaim] if self.settings.roles == RoleSource.PROVIDER else [])
        if any(_claim(claims, name) is None for name in needed) and isinstance(tokens.get('access_token'), str):
            # Only what the token lacks: what it says, verified, stands.
            for name, value in self._userinfo(tokens['access_token'], claims['sub']).items():
                claims.setdefault(name, value)

        return claims


def _name(claims: Dict[str, Any]) -> str:

    name = claims.get('name')

    return name.strip()[:200] if isinstance(name, str) else ''


def _otherActiveAdmins(session: Session, user: User) -> int:

    return session.scalar(select(func.count()).select_from(User).where(User.role == ADMIN, User.active.is_(True), User.id != user.id)) or 0


def signInWith(session: Session, settings: Settings, claims: Dict[str, Any]) -> User:
    """The account the verified `claims` sign in to, linked, made or brought
    up to date as needed, for the caller to commit; an SsoFailure if none.

    An identity already linked signs in to its account whatever its email is
    now. One not yet linked is linked to the account with its email, only if
    the provider vouches for the email: providers that let people set their
    own email would otherwise let anyone take over any account. An account
    linked to another identity from this issuer is refused rather than
    relinked; an admin unlinks it first. One linked from another issuer, as
    after a move to another of the provider's authorization servers, is
    linked anew.

    Linking leaves no password the policy wouldn't let the account use, so
    disabling someone at the provider is enough; but a break-glass admin,
    the one kind of account that keeps a password when only admins have one,
    isn't linked at all, so the provider can't take its role away.
    """

    sso = settings.sso
    assert sso is not None
    issuer, subject = claims['iss'], claims['sub']
    email = _email(claims)
    who = email or subject
    verified = bool(email) and (claims.get('email_verified') in VERIFIED or claims.get('xms_edov') in VERIFIED)
    trusted = verified or (bool(email) and sso.trustEmail)
    providerRole = roleFromClaims(sso, claims) if sso.roles == RoleSource.PROVIDER else None
    if sso.roles == RoleSource.PROVIDER and providerRole is None:
        raise SsoFailure(NO_ROLE, 'none of {}\'s groups gives a role'.format(who), who)
    if sso.hostedDomains and claims.get('hd') not in sso.hostedDomains:
        raise SsoFailure(DOMAIN, '{} isn\'t an account of {} (its hd claim is {!r})'.format(who, ', '.join(sso.hostedDomains), claims.get('hd')), who)
    identity = session.get(SsoIdentity, SsoIdentity.key(issuer, subject))

    if identity is not None:
        user = identity.user
    else:
        if not email:
            raise SsoFailure(EMAIL, 'the identity provider gave no email for {}: does Studio ask for the email scope?'.format(subject), who)
        if not trusted:
            raise SsoFailure(EMAIL, 'the identity provider doesn\'t say {} is verified; set BAUTA_STUDIO_OIDC_TRUST_EMAIL=1 only if its admins '
                                    'alone set emails'.format(email), who)
        found = session.scalar(select(User).where(User.email == email))
        if found is not None:
            linked = found.ssoIdentity
            if linked is not None and linked.issuer == issuer:
                raise SsoFailure(LINKED, '{}\'s account is linked to another identity at the provider'.format(email), who)
            if not found.active:
                raise SsoFailure(DISABLED, '{}\'s account is deactivated'.format(email), who)
            if found.passwordHash is not None and found.role == ADMIN and settings.passwordLogin == PasswordLogin.ADMINS:
                raise SsoFailure(BREAK_GLASS, '{} is a break-glass admin, which signs in with its password and isn\'t linked to the provider'
                                 .format(email), who)
            user = found
            detail: Dict[str, Any] = {}
            if linked is not None:
                # The old link goes before the new one is written: one per user.
                detail['issuerWas'] = linked.issuer
                session.delete(linked)
                session.flush()
            if user.passwordHash is not None and settings.passwordLogin != PasswordLogin.ALL:
                user.passwordHash = None
                detail['password'] = 'removed'
            user.ssoIdentity = SsoIdentity.of(issuer, subject)
            record(session, user, 'linked single sign-on', email, **detail)
        elif sso.autoCreate and email.rsplit('@', 1)[-1] in sso.allowedDomains:
            role = providerRole or VIEWER
            user = User(email=email, name=_name(claims), role=role, passwordHash=None, active=True)
            user.ssoIdentity = SsoIdentity.of(issuer, subject)
            session.add(user)
            record(session, None, 'added a user at their first sign-in', email, role=role)
        else:
            raise SsoFailure(NO_ACCOUNT, 'no account has the email {}'.format(email), who)

    if not user.active:
        raise SsoFailure(DISABLED, '{}\'s account is deactivated'.format(user.email), who)

    if providerRole is not None and providerRole != user.role:
        if user.role == ADMIN and user.id is not None and not _otherActiveAdmins(session, user):
            raise SsoFailure(LAST_ADMIN, '{} is the last active admin, and their groups would make them {}'.format(user.email, providerRole), who)
        record(session, user, 'role set by the identity provider', user.email, role=providerRole, was=user.role)
        user.role = providerRole

    # An email changed at the provider follows, if the provider vouches for it
    # and no other account has it.
    if identity is not None and verified and email != user.email and session.scalar(select(User.id).where(User.email == email)) is None:
        record(session, user, 'email set by the identity provider', email, was=user.email)
        user.email = email
    if not user.name:
        user.name = _name(claims)

    return user

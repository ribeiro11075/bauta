"""Where Studio finds bauta's configuration and keeps its own state, from
flags or the environment.
"""
from __future__ import annotations

import os
import sys
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple, Type, TypeVar

from ..project import UsageError

CONFIG_VARIABLE = 'BAUTA_STUDIO_CONFIG'
DATA_VARIABLE = 'BAUTA_STUDIO_DATA'
DATABASE_URL_VARIABLE = 'BAUTA_STUDIO_DATABASE_URL'
INSECURE_COOKIES_VARIABLE = 'BAUTA_STUDIO_INSECURE_COOKIES'
TRANSFORM_MODULES_VARIABLE = 'BAUTA_STUDIO_TRANSFORM_MODULES'
PUBLIC_URL_VARIABLE = 'BAUTA_STUDIO_PUBLIC_URL'
PASSWORD_LOGIN_VARIABLE = 'BAUTA_STUDIO_PASSWORD_LOGIN'
REQUIRE_TWO_FACTOR_VARIABLE = 'BAUTA_STUDIO_REQUIRE_2FA'
SECRET_KEY_FILE_VARIABLE = 'BAUTA_STUDIO_SECRET_KEY_FILE'
SESSION_HOURS_VARIABLE = 'BAUTA_STUDIO_SESSION_HOURS'
# Single sign-on through OpenID Connect: on once an issuer is named.
OIDC_PREFIX = 'BAUTA_STUDIO_OIDC_'
# How long a session lasts, by default: one signed in with a password, and one
# through the identity provider. An SSO session is shorter: someone the
# identity provider disables keeps a Studio session they already have until it
# ends.
SESSION_HOURS = 12
SSO_SESSION_HOURS = 8
# The key authenticator secrets are encrypted with, made in the data directory
# unless BAUTA_STUDIO_SECRET_KEY_FILE names one kept elsewhere.
SECRET_KEY_FILE = 'secret.key'
DEFAULT_SCOPES = ('openid', 'email', 'profile')
DEFAULT_ROLE_CLAIM = 'groups'
DEFAULT_SSO_LABEL = 'Sign in with SSO'
# Hosts a developer's identity provider, or Studio itself, may be reached on
# over plain HTTP. Anywhere else, tokens and codes travel only over TLS.
LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1'}
# How many runs' history the run list reads back.
HISTORY_RUNS = 50
# What Studio creates its data directory, and those in it, with: its owner's
# alone. It holds password hashes, run logs and the jobs files as they were.
PRIVATE_DIRECTORY_MODE = 0o700


EnumT = TypeVar('EnumT', bound=Enum)


class PasswordLogin(str, Enum):
    """Who may sign in with a password: everyone; only admins, which keeps a
    break-glass account for when the identity provider is down or
    misconfigured; or no one.
    """

    ALL = 'all'
    ADMINS = 'admins'
    OFF = 'off'


class TwoFactorPolicy(str, Enum):
    """Whose sign-in with a password needs a code from an authenticator app
    too. Signing in through the identity provider never does: the provider
    asks for its own second factor. By default, admins' once single sign-on is
    on, when a password is a break-glass admin's.
    """

    OFF = 'off'
    ADMINS = 'admins'
    ALL = 'all'


class RoleSource(str, Enum):
    """Where an SSO user's role comes from: Studio's Users page, or their
    groups at the identity provider, read again at every sign-in.
    """

    STUDIO = 'studio'
    PROVIDER = 'provider'


def _choice(kind: Type[EnumT], variable: str, value: object) -> EnumT:
    """`value` as a member of the enum `kind`, or a UsageError naming
    `variable` and what it may be.
    """

    try:
        return kind(value.strip().lower() if isinstance(value, str) else value)
    except ValueError:
        raise UsageError('{} must be one of {}: {!r}'.format(variable, ', '.join(member.value for member in kind), value)) from None


def _hours(variable: str, value: Optional[str], default: int) -> int:
    """`value` as a whole number of hours, 1 or more, or `default` if unset."""

    if value is None or not value.strip():
        return default
    try:
        hours = int(value)
    except ValueError:
        hours = 0
    if hours < 1:
        raise UsageError('{} must be a whole number of hours, 1 or more: {!r}'.format(variable, value))

    return hours


def _truthy(value: Optional[str]) -> bool:

    return (value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _list(value: Optional[str]) -> Tuple[str, ...]:

    return tuple(item.strip() for item in (value or '').split(',') if item.strip())


def httpsUrl(name: str, value: str) -> str:
    """`value` without a trailing slash, refused unless it is HTTPS or on this
    machine: an authorization code or an ID token sent in the clear is a
    sign-in for whoever reads it.
    """

    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {'https', 'http'} or not parsed.hostname:
        raise UsageError('{} must be a URL, such as https://studio.example.com: {!r}'.format(name, value))
    if parsed.scheme == 'http' and parsed.hostname not in LOCAL_HOSTS:
        raise UsageError('{} must use https unless it is on this machine: {}'.format(name, value))
    if parsed.query or parsed.fragment:
        raise UsageError('{} must not have a query or a fragment: {}'.format(name, value))

    return value.rstrip('/')


@dataclass(frozen=True)
class SsoSettings:
    """Signing in through an OpenID Connect identity provider: Okta, Entra ID,
    Google Workspace, Auth0, Keycloak and the rest. `issuer` is the provider's
    issuer exactly as its discovery document states it. `publicUrl` is where
    browsers reach Studio, which the callback the provider redirects to is
    under; it is never read from a request's Host header, which a client sets.

    An account is matched by the provider's issuer and subject once linked.
    Before that, a person is linked to the account an admin made with their
    email, if the provider vouches for the email (`email_verified`), or if
    `trustEmail` says the provider is one whose emails only its admins set.
    `autoCreate` makes an account for someone with none, only for an email in
    `allowedDomains`.

    With `roles` 'provider', the role is set at each sign-in from the
    `roleClaim` claim, the highest of `adminGroups`, `operatorGroups` and
    `viewerGroups` it names; someone it names none of is refused.

    `hostedDomains`, for Google Workspace, are the workspaces whose accounts
    may sign in, by the token's hd claim: a personal Google account made with
    a company email has a verified email but no hd.
    """

    issuer: str
    clientId: str
    clientSecret: Optional[str]
    publicUrl: str
    scopes: Tuple[str, ...] = DEFAULT_SCOPES
    label: str = DEFAULT_SSO_LABEL
    sessionHours: int = SSO_SESSION_HOURS
    trustEmail: bool = False
    autoCreate: bool = False
    allowedDomains: Tuple[str, ...] = ()
    roles: RoleSource = RoleSource.STUDIO
    roleClaim: str = DEFAULT_ROLE_CLAIM
    hostedDomains: Tuple[str, ...] = ()
    adminGroups: Tuple[str, ...] = ()
    operatorGroups: Tuple[str, ...] = ()
    viewerGroups: Tuple[str, ...] = ()

    def __post_init__(self) -> None:

        object.__setattr__(self, 'roles', _choice(RoleSource, OIDC_PREFIX + 'ROLES', self.roles))


    @property
    def redirectUri(self) -> str:

        return self.publicUrl + '/api/auth/sso/callback'


    @property
    def roleGroups(self) -> Dict[str, Tuple[str, ...]]:
        """Each role and the groups that give it, highest first."""

        return {'admin': self.adminGroups, 'operator': self.operatorGroups, 'viewer': self.viewerGroups}


    @classmethod
    def fromEnvironment(cls, environment: Mapping[str, str]) -> Optional['SsoSettings']:
        """None unless an issuer is set; a UsageError for any setting that is
        missing or wrong, so Studio doesn't start half configured.
        """

        def get(name: str) -> Optional[str]:
            value = environment.get(OIDC_PREFIX + name)
            return value.strip() if value is not None and value.strip() else None

        issuer = get('ISSUER')
        if issuer is None:
            stray = sorted(name for name in environment if name.startswith(OIDC_PREFIX) and environment[name].strip())
            if stray:
                raise UsageError('{} is set but {}ISSUER is not, so single sign-on is off'.format(', '.join(stray), OIDC_PREFIX))
            return None

        httpsUrl(OIDC_PREFIX + 'ISSUER', issuer)
        clientId = get('CLIENT_ID')
        if clientId is None:
            raise UsageError('{}CLIENT_ID must be set: the client ID the identity provider gave Studio'.format(OIDC_PREFIX))
        secret = get('CLIENT_SECRET')
        secretFile = get('CLIENT_SECRET_FILE')
        if secret is not None and secretFile is not None:
            raise UsageError('set {0}CLIENT_SECRET or {0}CLIENT_SECRET_FILE, not both'.format(OIDC_PREFIX))
        if secretFile is not None:
            try:
                secret = Path(secretFile).read_text().strip() or None
            except OSError as error:
                raise UsageError('{}CLIENT_SECRET_FILE can\'t be read: {}'.format(OIDC_PREFIX, error)) from error
        publicUrl = environment.get(PUBLIC_URL_VARIABLE, '').strip()
        if not publicUrl:
            raise UsageError('{} must be set for single sign-on: where browsers reach Studio, such as https://studio.example.com'
                             .format(PUBLIC_URL_VARIABLE))
        publicUrl = httpsUrl(PUBLIC_URL_VARIABLE, publicUrl)
        if urllib.parse.urlsplit(publicUrl).path not in {'', '/'}:
            raise UsageError('{} must be Studio\'s address with no path: Studio is served at the root of its host'.format(PUBLIC_URL_VARIABLE))

        # Space-separated, as OAuth sends them and providers write them.
        scopes = tuple((get('SCOPES') or '').split()) or DEFAULT_SCOPES
        if 'openid' not in scopes:
            raise UsageError('{}SCOPES must include openid'.format(OIDC_PREFIX))
        sessionHours = _hours(OIDC_PREFIX + 'SESSION_HOURS', get('SESSION_HOURS'), SSO_SESSION_HOURS)

        autoCreate = _truthy(get('AUTO_CREATE'))
        domains = tuple(domain.lower().lstrip('@') for domain in _list(get('ALLOWED_DOMAINS')))
        if autoCreate and not domains:
            raise UsageError('{0}AUTO_CREATE needs {0}ALLOWED_DOMAINS: the email domains whose people get an account at their first sign-in'
                             .format(OIDC_PREFIX))

        roles = _choice(RoleSource, OIDC_PREFIX + 'ROLES', get('ROLES') or RoleSource.STUDIO)
        groups = {role: _list(get(role.upper() + '_GROUPS')) for role in ('admin', 'operator', 'viewer')}
        if roles == RoleSource.PROVIDER and not any(groups.values()):
            raise UsageError('{0}ROLES=provider needs at least one of {0}ADMIN_GROUPS, {0}OPERATOR_GROUPS and {0}VIEWER_GROUPS'.format(OIDC_PREFIX))

        return cls(issuer=issuer, clientId=clientId, clientSecret=secret, publicUrl=publicUrl, scopes=scopes, label=get('LABEL') or DEFAULT_SSO_LABEL,
                   sessionHours=sessionHours, trustEmail=_truthy(get('TRUST_EMAIL')), autoCreate=autoCreate, allowedDomains=domains, roles=roles,
                   hostedDomains=tuple(domain.lower() for domain in _list(get('HOSTED_DOMAINS'))),
                   roleClaim=get('ROLE_CLAIM') or DEFAULT_ROLE_CLAIM, adminGroups=groups['admin'], operatorGroups=groups['operator'],
                   viewerGroups=groups['viewer'])


@dataclass(frozen=True)
class Settings:
    """`configDirectory` holds bauta's jobs.yaml and connections.yaml, as
    `bauta --config` would take it. `dataDirectory` is Studio's own: its
    database, unless `databaseUrl` names another, and the logs of the runs it
    starts. `secureCookies` is off only for plain HTTP on a developer's
    machine; behind TLS it must stay on. `transformModules` are the modules,
    with what is under them, an edit here may start naming in a transform,
    beyond bauta's own and those the jobs files already use. `sso` is single
    sign-on, off when None. `passwordLogin` is who may sign in with a password,
    and `requireTwoFactor` whose password needs an authenticator's code too.
    `secretKeyFile` holds the key authenticator secrets are encrypted with;
    by default one Studio makes in the data directory.
    """

    configDirectory: Path
    dataDirectory: Path
    databaseUrl: str
    secureCookies: bool = True
    sessionHours: int = SESSION_HOURS
    # How a run is started: this Python's bauta, so Studio and the runs it
    # starts are always the same version.
    bautaCommand: List[str] = field(default_factory=lambda: [sys.executable, '-m', 'bauta'])
    transformModules: Tuple[str, ...] = ()
    sso: Optional[SsoSettings] = None
    passwordLogin: PasswordLogin = PasswordLogin.ALL
    requireTwoFactor: TwoFactorPolicy = TwoFactorPolicy.OFF
    secretKeyFile: Optional[Path] = None

    def __post_init__(self) -> None:

        # Taken as text too, as the environment gives them.
        object.__setattr__(self, 'requireTwoFactor', _choice(TwoFactorPolicy, REQUIRE_TWO_FACTOR_VARIABLE, self.requireTwoFactor))
        object.__setattr__(self, 'passwordLogin', _choice(PasswordLogin, PASSWORD_LOGIN_VARIABLE, self.passwordLogin))

        if self.passwordLogin != PasswordLogin.ALL and self.sso is None:
            raise UsageError('{}={} needs single sign-on, or no one else could sign in: set {}ISSUER'.format(PASSWORD_LOGIN_VARIABLE,
                                                                                                          self.passwordLogin.value, OIDC_PREFIX))


    @classmethod
    def fromEnvironment(cls, configDirectory: Optional[str] = None, dataDirectory: Optional[str] = None) -> 'Settings':

        config = Path(configDirectory or os.environ.get(CONFIG_VARIABLE) or os.environ.get('BAUTA_CONFIG') or 'configuration').resolve()
        data = Path(dataDirectory or os.environ.get(DATA_VARIABLE) or 'studio-data').resolve()
        url = os.environ.get(DATABASE_URL_VARIABLE) or 'sqlite:///{}'.format(data / 'studio.db')

        modules = tuple(module.strip() for module in os.environ.get(TRANSFORM_MODULES_VARIABLE, '').split(',') if module.strip())

        sso = SsoSettings.fromEnvironment(os.environ)
        twoFactor = os.environ.get(REQUIRE_TWO_FACTOR_VARIABLE) or (TwoFactorPolicy.ADMINS if sso is not None else TwoFactorPolicy.OFF)
        keyFile = os.environ.get(SECRET_KEY_FILE_VARIABLE)

        return cls(configDirectory=config, dataDirectory=data, databaseUrl=url, secureCookies=os.environ.get(INSECURE_COOKIES_VARIABLE) != '1',
                   transformModules=modules, sso=sso,
                   passwordLogin=_choice(PasswordLogin, PASSWORD_LOGIN_VARIABLE, os.environ.get(PASSWORD_LOGIN_VARIABLE) or PasswordLogin.ALL),
                   requireTwoFactor=_choice(TwoFactorPolicy, REQUIRE_TWO_FACTOR_VARIABLE, twoFactor),
                   secretKeyFile=Path(keyFile).resolve() if keyFile else None,
                   sessionHours=_hours(SESSION_HOURS_VARIABLE, os.environ.get(SESSION_HOURS_VARIABLE), SESSION_HOURS))


    @property
    def runLogDirectory(self) -> Path:

        return self.dataDirectory / 'runs'


    @property
    def keyFile(self) -> Path:

        return self.secretKeyFile or self.dataDirectory / SECRET_KEY_FILE


    @property
    def backupDirectory(self) -> Path:
        """Where each configuration file is kept as it was before an edit."""

        return self.dataDirectory / 'backups'

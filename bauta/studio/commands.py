"""`bauta studio serve`, `bauta studio create-user` and `bauta studio set-password`."""
from __future__ import annotations

import argparse
import contextlib
import getpass
import logging
import sys
from typing import Optional

from sqlalchemy import select

from .settings import PRIVATE_DIRECTORY_MODE, Settings

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8710


def _settings(arguments: argparse.Namespace) -> Settings:

    return Settings.fromEnvironment(configDirectory=arguments.config, dataDirectory=arguments.data)


def serve(arguments: argparse.Namespace) -> int:

    import uvicorn

    from .app import createApp
    from .db import ADMIN, Database, User
    from .settings import PasswordLogin

    settings = _settings(arguments)
    settings.dataDirectory.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    app = createApp(settings)
    database = Database(settings.databaseUrl)
    try:
        with database.open() as session:
            if session.scalar(select(User).limit(1)) is None:
                print('No accounts yet. Add the first admin with:\n  bauta studio create-user --email you@example.com', file=sys.stderr)
            elif settings.sso is not None and settings.passwordLogin != PasswordLogin.OFF and session.scalar(
                    select(User).where(User.role == ADMIN, User.active.is_(True), User.passwordHash.is_not(None)).limit(1)) is None:
                print('Single sign-on is on, and no active admin has a password: if the identity provider is down or misconfigured, no one can '
                      'sign in to fix it. Keep a break-glass admin with:\n  bauta studio create-user --email break-glass@example.com',
                      file=sys.stderr)
    finally:
        database.close()
    if settings.sso is not None:
        _checkProvider(settings)
    if arguments.host != DEFAULT_HOST and not settings.secureCookies:
        print('Warning: cookies are not marked Secure, and Studio is listening beyond this machine. Serve it behind TLS.', file=sys.stderr)

    from .routes.sso import CallbackQueryFilter
    logging.getLogger('uvicorn.access').addFilter(CallbackQueryFilter())

    # One process: the runs Studio starts are tracked in it.
    try:
        uvicorn.run(app, host=arguments.host, port=arguments.port, proxy_headers=True, forwarded_allow_ips=arguments.forwarded_allow_ips)
    finally:
        app.state.database.close()

    return 0


def _checkProvider(settings: Settings) -> None:
    """Reads the identity provider's discovery document once, to say now, not
    at someone's first sign-in, that the issuer is wrong or unreachable. Studio
    starts either way: the provider may only be down for now.
    """

    from .sso import OidcClient, SsoFailure

    assert settings.sso is not None
    client = OidcClient(settings.sso)
    try:
        client.metadata()
    except SsoFailure as failure:
        print('Warning: single sign-on won\'t work until this is fixed: {}'.format(failure), file=sys.stderr)
    finally:
        client.close()


def _askPassword(email: str) -> Optional[str]:
    """A new password for `email`, typed twice, hashed; None, having said
    why, if the two differ or it is too short.
    """

    from .auth import hashPassword

    password = getpass.getpass('Password for {}: '.format(email))
    if password != getpass.getpass('Again: '):
        print('The passwords differ.', file=sys.stderr)
        return None
    try:
        return hashPassword(password)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return None


def createUser(arguments: argparse.Namespace) -> int:

    from .auth import record
    from .db import ROLES, Database, User

    settings = _settings(arguments)
    settings.dataDirectory.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    if arguments.role not in ROLES:
        print('--role must be one of {}'.format(', '.join(ROLES)), file=sys.stderr)
        return 2

    passwordHash = None
    if not arguments.sso_only:
        passwordHash = _askPassword(arguments.email)
        if passwordHash is None:
            return 2

    email = arguments.email.strip().lower()
    database = Database(settings.databaseUrl)
    with contextlib.closing(database), database.open() as session:
        if session.scalar(select(User).where(User.email == email)) is not None:
            print('{} already has an account.'.format(email), file=sys.stderr)
            return 2
        session.add(User(email=email, name=arguments.name or '', role=arguments.role, passwordHash=passwordHash))
        record(session, None, 'added a user from the command line', email, role=arguments.role, password=passwordHash is not None)
        session.commit()

    print('Added {} as {}{}.'.format(email, arguments.role, ', to sign in through single sign-on' if passwordHash is None else ''))

    return 0


def setPassword(arguments: argparse.Namespace) -> int:
    """Sets an account's password from the server's shell, and with
    --reset-2fa turns its two-factor sign-in off: the way back in for an admin
    who forgot their password, or lost both their app and their recovery
    codes, when no other admin can reset it. They are signed out everywhere.
    """

    from .auth import endSessionsOf, record
    from .db import Database, User
    from .twofactor import turnOff

    settings = _settings(arguments)
    email = arguments.email.strip().lower()
    database = Database(settings.databaseUrl)
    with contextlib.closing(database), database.open() as session:
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            print('No account has the email {}.'.format(email), file=sys.stderr)
            return 2
        passwordHash = _askPassword(email)
        if passwordHash is None:
            return 2
        user.passwordHash = passwordHash
        if arguments.reset_2fa:
            turnOff(user)
        endSessionsOf(session, user)
        record(session, None, 'set a password from the command line', email, twoFactor='reset' if arguments.reset_2fa else 'kept')
        session.commit()
        twoFactor = user.totpSecret is not None

    print('Set {}\'s password{}. They are signed out everywhere.'.format(
        email, ' and turned off their two-factor sign-in' if arguments.reset_2fa else '; their two-factor sign-in is still on' if twoFactor else ''))
    if not user.active:
        print('Note: the account is deactivated; an admin reactivates it on the Users page.', file=sys.stderr)

    return 0

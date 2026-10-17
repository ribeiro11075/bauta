"""`bauta studio serve` and `bauta studio create-user`."""
from __future__ import annotations

import argparse
import contextlib
import getpass
import sys

from sqlalchemy import select

from .settings import Settings

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8710


def _settings(arguments: argparse.Namespace) -> Settings:

    return Settings.fromEnvironment(configDirectory=arguments.config, dataDirectory=arguments.data)


def serve(arguments: argparse.Namespace) -> int:

    import uvicorn

    from .app import createApp
    from .db import Database, User

    settings = _settings(arguments)
    settings.dataDirectory.mkdir(parents=True, exist_ok=True)
    app = createApp(settings)
    database = Database(settings.databaseUrl)
    try:
        with database.open() as session:
            if session.scalar(select(User).limit(1)) is None:
                print('No accounts yet. Add the first admin with:\n  bauta studio create-user --email you@example.com', file=sys.stderr)
    finally:
        database.close()
    if arguments.host != DEFAULT_HOST and not settings.secureCookies:
        print('Warning: cookies are not marked Secure, and Studio is listening beyond this machine. Serve it behind TLS.', file=sys.stderr)

    # One process: the runs Studio starts are tracked in it.
    try:
        uvicorn.run(app, host=arguments.host, port=arguments.port, proxy_headers=True, forwarded_allow_ips=arguments.forwarded_allow_ips)
    finally:
        app.state.database.close()

    return 0


def createUser(arguments: argparse.Namespace) -> int:

    from .auth import hashPassword, record
    from .db import ROLES, Database, User

    settings = _settings(arguments)
    settings.dataDirectory.mkdir(parents=True, exist_ok=True)
    if arguments.role not in ROLES:
        print('--role must be one of {}'.format(', '.join(ROLES)), file=sys.stderr)
        return 2

    password = getpass.getpass('Password for {}: '.format(arguments.email))
    if password != getpass.getpass('Again: '):
        print('The passwords differ.', file=sys.stderr)
        return 2
    try:
        passwordHash = hashPassword(password)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    email = arguments.email.strip().lower()
    database = Database(settings.databaseUrl)
    with contextlib.closing(database), database.open() as session:
        if session.scalar(select(User).where(User.email == email)) is not None:
            print('{} already has an account.'.format(email), file=sys.stderr)
            return 2
        session.add(User(email=email, name=arguments.name or '', role=arguments.role, passwordHash=passwordHash))
        record(session, None, 'added a user from the command line', email, role=arguments.role)
        session.commit()

    print('Added {} as {}.'.format(email, arguments.role))

    return 0

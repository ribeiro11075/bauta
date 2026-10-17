"""`bauta studio`: the web console, when the `studio` extra is installed."""
from __future__ import annotations

import argparse

from ..log import Log
from .common import UsageError

STUDIO_EXTRA = 'pip install "bauta[studio]"'
# What the extra installs, checked before anything starts.
STUDIO_MODULES = ['fastapi', 'uvicorn', 'sqlalchemy', 'argon2']


def _commandStudio(arguments: argparse.Namespace, log: Log) -> int:
    """Hands over to bauta.studio, imported only now: without the extra,
    nothing else in bauta needs what it imports.
    """

    import importlib.util

    missing = [module for module in STUDIO_MODULES if importlib.util.find_spec(module) is None]
    if missing:
        raise UsageError('bauta studio needs its extra, which is not installed (no {}): {}'.format(', '.join(missing), STUDIO_EXTRA))

    from ..studio import commands

    return {'serve': commands.serve, 'create-user': commands.createUser}[arguments.studio_command](arguments)

"""Puts the repo root on sys.path, and stubs oracledb/psycopg in sys.modules
so any test that does exercise a dialect's connect() doesn't need those native
client libraries installed (the package itself only imports them lazily, inside
connect(), so this isn't required just to import bauta -- see
test_lazy_driver_imports.py).

Only stubs a module that's genuinely not installed (checked via find_spec,
which locates a module without importing it). Checking `moduleName not in
sys.modules` instead would be wrong: because the real import is lazy, a
genuinely-installed driver (e.g. psycopg for the postgres integration suite)
won't be in sys.modules yet at collection time either, and the stub would
permanently shadow the real package for the rest of the session the first
time something calls connect().

It also sets the package's logger up before any test runs, and detaches the
handlers a test's run added to it; see packageLogger below.
"""
import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

for moduleName in ('oracledb', 'psycopg'):
    if moduleName not in sys.modules and importlib.util.find_spec(moduleName) is None:
        sys.modules[moduleName] = types.ModuleType(moduleName)

# pytest's caplog reaches a logger that doesn't propagate, as the package's
# doesn't, only if that logger exists when a test starts. Log() creates it, so
# without this caplog saw the package's records only once an earlier test had
# run a command, and those tests failed when run on their own.
logging.getLogger('bauta').propagate = False


@pytest.fixture(autouse=True)
def packageLogger():
    """Detaches and closes the file and stream handlers a test's Log() added.
    The logger is process-wide, so a --log file otherwise kept receiving every
    later test's records, in a directory already torn down.
    """
    logger = logging.getLogger('bauta')
    before = set(logger.handlers)

    yield logger

    for handler in list(logger.handlers):
        if handler not in before and type(handler) in (logging.FileHandler, logging.StreamHandler):
            logger.removeHandler(handler)
            handler.close()

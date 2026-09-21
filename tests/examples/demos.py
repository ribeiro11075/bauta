"""Loading a demo under example/ for its test. They're loaded by file path
rather than imported, because example/ is deliberately not a package: nothing
in the library imports from it, so it carries no __init__.py.
"""
import contextlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Iterable, Iterator

EXAMPLES = Path(__file__).resolve().parents[2] / 'example'


@contextlib.contextmanager
def loadedDemo(folder: str, name: str, environment: Iterable[str] = ()) -> Iterator[object]:
    """example/<folder>/demo.py as module `name`. The variables in
    `environment` -- the ones the demo sets -- are put back as they were
    afterwards, so a temporary path or a demo key never leaks into a later test.
    """
    specification = importlib.util.spec_from_file_location(name, EXAMPLES / folder / 'demo.py')
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    previous = {variable: os.environ.get(variable) for variable in environment}

    try:
        specification.loader.exec_module(module)
        yield module
    finally:
        del sys.modules[name]
        for variable, value in previous.items():
            if value is None:
                os.environ.pop(variable, None)
            else:
                os.environ[variable] = value

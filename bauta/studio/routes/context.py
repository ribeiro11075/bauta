"""What every router is built from: Studio's settings and database, the
dependencies that sign a request in, and the configuration as it stands.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from fastapi import HTTPException

from ...log.scrubbing import describeError
from ...project import Project, UsageError
from .. import configfiles, workspace
from ..auth import LoginThrottle
from ..db import Database
from ..launcher import Launcher
from ..settings import Settings


@dataclass(frozen=True)
class Context:
    settings: Settings
    database: Database
    launcher: Launcher
    throttle: LoginThrottle
    sessions: Callable[..., Any]
    viewer: Callable[..., Any]
    operator: Callable[..., Any]
    admin: Callable[..., Any]

    def project(self) -> Project:
        """The configuration as it stands now, or a 409 saying why it doesn't load."""

        try:
            return workspace.loadProject(self.settings)
        except workspace.ConfigurationProblem as error:
            raise HTTPException(status_code=409, detail={'configuration': str(error)}) from error


@contextlib.contextmanager
def reachingDatabases(what: str) -> Iterator[None]:
    """For work that connects to a source or a target, where a failure is
    expected and its reason useful: a 422 saying why, with its message
    scrubbed, rather than a 500 saying nothing. Studio's own refusals pass
    through to their handlers.
    """

    try:
        yield
    except (HTTPException, configfiles.EditRefused, UsageError):
        raise
    except Exception as error:
        raise HTTPException(status_code=422, detail='{}: {}'.format(what, describeError(error))) from error

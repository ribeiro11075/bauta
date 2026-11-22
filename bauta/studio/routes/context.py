"""What every router is built from: Studio's settings and database, the
dependencies that sign a request in, and the configuration as it stands.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterator, Optional

from fastapi import HTTPException

from ...log.scrubbing import describeError
from ...project import Project, UsageError
from .. import configfiles, workspace
from ..auth import LoginThrottle
from ..twofactor import SecretBox
from ..db import Database
from ..launcher import Launcher
from ..settings import Settings

if TYPE_CHECKING:
    from ..sso import OidcClient


@dataclass(frozen=True)
class Context:
    """What every route of Studio's shares: its settings, database, launcher,
    throttles, secret box and identity provider's client, and the dependencies
    that require each role.
    """

    # secretBox: encrypts authenticator secrets, and throttles their codes.
    # ssoThrottle: counts single sign-ons that failed, by address.
    # oidc: the identity provider's client, when single sign-on is on.
    settings: Settings
    database: Database
    launcher: Launcher
    throttle: LoginThrottle
    sessions: Callable[..., Any]
    viewer: Callable[..., Any]
    operator: Callable[..., Any]
    admin: Callable[..., Any]
    secretBox: SecretBox
    codeThrottle: LoginThrottle
    ssoThrottle: LoginThrottle
    oidc: Optional['OidcClient'] = None

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

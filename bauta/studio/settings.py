"""Where Studio finds bauta's configuration and keeps its own state, from
flags or the environment.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

CONFIG_VARIABLE = 'BAUTA_STUDIO_CONFIG'
DATA_VARIABLE = 'BAUTA_STUDIO_DATA'
DATABASE_URL_VARIABLE = 'BAUTA_STUDIO_DATABASE_URL'
INSECURE_COOKIES_VARIABLE = 'BAUTA_STUDIO_INSECURE_COOKIES'
TRANSFORM_MODULES_VARIABLE = 'BAUTA_STUDIO_TRANSFORM_MODULES'
SESSION_HOURS = 12
# How many runs' history the run list reads back.
HISTORY_RUNS = 50


@dataclass(frozen=True)
class Settings:
    """`configDirectory` holds bauta's jobs.yaml and connections.yaml, as
    `bauta --config` would take it. `dataDirectory` is Studio's own: its
    database, unless `databaseUrl` names another, and the logs of the runs it
    starts. `secureCookies` is off only for plain HTTP on a developer's
    machine; behind TLS it must stay on. `transformModules` are the modules,
    with what is under them, an edit here may start naming in a transform,
    beyond bauta's own and those the jobs files already use.
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

    @classmethod
    def fromEnvironment(cls, configDirectory: Optional[str] = None, dataDirectory: Optional[str] = None) -> 'Settings':

        config = Path(configDirectory or os.environ.get(CONFIG_VARIABLE) or os.environ.get('BAUTA_CONFIG') or 'configuration').resolve()
        data = Path(dataDirectory or os.environ.get(DATA_VARIABLE) or 'studio-data').resolve()
        url = os.environ.get(DATABASE_URL_VARIABLE) or 'sqlite:///{}'.format(data / 'studio.db')

        modules = tuple(module.strip() for module in os.environ.get(TRANSFORM_MODULES_VARIABLE, '').split(',') if module.strip())

        return cls(configDirectory=config, dataDirectory=data, databaseUrl=url, secureCookies=os.environ.get(INSECURE_COOKIES_VARIABLE) != '1',
                   transformModules=modules)


    @property
    def runLogDirectory(self) -> Path:

        return self.dataDirectory / 'runs'


    @property
    def backupDirectory(self) -> Path:
        """Where each configuration file is kept as it was before an edit."""

        return self.dataDirectory / 'backups'

"""What the commands share: reading the flags into a configuration, where run
state, history and the manifest live, and how output is written. Loading and
checking a configuration is bauta.project's, which the commands share with
anything else that works with one; the names here are the flags' way to it.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..configuration import Configuration, ConnectionConfig, DataJobConfig, DataJobsFile, StorageLocation, TableLocation
from ..database.sessions import Connections, sourceQueryColumns, sourceQuerySample, targetColumns
from ..jobs.memory import DatabaseMemory, FileMemory, MemoryBackend
from ..log import Log
from ..project import (CONNECTIONS_FILE, DEFAULT_TABLES, JOBS_FILE, Location, Project, UsageError, checkColumnCounts,
                       checkMaskingCoverage, configDirectory, describeLocation, loadConnections, loadYaml, lockFileFor, openHistory, readJobs, requireAlias,
                       requireDatabase, resolveLocation, settingsFor)

EXIT_SUCCESS = 0


EXIT_JOBS_DID_NOT_SUCCEED = 1


EXIT_BAD_CONFIGURATION = 2


EXIT_INTERRUPTED = 130


MANIFEST_KEY_VARIABLE = 'BAUTA_MANIFEST_KEY'


NOTIFY_URL_VARIABLE = 'BAUTA_NOTIFY_URL'


def _configDirectory(arguments: argparse.Namespace) -> Path:
    """--config, else $BAUTA_CONFIG, else ./configuration -- so the
    common case is a bare `bauta run`.
    """

    return configDirectory(arguments.config)


def _resolveConfigurationPaths(arguments: argparse.Namespace) -> Tuple[Path, Path]:
    """Explicit --jobs/--connections win; otherwise both come from the config directory."""

    directory = _configDirectory(arguments)
    jobsPath = Path(arguments.jobs) if arguments.jobs else directory / JOBS_FILE
    connectionsPath = Path(arguments.connections) if arguments.connections else directory / CONNECTIONS_FILE

    return jobsPath, connectionsPath


def _resolveLocation(arguments: argparse.Namespace, setting: str, configured: Optional[StorageLocation] = None) -> Optional[Location]:
    """Where run state, history or the manifest goes: the setting's file flag
    (--memory, say), else its database flag (--memory-connection), else what the
    jobs file says, relative to it, else None. --<setting>-table renames the
    table either way. See project.resolveLocation.
    """

    jobsPath, _ = _resolveConfigurationPaths(arguments)

    return resolveLocation(jobsPath, setting, configured, path=getattr(arguments, setting, None),
                           connection=getattr(arguments, setting + '_connection', None), table=getattr(arguments, setting + '_table', None))


_loadYaml = loadYaml
_readJobs = readJobs
_describeLocation = describeLocation
_settingsFor = settingsFor
_history = openHistory
_requireAlias = requireAlias
_requireDatabase = requireDatabase
_Connections = Connections
_sourceQueryColumns = sourceQueryColumns
_sourceQuerySample = sourceQuerySample
_targetColumns = targetColumns
_checkColumnCounts = checkColumnCounts


def _refuseReadOnlyLocations(arguments: argparse.Namespace, jobsFile: DataJobsFile, connectionConfiguration: Dict[str, ConnectionConfig]) -> None:
    """Fails where run state, history or the manifest would be written to a
    readOnly connection, before anything runs: what `validate` and `run` ask.
    """

    locations = [_memoryLocation(arguments, jobsFile)] + [_resolveLocation(arguments, setting, getattr(jobsFile, setting))
                                                           for setting in ('history', 'manifest')]
    for location in locations:
        if isinstance(location, TableLocation):
            _settingsFor(location, connectionConfiguration)


def _memoryLocation(arguments: argparse.Namespace, jobsFile: DataJobsFile) -> Location:
    """Run state has a default where history and the manifest don't: memory.yaml beside the jobs file."""

    jobsPath, _ = _resolveConfigurationPaths(arguments)

    return _resolveLocation(arguments, 'memory', jobsFile.memory) or Path(os.path.normpath(jobsPath.parent / 'memory.yaml'))


def _memoryBackend(arguments: argparse.Namespace, jobsFile: DataJobsFile,
                   connectionConfiguration: Dict[str, ConnectionConfig]) -> Tuple[MemoryBackend, Path]:
    """The run memory to use, and the file a run holds as its lock, beside it.

    Run state in a table still needs a file for the lock, so it goes where a
    memory file would, and keeps overlapping runs apart on one machine only;
    across machines, let the scheduler do it (a CronJob's concurrencyPolicy:
    Forbid).
    """

    location = _memoryLocation(arguments, jobsFile)
    jobsPath, _ = _resolveConfigurationPaths(arguments)
    lockFile = lockFileFor(location, jobsPath, jobsFile.memory)

    if isinstance(location, TableLocation):
        return DatabaseMemory(connectionSettings=_settingsFor(location, connectionConfiguration), table=location.table or DEFAULT_TABLES['memory']), lockFile

    return FileMemory(memoryFile=location), lockFile


def _configureLogging(arguments: argparse.Namespace) -> Log:
    """stderr, where a container collects it; --log adds a file. --quiet
    leaves only errors on stderr, one line each.
    """

    level = getattr(logging, arguments.log_level.upper())
    log = Log(logFile=arguments.log, level=level, logFormat=arguments.log_format)

    if arguments.quiet:
        log.addErrorStream(stream=sys.stderr)
    else:
        log.addStreamHandler(stream=sys.stderr, level=level)

    return log


def _loadConnections(arguments: argparse.Namespace) -> Dict[str, ConnectionConfig]:

    _, connectionsPath = _resolveConfigurationPaths(arguments)

    return loadConnections(connectionsPath)


def _loadProject(arguments: argparse.Namespace) -> Project:
    """The configuration the flags name, loaded and validated."""

    jobsPath, connectionsPath = _resolveConfigurationPaths(arguments)

    return Project.load(directory=_configDirectory(arguments), jobsPath=jobsPath, connectionsPath=connectionsPath)


def _loadDataJobs(arguments: argparse.Namespace) -> Tuple[DataJobsFile, Dict[str, ConnectionConfig]]:

    project = _loadProject(arguments)

    return project.jobsFile, project.connections


def _selectJobs(jobs: Dict[str, DataJobConfig], requested: Optional[List[str]], log: Log) -> Dict[str, DataJobConfig]:
    """Narrow a job map to --job selections, naming each predecessor left out."""

    if not requested:
        return jobs

    unknown = [job for job in requested if job not in jobs]
    if unknown:
        raise UsageError('no such job(s): {}. Known jobs: {}'.format(', '.join(unknown), ', '.join(sorted(jobs))))

    selected = {name: jobs[name] for name in requested}

    for name, job in selected.items():
        ignored = [predecessor for predecessor in job.predecessors if predecessor not in selected]
        if ignored:
            log.logging.warning('--job {} is running without its predecessor(s): {}. They will NOT run, and {} may read stale upstream data'.format(
                name, ', '.join(ignored), name))

    return selected


def _toolVersion() -> str:

    from importlib.metadata import PackageNotFoundError, version

    try:
        return version('bauta')
    except PackageNotFoundError:
        return 'unknown'


def _checkMaskingCoverage(name: str, job: DataJobConfig, returned: Sequence[str], log: Log) -> Optional[str]:
    """Whether the job's masking policy covers every column its query returns."""

    return checkMaskingCoverage(name, job, returned)


def _writeOutput(text: str, output: Optional[str]) -> None:
    """stdout, or a file that must not already exist -- a generated proposal
    overwriting a reviewed jobs.yaml would lose the review.
    """

    if not output:
        sys.stdout.write(text)
        return

    path = Path(output)
    if path.exists():
        raise UsageError('{} already exists; choose another --output, or remove it first'.format(path))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print('wrote {}'.format(path))


def _discoveryRulesFile(arguments: argparse.Namespace) -> Tuple[Optional[Path], Any]:
    """The discovery.yaml in use and its validated rules: --rules, else one in
    the configuration directory if there is one, else (None, None).
    """

    from ..generate.discovery import DISCOVERY_FILE

    path = Path(arguments.rules) if arguments.rules else _configDirectory(arguments) / DISCOVERY_FILE
    if not arguments.rules and not path.exists():
        return None, None

    return path, Configuration.validateDiscoveryRules(_loadYaml(path), str(path))


def _discoveryRules(arguments: argparse.Namespace) -> Any:
    """What discover, audit and synthesize recognise personal data by: the
    built-in rules, with a discovery.yaml's ahead of them.
    """

    from ..generate.discovery import discoveryRules

    return discoveryRules(_discoveryRulesFile(arguments)[1])

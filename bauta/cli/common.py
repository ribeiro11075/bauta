"""What the commands share: finding and loading the configuration, where run
state, history and the manifest live, and one connection per alias.
"""
from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

import yaml

from ..configuration import (Configuration, ConfigurationError, DatabaseConnectionConfig, DataJobConfig, DataJobsFile, StorageLocation, TableLocation,
                             expandEnvironmentVariables)
from ..database import Database
from ..log import Log
from ..masking import MaskingError
from ..jobs.memory import DatabaseMemory, FileMemory, MemoryBackend


EXIT_SUCCESS = 0


EXIT_JOBS_DID_NOT_SUCCEED = 1


EXIT_BAD_CONFIGURATION = 2


EXIT_INTERRUPTED = 130


CONFIG_DIRECTORY_VARIABLE = 'BAUTA_CONFIG'


MANIFEST_KEY_VARIABLE = 'BAUTA_MANIFEST_KEY'


NOTIFY_URL_VARIABLE = 'BAUTA_NOTIFY_URL'


class UsageError(Exception):
    """A problem with how the command was invoked, rather than with a job."""


def _loadYaml(path: Path) -> Any:
    """Load a YAML file, expanding ${NAME} from the environment."""

    try:
        with open(path) as file:
            return expandEnvironmentVariables(yaml.safe_load(file))
    except FileNotFoundError as error:
        raise UsageError('no such file: {}'.format(path)) from error
    except yaml.YAMLError as error:
        raise UsageError('{} is not valid YAML: {}'.format(path, error)) from error


def _configDirectory(arguments: argparse.Namespace) -> Path:
    """--config, else $BAUTA_CONFIG, else ./configuration -- so the
    common case is a bare `bauta run`.
    """

    return Path(arguments.config or os.environ.get(CONFIG_DIRECTORY_VARIABLE) or 'configuration')


def _resolveConfigurationPaths(arguments: argparse.Namespace) -> Tuple[Path, Path]:
    """Explicit --jobs/--databases win; otherwise both come from the config directory."""

    configDirectory = _configDirectory(arguments)
    jobsPath = Path(arguments.jobs) if arguments.jobs else configDirectory / 'jobs.yaml'
    databasesPath = Path(arguments.databases) if arguments.databases else configDirectory / 'database.yaml'

    return jobsPath, databasesPath


Location = Union[Path, TableLocation]


DEFAULT_TABLES = {'memory': 'bauta_memory', 'history': 'bauta_history', 'manifest': 'bauta_manifest'}


def _resolveLocation(arguments: argparse.Namespace, setting: str, configured: Optional[StorageLocation] = None) -> Optional[Location]:
    """Where run state, history or the manifest goes: the setting's file flag
    (--memory, say), else its database flag (--memory-database), else what the
    jobs file says, else None. A file the jobs file names is relative to it,
    not the working directory, so cron, a shell and CI find the same one
    wherever they start. --<setting>-table renames the table either way.
    """

    fileFlag = getattr(arguments, setting, None)
    databaseFlag = getattr(arguments, setting + '_database', None)
    table = getattr(arguments, setting + '_table', None)

    if fileFlag:
        return Path(fileFlag)
    if databaseFlag:
        return TableLocation(database=databaseFlag, table=table or DEFAULT_TABLES[setting])
    if isinstance(configured, TableLocation):
        return TableLocation(database=configured.database, table=table or configured.table or DEFAULT_TABLES[setting])
    if configured:
        jobsPath, _ = _resolveConfigurationPaths(arguments)
        return Path(os.path.normpath(jobsPath.parent / configured))

    return None


def _describeLocation(location: Location) -> str:

    return str(location) if isinstance(location, Path) else 'table {} in {}'.format(location.table, location.database)


def _settingsFor(location: TableLocation, databaseConfiguration: Dict[str, DatabaseConnectionConfig]) -> DatabaseConnectionConfig:

    _requireAlias(databaseConfiguration, location.database)

    return databaseConfiguration[location.database]


def _memoryLocation(arguments: argparse.Namespace, jobsFile: DataJobsFile) -> Location:
    """Run state has a default where history and the manifest don't: memory.yaml beside the jobs file."""

    jobsPath, _ = _resolveConfigurationPaths(arguments)

    return _resolveLocation(arguments, 'memory', jobsFile.memory) or Path(os.path.normpath(jobsPath.parent / 'memory.yaml'))


def _memoryBackend(arguments: argparse.Namespace, jobsFile: DataJobsFile,
                   databaseConfiguration: Dict[str, DatabaseConnectionConfig]) -> Tuple[MemoryBackend, Path]:
    """The run memory to use, and the file a run holds as its lock, beside it.

    Run state in a table still needs a file for the lock, so it goes where a
    memory file would, and keeps overlapping runs apart on one machine only;
    across machines, let the scheduler do it (a CronJob's concurrencyPolicy:
    Forbid).
    """

    location = _memoryLocation(arguments, jobsFile)

    if isinstance(location, TableLocation):
        jobsPath, _ = _resolveConfigurationPaths(arguments)
        wouldBe = jobsFile.memory if isinstance(jobsFile.memory, str) else 'memory.yaml'
        memory = DatabaseMemory(connectionSettings=_settingsFor(location, databaseConfiguration), table=location.table or DEFAULT_TABLES['memory'])
        return memory, Path(os.path.normpath(jobsPath.parent / wouldBe)).with_name('memory.run.lock')

    return FileMemory(memoryFile=location), location.with_name(location.name + '.run.lock')


def _history(location: Location, databaseConfiguration: Dict[str, DatabaseConnectionConfig]) -> Any:

    from ..jobs.reporting import DatabaseHistory, FileHistory

    if isinstance(location, Path):
        return FileHistory(location)

    return DatabaseHistory(connectionSettings=_settingsFor(location, databaseConfiguration), table=location.table or DEFAULT_TABLES['history'])


def _configureLogging(arguments: argparse.Namespace) -> Log:
    """stderr unless --quiet, where a container collects it; --log adds a file."""

    level = getattr(logging, arguments.log_level.upper())
    log = Log(logFile=arguments.log, level=level, logFormat=arguments.log_format)

    if not arguments.quiet:
        log.addStreamHandler(stream=sys.stderr, level=level)

    return log


def _loadDatabases(arguments: argparse.Namespace) -> Dict[str, DatabaseConnectionConfig]:

    _, databasesPath = _resolveConfigurationPaths(arguments)

    return Configuration.validateDatabaseConfiguration(_loadYaml(databasesPath))


def _loadDataJobs(arguments: argparse.Namespace) -> Tuple[DataJobsFile, Dict[str, DatabaseConnectionConfig]]:

    jobsPath, databasesPath = _resolveConfigurationPaths(arguments)
    databaseConfiguration = Configuration.validateDatabaseConfiguration(_loadYaml(databasesPath))
    jobsFile = Configuration.validateJobConfiguration(_loadYaml(jobsPath), DataJobsFile)
    Configuration.validateJobGraph(jobsFile.jobs, databases=databaseConfiguration)

    unknown = ['{}: database "{}" is not a known database alias'.format(setting, location.database)
               for setting, location in jobsFile.tableLocations().items() if location.database not in databaseConfiguration]
    if unknown:
        raise ConfigurationError('Invalid configuration in {}:\n'.format(jobsPath) + '\n'.join(unknown))

    return jobsFile, databaseConfiguration


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


class _Connections:
    """One open connection per alias, for a command that asks many things of
    the same few databases. A dry run or `audit --connect` used to open one
    per question -- three or four a job, each running passwordCommand again.

    A connection whose statement failed is closed and forgotten, since
    PostgreSQL refuses anything more on it until rolled back; the next use
    opens a fresh one.
    """

    def __init__(self, databaseConfiguration: Mapping[str, DatabaseConnectionConfig]) -> None:
        self._settings = databaseConfiguration
        self._open: Dict[str, Database] = {}


    @contextlib.contextmanager
    def use(self, alias: str) -> Iterator[Database]:

        database = self._open.get(alias)
        if database is None:
            database = self._open[alias] = Database(connectionSettings=self._settings[alias])

        try:
            yield database
        except BaseException:
            self._open.pop(alias, None)
            with contextlib.suppress(Exception):
                database.close()
            raise


    def __enter__(self) -> '_Connections':

        return self


    def __exit__(self, *exception: Any) -> None:

        for database in self._open.values():
            with contextlib.suppress(Exception):
                database.close()
        self._open.clear()


def _sourceQueryColumns(job: DataJobConfig, connections: _Connections) -> List[str]:
    """The columns a job's sourceQuery returns, by reading and discarding one
    row, since rewriting arbitrary SQL isn't portable.
    """

    with connections.use(job.sourceDatabase) as database:
        query = job.sourceQuery
        parameters = None
        if job.watermarkColumn:
            query = database.substituteWatermarkPlaceholder(query)
            parameters = (job.watermarkInitial,)
        columns, chunks = database.stream(query=query, chunkSize=1, parameters=parameters)
        chunks.close()

    return columns


def _targetColumns(job: DataJobConfig, connections: _Connections) -> List[str]:
    """The target's columns, in the order a load fills them."""

    with connections.use(job.targetDatabase) as database:
        return database.getAllColumnNames(table=job.targetTableFinal)


def _checkColumnCounts(name: str, job: DataJobConfig, returned: Sequence[str], targetColumns: Sequence[str]) -> Optional[str]:
    """Whether sourceQuery returns as many columns as the load fills.

    `targetColumns` on the job names the ones it fills; without it the load
    fills every column the target has.
    """

    filled = list(job.targetColumns) if job.targetColumns else list(targetColumns)

    if len(returned) == len(filled):
        return None

    return ('{}: sourceQuery returns {} column(s) {} and the load fills {} in {} ({}). '
            'List the ones the query fills in targetColumns, in the query\'s order'.format(
                name, len(returned), list(returned), len(filled), job.targetTableFinal, ', '.join(filled)))


def _checkMaskingCoverage(name: str, job: DataJobConfig, returned: Sequence[str], log: Log) -> Optional[str]:
    """Whether the job's masking policy covers every column its query returns."""

    from ..masking import MaskingPlan

    assert job.masking is not None
    try:
        plan = MaskingPlan(key=job.masking.key.get_secret_value(), columns=job.masking.columns, defaultStrategy=job.masking.defaultStrategy)
        plan.bind(returned)
        log.logging.info('{}: masking policy covers all {} column(s), key {}'.format(name, len(returned), plan.fingerprint))
    except MaskingError as error:
        return '{}: {}'.format(name, error)

    return None


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


def _requireAlias(databaseConfiguration: Dict[str, DatabaseConnectionConfig], alias: str) -> None:

    if alias not in databaseConfiguration:
        raise UsageError('no database alias {!r}. Known aliases: {}'.format(alias, ', '.join(sorted(databaseConfiguration))))

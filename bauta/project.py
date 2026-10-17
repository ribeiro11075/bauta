"""A configuration directory as the `bauta` command reads it, for a program
that works with one the way the command does: a console, a scheduler's
plugin, a check of its own.

`Project.load()` reads jobs.yaml with its includes and connections.yaml,
validated as `bauta validate` validates them, and knows where run state,
history and the manifest are kept. The checks the commands make are methods
returning data rather than printing: `validationProblems()` and
`dryRunProblems()` as `validate` and `run --dry-run` find them, `audit()` and
`coverage()` as the reports their commands render.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import yaml

from .configuration import (Configuration, ConfigurationError, ConnectionConfig, DataJobConfig, DataJobsFile, FilesConnection, IcebergConnection,
                            InsertStrategy, JobsDocument, StorageLocation, TableLocation, expandEnvironmentVariables, isLake, parseYaml, readJobsFile)
from .database import DIALECTS, Database
from .database.sessions import Connections, sourceQueryColumns
from .log import LOGGER_NAME
from .log.scrubbing import describeError
from .masking import MaskingError

CONFIG_DIRECTORY_VARIABLE = 'BAUTA_CONFIG'
CONNECTIONS_FILE = 'connections.yaml'
JOBS_FILE = 'jobs.yaml'
DEFAULT_TABLES = {'memory': 'bauta_memory', 'history': 'bauta_history', 'manifest': 'bauta_manifest'}
# The lock a run holds, and the directory of live cycles beside it, take the
# run-state file's name with these in place of nothing.
LOCK_SUFFIX = '.run.lock'
RUNNING_SUFFIX = '.running'
# A file in jobs.yaml or a table in one of connections.yaml's aliases.
Location = Union[Path, TableLocation]

logger = logging.getLogger(LOGGER_NAME)


class UsageError(Exception):
    """A problem with how something was asked for, rather than with a job: a
    file that isn't there, an alias that doesn't exist, a choice left open.
    """


def loadYaml(path: Path) -> Any:
    """A YAML file, with ${NAME} expanded from the environment."""

    try:
        with open(path) as file:
            return expandEnvironmentVariables(parseYaml(file))
    except FileNotFoundError as error:
        raise UsageError('no such file: {}'.format(path)) from error
    except yaml.YAMLError as error:
        raise UsageError('{} is not valid YAML: {}'.format(path, error)) from error


def readJobs(jobsPath: Path) -> JobsDocument:
    """The jobs file with the files its `include` names merged in."""

    return readJobsFile(jobsPath, load=loadYaml)


def configDirectory(directory: Optional[Union[str, Path]] = None) -> Path:
    """`directory`, else $BAUTA_CONFIG, else ./configuration -- so the
    common case is a bare `bauta run`.
    """

    return Path(directory or os.environ.get(CONFIG_DIRECTORY_VARIABLE) or 'configuration')


def loadConnections(connectionsPath: Path) -> Dict[str, ConnectionConfig]:

    return Configuration.validateConnectionConfiguration(loadYaml(connectionsPath), directory=str(connectionsPath.parent))


def resolveLocation(jobsPath: Path, setting: str, configured: Optional[StorageLocation] = None, path: Optional[Union[str, Path]] = None,
                    connection: Optional[str] = None, table: Optional[str] = None) -> Optional[Location]:
    """Where run state, history or the manifest goes: `path`, else a table of
    `connection`, else what the jobs file says, else None. A file the jobs
    file names is relative to it, not the working directory, so cron, a shell
    and CI find the same one wherever they start. `table` renames the table
    either way.
    """

    if path:
        return Path(path)
    if connection:
        return TableLocation(connection=connection, table=table or DEFAULT_TABLES[setting])
    if isinstance(configured, TableLocation):
        return TableLocation(connection=configured.connection, table=table or configured.table or DEFAULT_TABLES[setting])
    if configured:
        return Path(os.path.normpath(jobsPath.parent / configured))

    return None


def lockFileFor(memoryLocation: Location, jobsPath: Path, configured: Optional[StorageLocation] = None) -> Path:
    """The file a run holds as its lock, beside its run state. Run state in a
    table still needs a file for the lock, so it goes where a memory file
    would, and keeps overlapping runs apart on one machine only; across
    machines, let the scheduler do it (a CronJob's concurrencyPolicy: Forbid).
    """

    if isinstance(memoryLocation, TableLocation):
        wouldBe = configured if isinstance(configured, str) else 'memory.yaml'
        return Path(os.path.normpath(jobsPath.parent / wouldBe)).with_name('memory' + LOCK_SUFFIX)

    return memoryLocation.with_name(memoryLocation.name + LOCK_SUFFIX)


def runningDirectoryFor(lockFile: Path) -> Path:
    """Where `run` keeps a file for each live cycle: beside its lock, named
    for it -- memory.yaml.running beside memory.yaml.run.lock. See jobs.status.
    """

    return lockFile.with_name(lockFile.name[:-len(LOCK_SUFFIX)] + RUNNING_SUFFIX)


def describeLocation(location: Location) -> str:

    return str(location) if isinstance(location, Path) else 'table {} in {}'.format(location.table, location.connection)


def settingsFor(location: TableLocation, connectionConfiguration: Mapping[str, ConnectionConfig], writing: bool = True) -> ConnectionConfig:
    """The connection a table location is in. One that is `readOnly` is
    refused for writing, naming what was to be kept there: run state on one
    failed later, as `no such table: bauta_memory`.
    """

    requireAlias(connectionConfiguration, location.connection)
    settings = connectionConfiguration[location.connection]
    if writing and getattr(settings, 'readOnly', False):
        raise UsageError('{} is in connection "{}", which is readOnly: keep it in a file, or in a table of another connection'.format(
            describeLocation(location), location.connection))

    return settings


def requireAlias(connectionConfiguration: Mapping[str, ConnectionConfig], alias: str) -> None:

    if alias not in connectionConfiguration:
        raise UsageError('no connection alias {!r}. Known aliases: {}'.format(alias, ', '.join(sorted(connectionConfiguration))))


def openHistory(location: Location, connectionConfiguration: Mapping[str, ConnectionConfig], writing: bool = True) -> Any:
    """The RunHistory kept at `location`."""

    from .jobs.reporting import DatabaseHistory, FileHistory

    if isinstance(location, Path):
        return FileHistory(location)

    return DatabaseHistory(connectionSettings=settingsFor(location, connectionConfiguration, writing), table=location.table or DEFAULT_TABLES['history'])


@dataclass(frozen=True)
class Project:
    """A jobs file and its connections, loaded and validated together.

    `jobsFile` is the validated DataJobsFile, `connections` each alias's
    ConnectionConfig, and `document` the jobs file as read: `files` every
    file its `include` brought in, `origins` the file each job came from.
    """

    jobsPath: Path
    connectionsPath: Path
    directory: Path
    jobsFile: DataJobsFile
    connections: Dict[str, ConnectionConfig]
    document: JobsDocument = field(repr=False)

    @classmethod
    def load(cls, directory: Optional[Union[str, Path]] = None, jobsPath: Optional[Union[str, Path]] = None,
             connectionsPath: Optional[Union[str, Path]] = None) -> 'Project':
        """The jobs.yaml and connections.yaml in `directory` (default: as
        configDirectory says), or the files named. Raises ConfigurationError
        for an invalid one, and UsageError for one that isn't there.
        """

        folder = configDirectory(directory)
        jobs = Path(jobsPath) if jobsPath else folder / JOBS_FILE
        connectionsFile = Path(connectionsPath) if connectionsPath else folder / CONNECTIONS_FILE

        connections = loadConnections(connectionsFile)
        document = readJobs(jobs)
        jobsFile = Configuration.validateJobConfiguration(document.content, DataJobsFile)
        Configuration.validateJobGraph(jobsFile.jobs, connections=connections)

        unknown = ['{}: connection "{}" is not a known connection alias'.format(setting, location.connection)
                   for setting, location in jobsFile.tableLocations().items() if location.connection not in connections]
        unknown += ['{}: connection "{}" is a files or Iceberg connection; {} is kept in a database table or a file'.format(
                        setting, location.connection, setting)
                    for setting, location in jobsFile.tableLocations().items() if isLake(connections.get(location.connection))]
        if unknown:
            raise ConfigurationError('Invalid configuration in {}:\n'.format(jobs) + '\n'.join(unknown))

        return cls(jobsPath=jobs, connectionsPath=connectionsFile, directory=folder, jobsFile=jobsFile, connections=connections, document=document)


    # Where things are kept --------------------------------------------------

    def location(self, setting: str) -> Optional[Location]:
        """Where the jobs file keeps `setting` -- 'memory', 'history' or
        'manifest' -- or None for history or a manifest it doesn't keep. Run
        state is always kept somewhere: memory.yaml beside the jobs file
        unless it says otherwise.
        """

        location = resolveLocation(self.jobsPath, setting, getattr(self.jobsFile, setting))
        if location is None and setting == 'memory':
            return Path(os.path.normpath(self.jobsPath.parent / 'memory.yaml'))

        return location


    def lockFile(self) -> Path:
        """The file `run` holds as its lock: see lockFileFor."""

        location = self.location('memory')
        assert location is not None

        return lockFileFor(location, self.jobsPath, self.jobsFile.memory)


    def memory(self) -> Any:
        """The run state `run` keeps, for reading: a MemoryBackend, to use in
        a `with` block.
        """

        from .jobs.memory import DatabaseMemory, FileMemory

        location = self.location('memory')
        if isinstance(location, TableLocation):
            return DatabaseMemory(connectionSettings=settingsFor(location, self.connections, writing=False),
                                  table=location.table or DEFAULT_TABLES['memory'])
        assert location is not None

        return FileMemory(memoryFile=location)


    def schedule(self) -> List[Dict[str, Any]]:
        """Where each job stands, as `bauta jobs` shows it: `due`,
        `throttled` inside its `refresh` window, or `inactive`; what it waits
        for; when it last ran and is next due, in seconds since 1970; and its
        watermark. See jobs.dependencyGraph.jobSchedule.
        """

        from .jobs.dependencyGraph import jobSchedule

        with self.memory() as memory:
            return jobSchedule(self.jobsFile.jobs, memory.read(), memory.readWatermarks())


    def running(self) -> List[Dict[str, Any]]:
        """What `run` is doing now, on this machine or another sharing the
        directory: a record per live cycle, the latest started first. Empty
        if nothing is running. See jobs.status.readRunStatuses.
        """

        from .jobs.status import readRunStatuses

        return readRunStatuses(runningDirectoryFor(self.lockFile()))


    def history(self) -> Any:
        """The RunHistory `run` records into, for reading, or None if the jobs
        file keeps none.
        """

        location = self.location('history')

        return None if location is None else openHistory(location, self.connections, writing=False)


    def readManifest(self, runId: Optional[str] = None) -> Optional[Tuple[str, Dict[str, Any]]]:
        """The manifest `run` writes, as (where it was read from, its JSON):
        from a file, or from a table the latest run's unless `runId` names
        one. None if the jobs file keeps none or none has been written yet.
        """

        import json

        from .jobs.reporting import DatabaseManifests

        location = self.location('manifest')
        if location is None:
            return None

        if isinstance(location, Path):
            if runId is not None:
                raise UsageError('runId picks a manifest from a table, not a file')
            if not location.exists():
                return None
            return str(location), json.loads(location.read_text())

        try:
            readRun, manifest = DatabaseManifests(settingsFor(location, self.connections, writing=False),
                                                  table=location.table or DEFAULT_TABLES['manifest']).read(runId)
        except KeyError:
            return None

        return 'run {} in {}'.format(readRun, describeLocation(location)), manifest


    def discoveryRulesFile(self, rulesPath: Optional[Union[str, Path]] = None) -> Tuple[Optional[Path], Any]:
        """The discovery.yaml in use and its validated rules: `rulesPath`, else
        one in the configuration directory if there is one, else (None, None).
        """

        from .generate.discovery import DISCOVERY_FILE

        path = Path(rulesPath) if rulesPath else self.directory / DISCOVERY_FILE
        if not rulesPath and not path.exists():
            return None, None

        return path, Configuration.validateDiscoveryRules(loadYaml(path), str(path))


    def discoveryRules(self, rulesPath: Optional[Union[str, Path]] = None) -> Any:
        """What discover, audit and synthesize recognise personal data by: the
        built-in rules, with a discovery.yaml's ahead of them.
        """

        from .generate.discovery import discoveryRules

        return discoveryRules(self.discoveryRulesFile(rulesPath)[1])


    # Checks -----------------------------------------------------------------

    def validationProblems(self) -> List[str]:
        """What `bauta validate` finds beyond what loading already refused,
        without connecting to anything: transformers that don't resolve,
        connection settings a driver would refuse, and masking settings the
        machine can't honour. Empty if there is nothing.
        """

        from .masking import effectiveMaskingThreads, requireNativeProblem
        from .transform import resolveTransformer

        problems = []
        for name, job in self.jobsFile.jobs.items():
            for column, references in job.sourceQueryColumnTransforms.items():
                for reference in references:
                    try:
                        resolveTransformer(reference)
                    except Exception as error:
                        problems.append('{}: {} -> {}'.format(name, column, error))

        for alias, settings in sorted(self.connections.items()):
            if isLake(settings):
                continue
            assert not isinstance(settings, (FilesConnection, IcebergConnection))
            try:
                DIALECTS[settings.type].connectArguments(settings, resolvePassword=False)
            except ConfigurationError as error:
                problems.append('{}: {}'.format(alias, error))

        try:
            effectiveMaskingThreads(self.jobsFile.maskingThreads)
        except ValueError as error:
            problems.append(str(error))
        if any(job.masking is not None and job.active for job in self.jobsFile.jobs.values()):
            problem = requireNativeProblem(self.jobsFile.requireNative)
            if problem:
                problems.append(problem)

        return problems


    def dryRunProblems(self, jobs: Optional[Mapping[str, DataJobConfig]] = None) -> List[str]:
        """What `run --dry-run` finds for `jobs` (default: every job): see
        dryRunProblems.
        """

        return dryRunProblems(self.jobsFile.jobs if jobs is None else jobs, self.connections)


    def audit(self, jobs: Optional[Sequence[str]] = None, connect: bool = False, sample: int = 1000, rulesPath: Optional[Union[str, Path]] = None,
              rules: Any = None) -> Dict[str, Any]:
        """The audit report, as `bauta audit --format json` writes it, for
        `jobs` (default: all of them). Offline unless `connect`, which also
        runs each query for its real columns and up to `sample` of its rows,
        and asks each connection whether it is encrypted.
        """

        from .review.audit import ConnectedFacts, auditJobs
        from .review.connected import connectedFacts

        selected = selectJobs(self.jobsFile.jobs, jobs)
        facts = connectedFacts(selected, self.connections, sample) if connect else ConnectedFacts()

        return auditJobs(selected, facts, connections=self.connections, rules=rules if rules is not None else self.discoveryRules(rulesPath))


    def drift(self, jobs: Optional[Sequence[str]] = None, sample: int = 1000, maskKeys: bool = False, rulesPath: Optional[Union[str, Path]] = None,
              rules: Any = None) -> List[Any]:
        """What `discover --update` finds: each masked job whose policy no
        longer names exactly the columns its sourceQuery returns, as a
        JobDrift with a proposal for each new column and the gone ones. Reads
        up to `sample` rows of each query, which stay in memory. Empty if no
        job has drifted. `generate.drift.applyDrift` writes one into its file.
        """

        return _driftOf(self, jobs, sample, maskKeys, rules if rules is not None else self.discoveryRules(rulesPath))


    def verifyReferences(self, jobs: Optional[Sequence[str]] = None) -> Tuple[List[Any], List[Any]]:
        """What `bauta verify-references` finds for the active jobs, or `jobs`:
        for each key that applies to a copy -- the target's own, and those of
        the sources copied into it -- the rows whose reference points at
        nothing, as ReferenceResults; and an UncheckedNote for each target
        where no key applied, naming the schemas that do declare some. Reads
        the sources' catalogs only, never their rows.
        """

        from .database.dialects import bareName, unqualifiedName
        from .review.connected import readForeignKeys, schemasUsed
        from .review.references import uncheckedNote, verifyReferences

        selected = selectJobs(self.jobsFile.jobs, jobs)
        if not jobs:
            selected = {name: job for name, job in selected.items() if job.active}
        keysByAlias: Dict[str, List[Any]] = {}
        elsewhere: Dict[str, Dict[str, int]] = {}
        for alias in sorted({job.sourceConnection for job in selected.values()}):
            try:
                with Database(connectionSettings=self.connections[alias]) as database:
                    keysByAlias[alias], elsewhere[alias] = readForeignKeys(database, schemasUsed(selected, alias, targets=False))
            except Exception as error:
                logger.warning('{}: could not read foreign keys, so only the target\'s own are checked -- {}'.format(alias, describeError(error)))

        results: List[Any] = []
        notes: List[Any] = []
        for target in sorted({job.targetConnection for job in selected.values() if not isLake(self.connections[job.targetConnection])}):
            targetJobs = [job for job in selected.values() if job.targetConnection == target]
            # Keyed bare, as the catalogs report names: a job naming a reserved
            # word writes it quoted, and a quoted key would match nothing.
            loaded = {bareName(unqualifiedName(job.targetTableFinal)).upper(): job.targetTableFinal for job in targetJobs}
            sources = sorted({job.sourceConnection for job in targetJobs} - {target})
            sourceKeys = [foreignKey for alias in sources for foreignKey in keysByAlias.get(alias, [])]
            with Database(connectionSettings=self.connections[target]) as database:
                checked = verifyReferences(database, target, loaded, sourceKeys)
                if not checked:
                    # Nothing to count is a pass only where there is nothing to
                    # find: keys declared in a schema these jobs don't use mean
                    # the check looked in the wrong place.
                    missed = {target: database.foreignKeysElsewhere(schemasUsed(selected, target, sources=False))}
                    missed.update({alias: elsewhere.get(alias, {}) for alias in sources})
                    notes.append(uncheckedNote(target, {alias: counts for alias, counts in missed.items() if counts}))
            results.extend(checked)

        return results, notes


    def coverage(self, connection: Optional[str] = None, schema: Optional[str] = None, jobs: Optional[Sequence[str]] = None,
                 rulesPath: Optional[Union[str, Path]] = None, rules: Any = None) -> Dict[str, Any]:
        """The coverage report, as `bauta coverage --format json` writes it:
        every table in `connection`'s schema (default: the one every job reads
        from) and what the active jobs, or `jobs`, do with each.
        """

        from .review.coverage import coverageReport, jobsReading

        selected = selectJobs(self.jobsFile.jobs, jobs)
        if not jobs:
            selected = {name: job for name, job in selected.items() if job.active}
        alias = connection or theOnlySourceConnection(selected)
        requireDatabase(self.connections, alias)

        with Database(connectionSettings=self.connections[alias]) as database:
            tables = database.listTables(schema=schema)
            # Only for the tables nothing covers: the rest are already accounted
            # for, and reading every column of a whole schema is not free.
            covered = {table for table in tables if jobsReading(table, alias, selected)}
            columns = {}
            for table in tables:
                if table in covered:
                    continue
                try:
                    columns[table] = database.getAllColumnNames(table=table)
                except Exception as error:
                    logger.warning('{}: could not read its columns -- {}'.format(table, describeError(error)))

        return coverageReport(alias, tables, selected, acknowledged=self.jobsFile.acknowledged.get(alias), columns=columns,
                              rules=rules if rules is not None else self.discoveryRules(rulesPath))


def _driftOf(project: 'Project', jobs: Optional[Sequence[str]], sample: int, maskKeys: bool, rules: Any) -> List[Any]:

    from .generate.drift import _wholeTable, findDrift

    selected = {name: job for name, job in selectJobs(project.jobsFile.jobs, jobs).items()
                if job.masking is not None and not isLake(project.connections.get(job.sourceConnection))}
    drifts = []
    for alias in sorted({job.sourceConnection for job in selected.values()}):
        requireDatabase(project.connections, alias)
        with Database(connectionSettings=project.connections[alias]) as database:
            # The keys of each schema a job reads whole, where a new key
            # column finds the domain its reference shares.
            wholeTables = [table for table in (_wholeTable(job.sourceQuery) for job in selected.values() if job.sourceConnection == alias) if table]
            try:
                foreignKeys = database.getForeignKeysFor(wholeTables)
            except NotImplementedError:
                foreignKeys = []
            for name in sorted(name for name, job in selected.items() if job.sourceConnection == alias):
                drift = findDrift(database, name, selected[name], rules=rules, sampleSize=sample, maskKeys=maskKeys, foreignKeys=foreignKeys)
                if drift.any():
                    drifts.append(drift)

    return drifts


def selectJobs(jobs: Dict[str, DataJobConfig], requested: Optional[Sequence[str]]) -> Dict[str, DataJobConfig]:
    """`jobs` narrowed to the ones `requested` names, or all of them."""

    if not requested:
        return jobs

    unknown = [job for job in requested if job not in jobs]
    if unknown:
        raise UsageError('no such job(s): {}. Known jobs: {}'.format(', '.join(unknown), ', '.join(sorted(jobs))))

    return {name: jobs[name] for name in requested}


def theOnlySourceConnection(jobs: Mapping[str, Any]) -> str:
    """The alias every job reads from, when no connection is named."""

    aliases = sorted({job.sourceConnection for job in jobs.values()})

    if len(aliases) != 1:
        raise UsageError('--connection is required: the jobs read from {}'.format(
            ', '.join(aliases) if aliases else 'no database'))

    return aliases[0]


def requireDatabase(connectionConfiguration: Mapping[str, ConnectionConfig], alias: str) -> Any:
    """The alias's settings, which must be a database's: the commands that
    read catalogs or generate tables have nothing to ask of a directory.
    """

    requireAlias(connectionConfiguration, alias)
    settings = connectionConfiguration[alias]
    if isLake(settings):
        raise UsageError('{} is a {} connection; this command works with databases. A job writing to it names it as its '
                         'targetConnection'.format(alias, settings.type.value))
    assert not isinstance(settings, (FilesConnection, IcebergConnection))

    return settings


# Dry run ----------------------------------------------------------------------

def checkColumnCounts(name: str, job: DataJobConfig, returned: Sequence[str], targetColumns: Sequence[str]) -> Optional[str]:
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


def checkMaskingCoverage(name: str, job: DataJobConfig, returned: Sequence[str]) -> Optional[str]:
    """Whether the job's masking policy covers every column its query returns."""

    from .masking import MaskingPlan

    assert job.masking is not None
    try:
        plan = MaskingPlan(key=job.masking.key.get_secret_value(), columns=job.masking.columns, defaultStrategy=job.masking.defaultStrategy)
        plan.bind(returned)
        logger.info('{}: masking policy covers all {} column(s), key {}'.format(name, len(returned), plan.fingerprint))
    except MaskingError as error:
        return '{}: {}'.format(name, error)

    return None


def _checkTargetTables(name: str, job: DataJobConfig, connections: Connections, problems: List[str]) -> Optional[List[str]]:
    """A database target's columns, having checked it is there, keyed for an
    upsert, and has a stage table holding them all; None, with the problem
    added, when the target can't be read.
    """

    try:
        with connections.use(job.targetConnection) as database:
            columns = database.getAllColumnNames(table=job.targetTableFinal)
            logger.info('{}: target {} has {} column(s)'.format(name, job.targetTableFinal, len(columns)))

            if job.insertStrategy == InsertStrategy.UPSERT and not database.getPrimaryColumnNames(table=job.targetTableFinal):
                problems.append('{}: target {} has no primary key, so insertStrategy: upsert cannot match rows'.format(name, job.targetTableFinal))
    except Exception as error:
        problems.append('{}: target {} is not readable -- {}'.format(name, job.targetTableFinal, describeError(error)))
        return None

    # The stage table is where the rows actually land, so a run fails at
    # once without it.
    if job.targetTableStage:
        try:
            with connections.use(job.targetConnection) as database:
                staged = {column.upper() for column in database.getAllColumnNames(table=job.targetTableStage)}
            logger.info('{}: stage table {} has {} column(s)'.format(name, job.targetTableStage, len(staged)))
            missing = [column for column in columns if column.upper() not in staged]
            if missing:
                problems.append('{}: stage table {} is missing column(s) {}, which the load writes'.format(
                    name, job.targetTableStage, ', '.join(missing)))
        except Exception as error:
            problems.append('{}: stage table {} is not readable -- {}'.format(name, job.targetTableStage, describeError(error)))

    return columns


def dryRunProblems(jobs: Mapping[str, DataJobConfig], connectionConfiguration: Mapping[str, ConnectionConfig]) -> List[str]:
    """Everything that needs a connection to find, moving no rows: that each
    alias connects or can be written, target tables exist, upsert targets
    have a primary key, and each query's columns fit its target and masking
    policy. Empty if there is nothing. What it checks is logged as it goes.
    """

    problems: List[str] = []
    unreachable = set()
    aliases = sorted({job.sourceConnection for job in jobs.values()} | {job.targetConnection for job in jobs.values()})

    with Connections(connectionConfiguration) as connections:

        for alias in aliases:
            settings = connectionConfiguration[alias]
            if isLake(settings):
                from .lake import checkWritable

                try:
                    checkWritable(settings)
                    logger.info('{}: writable ({})'.format(alias, settings.describeTarget()))
                except Exception as error:
                    unreachable.add(alias)
                    problems.append('{}: cannot write to {} -- {}'.format(alias, settings.describeTarget(), describeError(error)))
                continue
            try:
                with connections.use(alias) as database:
                    encrypted = {True: 'encrypted', False: 'NOT encrypted', None: 'encryption unknown'}[database.isEncrypted()]
                    logger.info('{}: connected ({}, {})'.format(alias, connectionConfiguration[alias].type.value, encrypted))
            except Exception as error:
                unreachable.add(alias)
                problems.append('{}: cannot connect to {} -- {}'.format(
                    alias, connectionConfiguration[alias].describeTarget(), describeError(error)))

        for name, job in jobs.items():
            if job.targetConnection in unreachable:
                continue

            # A lake has no table to read before the first run: it takes the
            # columns the query returns.
            lake = isLake(connectionConfiguration[job.targetConnection])
            columns = None if lake else _checkTargetTables(name, job, connections, problems)
            if (not lake and columns is None) or job.sourceConnection in unreachable:
                continue

            try:
                returned = sourceQueryColumns(job, connections)
            except Exception as error:
                problems.append('{}: sourceQuery could not be checked -- {}'.format(name, describeError(error)))
                continue

            # The same comparison the load makes, made before it writes: a target
            # that gained or lost a column against a query that didn't is the
            # ordinary way a working job stops working.
            problem = checkColumnCounts(name, job, returned, returned if columns is None else columns)
            if problem is None and job.masking is not None:
                problem = checkMaskingCoverage(name, job, returned)
            if problem is None and job.partitions is not None and job.partitions.column is not None:
                from .jobs.partitions import resolveColumn

                try:
                    resolveColumn(job.partitions.column, returned)
                except ConfigurationError as error:
                    problem = '{}: {}'.format(name, error)
            if problem:
                problems.append(problem)

    return problems

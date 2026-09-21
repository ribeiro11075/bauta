"""The commands that run jobs and look after their state: run, validate,
jobs, history and verify-manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..configuration import ConfigurationError, DatabaseConnectionConfig, DataJobsFile, InsertStrategy
from ..database import DIALECTS
from ..jobs.dependencyGraph import DependencyGraph
from ..log import Log
from ..masking import keyFingerprint, sealManifest, verifyManifest
from ..jobs.memory import RunInProgressError, exclusiveRun
from ..jobs.runner import RunResult, runDataJobs
from ..log.scrubbing import describeError
from .common import (DEFAULT_TABLES, EXIT_INTERRUPTED, EXIT_JOBS_DID_NOT_SUCCEED, EXIT_SUCCESS, NOTIFY_URL_VARIABLE, _Connections, Location,
                     UsageError, _checkColumnCounts, _checkMaskingCoverage, _describeLocation, _discoveryRulesFile, _history, _loadDatabases,
                     _loadDataJobs, _memoryBackend, _memoryLocation, _resolveConfigurationPaths, _resolveLocation, _selectJobs, _settingsFor,
                     _sourceQueryColumns, _toolVersion)


def _cycleReporter(arguments: argparse.Namespace, jobsFile: DataJobsFile, databaseConfiguration: Dict[str, DatabaseConnectionConfig],
                   log: Log) -> Optional[Callable[[RunResult], None]]:
    """What `run` does as each cycle ends: history and notifications, as the
    flags and the jobs file ask. None if they ask for nothing.
    """

    from ..jobs.reporting import RunHistory, newRunId, notify

    historyLocation = _resolveLocation(arguments, 'history', jobsFile.history)
    history: Optional[RunHistory] = _history(historyLocation, databaseConfiguration) if historyLocation else None

    notifyUrl = arguments.notify_url or os.environ.get(NOTIFY_URL_VARIABLE)

    if not (history or notifyUrl):
        return None

    def attempt(what: str, step: Callable[[], Any]) -> None:
        # Each is independent: one failing mustn't cost the others.
        try:
            step()
        except Exception as error:
            log.logging.error('Could not {}: {}'.format(what, describeError(error)))

    def report(result: RunResult) -> None:
        if history is not None:
            attempt('record the run history', lambda: history.append(result, newRunId()))
        if notifyUrl:
            attempt('send the notification', lambda: notify(notifyUrl, result, always=arguments.notify_on == 'always'))

    return report


def _applyJobSelection(jobsFile: DataJobsFile, arguments: argparse.Namespace, log: Log) -> DataJobsFile:
    """--job also forces the selected jobs to run, ignoring `refresh`."""

    jobs = _selectJobs(jobsFile.jobs, arguments.job, log)

    if arguments.job or arguments.force:
        jobs = {name: job.model_copy(update={'refresh': None}) for name, job in jobs.items()}

    return jobsFile.model_copy(update={'jobs': jobs, 'workers': arguments.workers or jobsFile.workers})


def _reportRun(result: RunResult, log: Log) -> int:

    for outcome in result.completed:
        log.logging.info('{}: completed in {:.1f}s, {} row(s)'.format(outcome.job, outcome.durationSeconds, outcome.rowCount),
                          extra={'job': outcome.job, 'status': outcome.status.value, 'rowCount': outcome.rowCount,
                                 'durationSeconds': round(outcome.durationSeconds, 3), 'attempts': outcome.attempts})

    if result.interrupted:
        return EXIT_INTERRUPTED

    if result.succeeded:
        return EXIT_SUCCESS

    return EXIT_JOBS_DID_NOT_SUCCEED


def _commandRun(arguments: argparse.Namespace, log: Log) -> int:
    """Holds a lock beside the memory file for the whole run, so an overlapping
    invocation -- a cron interval shorter than a slow run -- exits instead of
    running the same jobs concurrently.
    """

    jobsFile, databaseConfiguration = _loadDataJobs(arguments)
    jobsFile = _applyJobSelection(jobsFile, arguments, log)

    if arguments.dry_run:
        return _dryRunDataJobs(jobsFile, databaseConfiguration, log)

    memory, lockFile = _memoryBackend(arguments, jobsFile, databaseConfiguration)
    lockFile.parent.mkdir(parents=True, exist_ok=True)

    try:
        with memory, exclusiveRun(lockFile):
            result = runDataJobs(jobsFile=jobsFile, databaseConfiguration=databaseConfiguration, logFile=arguments.log,
                                 memory=memory, runForever=arguments.forever,
                                 logLevel=getattr(logging, arguments.log_level.upper()), logFormat=arguments.log_format,
                                 acceptKeyChange=arguments.accept_key_change,
                                 onCycle=_cycleReporter(arguments, jobsFile, databaseConfiguration, log))
    except RunInProgressError as error:
        log.logging.error(str(error))
        return EXIT_JOBS_DID_NOT_SUCCEED

    manifestLocation = _resolveLocation(arguments, 'manifest', jobsFile.manifest)
    if manifestLocation:
        _writeManifest(manifestLocation, result, jobsFile, databaseConfiguration, arguments, log)

    return _reportRun(result, log)


def _writeManifest(location: Location, result: RunResult, jobsFile: DataJobsFile, databaseConfiguration: Dict[str, DatabaseConnectionConfig],
                   arguments: argparse.Namespace, log: Log) -> None:
    """Writes the run's masking manifest as sealed JSON, to a file or a table,
    even when a job failed, with the tool version and a digest of the jobs
    file. Signed when the signing key's variable is set.
    """

    jobsPath, _ = _resolveConfigurationPaths(arguments)
    manifest = result.maskingManifest(jobsFile.jobs)
    manifest.update(tool={'name': 'bauta', 'version': _toolVersion()},
                    configuration={'jobsFile': str(jobsPath), 'sha256': hashlib.sha256(jobsPath.read_bytes()).hexdigest()})

    signingKey = os.environ.get(arguments.manifest_key_variable)
    manifest = sealManifest(manifest, signingKey=signingKey)

    if isinstance(location, Path):
        location.parent.mkdir(parents=True, exist_ok=True)
        location.write_text(json.dumps(manifest, indent=2) + '\n')
        where = str(location)
    else:
        from ..jobs.reporting import DatabaseManifests, newRunId

        runId = newRunId()
        DatabaseManifests(_settingsFor(location, databaseConfiguration), table=location.table or DEFAULT_TABLES['manifest']).write(manifest, runId)
        where = '{}, run {}'.format(_describeLocation(location), runId)

    log.logging.info('Wrote the masking manifest for {} job(s) to {}, {}'.format(
        len(manifest['jobs']), where, 'signed' if signingKey else 'unsigned (set ${} to sign it)'.format(arguments.manifest_key_variable)))


def _readManifest(arguments: argparse.Namespace) -> Tuple[str, Dict[str, Any]]:
    """The manifest to verify, and what to call it: the file named, else from
    --manifest-database, else from wherever the jobs file's `manifest` says --
    from a table, the latest run's unless --run names one.
    """

    location: Optional[Location]
    if arguments.manifest:
        location = Path(arguments.manifest)
        databaseConfiguration: Dict[str, DatabaseConnectionConfig] = {}
    elif arguments.manifest_database:
        location = _resolveLocation(arguments, 'manifest')
        databaseConfiguration = _loadDatabases(arguments)
    else:
        jobsFile, databaseConfiguration = _loadDataJobs(arguments)
        location = _resolveLocation(arguments, 'manifest', jobsFile.manifest)
        if location is None:
            raise UsageError('name the manifest to verify: a FILE, --manifest-database ALIAS, or `manifest` in the jobs file')

    if isinstance(location, Path):
        if arguments.run:
            raise UsageError('--run picks a manifest from a table, not a file')
        try:
            return str(location), json.loads(location.read_text())
        except FileNotFoundError as error:
            raise UsageError('no such file: {}'.format(location)) from error
        except ValueError as error:
            raise UsageError('{} is not valid JSON: {}'.format(location, error)) from error

    from ..jobs.reporting import DatabaseManifests

    assert location is not None
    try:
        runId, manifest = DatabaseManifests(_settingsFor(location, databaseConfiguration),
                                            table=location.table or DEFAULT_TABLES['manifest']).read(arguments.run)
    except KeyError as error:
        raise UsageError(error.args[0]) from error
    except ValueError as error:
        raise UsageError('the manifest in {} is not valid JSON: {}'.format(_describeLocation(location), error)) from error

    return 'run {} in {}'.format(runId, _describeLocation(location)), manifest


def _commandVerifyManifest(arguments: argparse.Namespace, log: Log) -> int:
    """Checks a manifest's digest, and its signature if it has one -- which
    needs the key, or it's a usage error.
    """

    path, manifest = _readManifest(arguments)

    signingKey = os.environ.get(arguments.manifest_key_variable)

    try:
        verification = verifyManifest(manifest, signingKey=signingKey)
    except ValueError as error:
        log.logging.error('{}: {}'.format(path, error))
        return EXIT_JOBS_DID_NOT_SUCCEED

    if not verification.digestValid:
        log.logging.error('{}: the digest does not match -- the manifest was changed after it was written'.format(path))
        return EXIT_JOBS_DID_NOT_SUCCEED

    if not verification.signed:
        if signingKey is not None:
            # With a key to check against, a signature is expected, and a
            # missing one is how an edited manifest would pass: strip it,
            # recompute the digest.
            log.logging.error('{}: not signed, though ${} is set to verify a signature -- a signed manifest may have had its '
                              'signature removed'.format(path, arguments.manifest_key_variable))
            return EXIT_JOBS_DID_NOT_SUCCEED
        print('{}: intact. It is not signed, so this shows only that it is unchanged, not who wrote it.'.format(path))
        return EXIT_SUCCESS

    if signingKey is None:
        raise UsageError('{} is signed with key {}; set ${} to verify the signature'.format(
            path, verification.signingKeyFingerprint, arguments.manifest_key_variable))

    if not verification.signatureValid:
        log.logging.error('{}: the signature is not valid for key {} -- it was signed with key {}, or altered'.format(
            path, keyFingerprint(signingKey), verification.signingKeyFingerprint))
        return EXIT_JOBS_DID_NOT_SUCCEED

    print('{}: intact, and signed with key {}.'.format(path, verification.signingKeyFingerprint))

    return EXIT_SUCCESS


def _commandValidate(arguments: argparse.Namespace, log: Log) -> int:
    """Offline checks only: config schema, the job graph, and that every
    transformer reference resolves. No connection is opened, so this is safe in
    CI and in a pre-commit hook. `run --dry-run` is the online counterpart.
    """

    from ..transform import resolveTransformer

    jobsFile, databaseConfiguration = _loadDataJobs(arguments)

    problems = []
    for name, job in jobsFile.jobs.items():
        for column, references in job.sourceQueryColumnTransforms.items():
            for reference in references:
                try:
                    resolveTransformer(reference)
                except Exception as error:
                    problems.append('{}: {} -> {}'.format(name, column, error))

    for alias, settings in sorted(databaseConfiguration.items()):
        try:
            DIALECTS[settings.type].connectArguments(settings, resolvePassword=False)
        except ConfigurationError as error:
            problems.append('{}: {}'.format(alias, error))

    from ..masking import effectiveMaskingThreads

    try:
        effectiveMaskingThreads(jobsFile.maskingThreads)
    except ValueError as error:
        problems.append(str(error))

    if problems:
        raise ConfigurationError('invalid configuration:\n' + '\n'.join(problems))

    print('configuration is valid: {} database alias(es), {} job(s)'.format(len(databaseConfiguration), len(jobsFile.jobs)))
    print('run state: {}'.format(_describeLocation(_memoryLocation(arguments, jobsFile))))
    for setting, missing in (('history', 'not recorded'), ('manifest', 'not written')):
        location = _resolveLocation(arguments, setting, getattr(jobsFile, setting))
        print('{}: {}'.format(setting, _describeLocation(location) if location else missing))

    from ..masking import MASKING_THREADS_VARIABLE, availableCores, maskingThreadsFor, nativeVersion

    if any(job.masking is not None for job in jobsFile.jobs.values()):
        if nativeVersion() is None:
            print('masking: in Python, one thread per job (pip install "bauta[native]" to use more)')
        else:
            concurrent = max(1, min(jobsFile.workers, sum(job.active for job in jobsFile.jobs.values())))
            source = '${}={}'.format(MASKING_THREADS_VARIABLE, os.environ[MASKING_THREADS_VARIABLE]) if os.environ.get(MASKING_THREADS_VARIABLE) \
                else 'maskingThreads: {}'.format(jobsFile.maskingThreads)
            shared, alone = maskingThreadsFor(jobsFile.maskingThreads, concurrent), maskingThreadsFor(jobsFile.maskingThreads, 1)
            print('masking: bauta-rs {}, {} thread(s) per job ({}; {} core(s)){}'.format(
                nativeVersion(), shared if shared == alone else '{} to {}'.format(shared, alone), source, availableCores(),
                '' if shared == alone else ': {} with {} jobs running, {} for a job running alone'.format(shared, concurrent, alone)))

    rulesPath, rulesFile = _discoveryRulesFile(arguments)
    if rulesFile is None:
        print('discovery rules: built-in')
    else:
        builtins = 'none built-in' if not rulesFile.builtins else \
            'built-in except {}'.format(', '.join(rulesFile.exclude)) if rulesFile.exclude else 'then the built-in ones'
        print('discovery rules: {} ({} name, {} value), {}'.format(rulesPath, len(rulesFile.names), len(rulesFile.values), builtins))

    return EXIT_SUCCESS


def _dryRunDataJobs(jobsFile: DataJobsFile, databaseConfiguration: Dict[str, DatabaseConnectionConfig], log: Log) -> int:
    """Everything `validate` does, plus what needs a connection: that each alias
    connects, target tables exist, and upsert targets have a primary key.
    """

    problems: List[str] = []
    unreachable = set()
    aliases = sorted({job.sourceDatabase for job in jobsFile.jobs.values()} | {job.targetDatabase for job in jobsFile.jobs.values()})

    with _Connections(databaseConfiguration) as connections:

        for alias in aliases:
            try:
                with connections.use(alias) as database:
                    encrypted = {True: 'encrypted', False: 'NOT encrypted', None: 'encryption unknown'}[database.isEncrypted()]
                    log.logging.info('{}: connected ({}, {})'.format(alias, databaseConfiguration[alias].type.value, encrypted))
            except Exception as error:
                unreachable.add(alias)
                problems.append('{}: cannot connect to {} -- {}'.format(
                    alias, databaseConfiguration[alias].describeTarget(), describeError(error)))

        for name, job in jobsFile.jobs.items():
            if job.targetDatabase in unreachable:
                continue
            try:
                with connections.use(job.targetDatabase) as database:
                    columns = database.getAllColumnNames(table=job.targetTableFinal)
                    log.logging.info('{}: target {} has {} column(s)'.format(name, job.targetTableFinal, len(columns)))

                    if job.insertStrategy == InsertStrategy.UPSERT and not database.getPrimaryColumnNames(table=job.targetTableFinal):
                        problems.append('{}: target {} has no primary key, so insertStrategy: upsert cannot match rows'.format(name, job.targetTableFinal))
            except Exception as error:
                problems.append('{}: target {} is not readable -- {}'.format(name, job.targetTableFinal, describeError(error)))
                continue

            # The stage table is where the rows actually land, so a run fails at
            # once without it -- which a dry run used to pass, looking only at the
            # table the job names as its target.
            if job.targetTableStage:
                try:
                    with connections.use(job.targetDatabase) as database:
                        staged = {column.upper() for column in database.getAllColumnNames(table=job.targetTableStage)}
                    log.logging.info('{}: stage table {} has {} column(s)'.format(name, job.targetTableStage, len(staged)))
                    missing = [column for column in columns if column.upper() not in staged]
                    if missing:
                        problems.append('{}: stage table {} is missing column(s) {}, which the load writes'.format(
                            name, job.targetTableStage, ', '.join(missing)))
                except Exception as error:
                    problems.append('{}: stage table {} is not readable -- {}'.format(name, job.targetTableStage, describeError(error)))

            if job.sourceDatabase in unreachable:
                continue

            try:
                returned = _sourceQueryColumns(job, connections)
            except Exception as error:
                problems.append('{}: sourceQuery could not be checked -- {}'.format(name, describeError(error)))
                continue

            # The same comparison the load makes, made before it writes: a target
            # that gained or lost a column against a query that didn't is the
            # ordinary way a working job stops working, and a run finds it at 03:00.
            problem = _checkColumnCounts(name, job, returned, columns)
            if problem:
                problems.append(problem)
            elif job.masking is not None:
                problem = _checkMaskingCoverage(name, job, returned, log)
                if problem:
                    problems.append(problem)

    if problems:
        for problem in problems:
            log.logging.error(problem)
        return EXIT_JOBS_DID_NOT_SUCCEED

    print('dry run passed: {} database alias(es), {} job(s), no rows moved'.format(len(aliases), len(jobsFile.jobs)))

    return EXIT_SUCCESS


def _commandHistory(arguments: argparse.Namespace, log: Log) -> int:
    """The latest outcomes `run` recorded, newest first, from the flags'
    history or else the jobs file's.
    """

    from ..jobs.reporting import renderHistory

    location: Optional[Location]
    if arguments.history:
        location, databaseConfiguration = _resolveLocation(arguments, 'history'), {}
    elif arguments.history_database:
        location, databaseConfiguration = _resolveLocation(arguments, 'history'), _loadDatabases(arguments)
    else:
        jobsFile, databaseConfiguration = _loadDataJobs(arguments)
        location = _resolveLocation(arguments, 'history', jobsFile.history)
        if location is None:
            raise UsageError('name the history to read: --history FILE, --history-database ALIAS, or `history` in the jobs file')

    assert location is not None
    history = _history(location, databaseConfiguration)

    records = history.read(limit=arguments.limit, job=arguments.job)
    sys.stdout.write(json.dumps(records, indent=2) + '\n' if arguments.format == 'json' else renderHistory(records))

    return EXIT_SUCCESS


def _commandJobs(arguments: argparse.Namespace, log: Log) -> int:
    """Prints the graph as the scheduler sees it now, including which jobs
    their refresh window holds back.
    """

    jobsFile, databaseConfiguration = _loadDataJobs(arguments)
    memory, _ = _memoryBackend(arguments, jobsFile, databaseConfiguration)
    with memory:
        graph = DependencyGraph(jobs=jobsFile.jobs, memory=memory.read())
        watermarks = memory.readWatermarks()

    print('{:<28} {:<10} {:<22} {}'.format('JOB', 'STATE', 'WAITS FOR', 'WATERMARK'))

    for name, job in sorted(jobsFile.jobs.items()):
        if not job.active:
            state = 'inactive'
        elif name not in graph.activeJobs:
            state = 'throttled'
        else:
            state = 'due'

        waitsFor = ', '.join(graph.activePredecessors.get(name, job.predecessors)) or '-'
        print('{:<28} {:<10} {:<22} {}'.format(name, state, waitsFor, watermarks.get(name, '-')))

    print('\n{} of {} job(s) due this cycle. "throttled" means inside its `refresh` window.'.format(len(graph.activeJobs), len(jobsFile.jobs)))

    return EXIT_SUCCESS

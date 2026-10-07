"""The commands that run jobs and look after their state: run, validate,
jobs, history and verify-manifest.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..configuration import ConfigurationError, ConnectionConfig, DataJobConfig, DataJobsFile, FilesConnection, IcebergConnection, InsertStrategy, isLake
from ..database import DIALECTS
from ..jobs.dependencyGraph import DependencyGraph
from ..log import Log
from ..masking import keyFingerprint, sealManifest, verifyManifest
from ..jobs.memory import RunInProgressError, exclusiveRun
from ..jobs.runner import RunResult, runDataJobs
from ..log.scrubbing import describeError
from .common import (DEFAULT_TABLES, EXIT_INTERRUPTED, EXIT_JOBS_DID_NOT_SUCCEED, EXIT_SUCCESS, NOTIFY_URL_VARIABLE, _Connections, _refuseReadOnlyLocations, Location,
                     UsageError, _checkColumnCounts, _checkMaskingCoverage, _describeLocation, _discoveryRulesFile, _history, _loadConnections,
                     _loadDataJobs, _memoryBackend, _memoryLocation, _readJobs, _resolveConfigurationPaths, _resolveLocation, _selectJobs, _settingsFor,
                     _sourceQueryColumns, _toolVersion)


def _cycleReporter(arguments: argparse.Namespace, jobsFile: DataJobsFile, connectionConfiguration: Dict[str, ConnectionConfig],
                   log: Log) -> Optional[Callable[[RunResult], None]]:
    """What `run` does as each cycle ends: history and notifications, as the
    flags and the jobs file ask. None if they ask for nothing.
    """

    from ..jobs.reporting import RunHistory, newRunId, notify

    historyLocation = _resolveLocation(arguments, 'history', jobsFile.history)
    history: Optional[RunHistory] = _history(historyLocation, connectionConfiguration) if historyLocation else None

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

    jobsFile, connectionConfiguration = _loadDataJobs(arguments)
    jobsFile = _applyJobSelection(jobsFile, arguments, log)

    if arguments.full_refresh:
        if arguments.forever:
            raise UsageError('--full-refresh replaces every incremental target, so it runs once; schedule it, rather than with --forever')
        from ..jobs.fullRefresh import fullRefreshJobs

        jobs = {name: job.model_copy(update={'refresh': None}) for name, job in jobsFile.jobs.items()}
        jobsFile = jobsFile.model_copy(update={'jobs': fullRefreshJobs(jobs, connectionConfiguration)})

    if arguments.dry_run:
        return _dryRunDataJobs(jobsFile, connectionConfiguration, log)

    _refuseReadOnlyLocations(arguments, jobsFile, connectionConfiguration)
    memory, lockFile = _memoryBackend(arguments, jobsFile, connectionConfiguration)
    lockFile.parent.mkdir(parents=True, exist_ok=True)

    try:
        # A --job run locks only its jobs, so an orchestrator's task per job can run side by side.
        with memory, exclusiveRun(lockFile, jobs=sorted(jobsFile.jobs) if arguments.job else None):
            result = runDataJobs(jobsFile=jobsFile, connectionConfiguration=connectionConfiguration, logFile=arguments.log,
                                 memory=memory, runForever=arguments.forever,
                                 logLevel=getattr(logging, arguments.log_level.upper()), logFormat=arguments.log_format,
                                 acceptKeyChange=arguments.accept_key_change,
                                 onCycle=_cycleReporter(arguments, jobsFile, connectionConfiguration, log))
    except RunInProgressError as error:
        log.logging.error(str(error))
        return EXIT_JOBS_DID_NOT_SUCCEED

    manifestLocation = _resolveLocation(arguments, 'manifest', jobsFile.manifest)
    if manifestLocation:
        _writeManifest(manifestLocation, result, jobsFile, connectionConfiguration, arguments, log)

    return _reportRun(result, log)


def _writeManifest(location: Location, result: RunResult, jobsFile: DataJobsFile, connectionConfiguration: Dict[str, ConnectionConfig],
                   arguments: argparse.Namespace, log: Log) -> None:
    """Writes the run's masking manifest as sealed JSON, to a file or a table,
    even when a job failed, with the tool version and a digest of the jobs
    file. Signed when the signing key's variable is set.
    """

    jobsPath, _ = _resolveConfigurationPaths(arguments)
    manifest = result.maskingManifest(jobsFile.jobs)
    configuration: Dict[str, Any] = {'jobsFile': str(jobsPath), 'sha256': hashlib.sha256(jobsPath.read_bytes()).hexdigest()}
    included = _readJobs(jobsPath).files[1:]
    if included:
        # Each file's digest, since the jobs file's alone no longer says what ran.
        configuration['includes'] = [{'file': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in included]
    manifest.update(tool={'name': 'bauta', 'version': _toolVersion()}, configuration=configuration)

    signingKey = os.environ.get(arguments.manifest_key_variable)

    if isinstance(location, Path):
        from ..jobs.memory import exclusiveLock

        location.parent.mkdir(parents=True, exist_ok=True)
        # `run --job` runs go side by side, each with its own jobs: each
        # adds its jobs to the file under this lock, where each used to
        # replace it with only its own.
        with exclusiveLock(location.with_name(location.name + '.lock')):
            destination = location
            if arguments.job:
                manifest, destination = _mergedManifest(location, manifest, signingKey, arguments.manifest_key_variable, log)
            _writeAtomically(destination, json.dumps(sealManifest(manifest, signingKey=signingKey), indent=2) + '\n')
        where = str(destination)
    else:
        from ..jobs.reporting import DatabaseManifests, newRunId

        runId = newRunId()
        manifest = sealManifest(manifest, signingKey=signingKey)
        DatabaseManifests(_settingsFor(location, connectionConfiguration), table=location.table or DEFAULT_TABLES['manifest']).write(manifest, runId)
        where = '{}, run {}'.format(_describeLocation(location), runId)

    log.logging.info('Wrote the masking manifest for {} job(s) to {}, {}'.format(
        len(manifest['jobs']), where, 'signed' if signingKey else 'unsigned (set ${} to sign it)'.format(arguments.manifest_key_variable)))


def _writeAtomically(path: Path, text: str) -> None:
    """Replaces `path` with `text` in one step, so a reader -- or a run dying
    part-way -- never sees half a file.
    """

    temporary = path.with_name('.{}.{}.tmp'.format(path.name, os.getpid()))
    try:
        temporary.write_text(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _setAside(location: Path, label: str) -> Path:
    """Where to keep a manifest file beside `location` under `label`, stamped
    with the time in UTC so a second one never replaces the first.
    """

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')

    return location.with_name('{}.{}-{}'.format(location.name, label, stamp))


def _mergedManifest(location: Path, manifest: Dict[str, Any], signingKey: Optional[str], keyVariable: str,
                    log: Log) -> Tuple[Dict[str, Any], Path]:
    """`manifest`, for a `run --job`, with the jobs the file at `location`
    already records and this run didn't run, and where to write it. Each job
    keeps when it was masked and by which implementation, since they now
    differ by job.

    What is carried over is re-sealed, so it must verify first, and a file
    that doesn't is never destroyed, being what an auditor would examine:

    - altered, or unreadable: moved aside as `<name>.rejected-<time>`, and
      the file started afresh with this run's jobs;
    - signed with a key this run can't check -- another key, or none set --
      left as it is, since that is a task set up wrong rather than a file
      tampered with, and one such task would otherwise empty it of every
      other job's entry. This run's manifest goes beside it, as
      `<name>.unmerged-<time>`.
    """

    for entry in manifest['jobs']:
        entry.update(generatedAt=manifest['generatedAt'], maskedBy=manifest['maskedBy'])

    if not location.exists():
        return manifest, location

    try:
        earlier = json.loads(location.read_text())
        verification = verifyManifest(earlier, signingKey=signingKey)
        why = None if verification.digestValid else 'it was altered'
    except (ValueError, OSError) as error:
        verification, why = None, 'it could not be read: {}'.format(error)

    if why is not None:
        rejected = _setAside(location, 'rejected')
        os.replace(location, rejected)
        log.logging.error('{} does not verify ({}); kept as {} for examination, and started afresh with this run\'s job(s)'.format(
            location, why, rejected))
        return manifest, location

    assert verification is not None
    if verification.signed and not verification.signatureValid:
        unmerged = _setAside(location, 'unmerged')
        log.logging.error('{} is signed with key {}, which {}, so this run\'s job(s) cannot be added to it; left as it is, and this run\'s '
                          'manifest written to {}'.format(location, verification.signingKeyFingerprint,
                                                          'is not this run\'s' if signingKey else '${} is not set to check'.format(keyVariable),
                                                          unmerged))
        return manifest, unmerged

    ran = {entry['job'] for entry in manifest['jobs']}
    carried = [dict(entry, generatedAt=entry.get('generatedAt', earlier.get('generatedAt')), maskedBy=entry.get('maskedBy', earlier.get('maskedBy')))
               for entry in earlier.get('jobs', []) if entry.get('job') not in ran]

    return dict(manifest, jobs=sorted(carried + manifest['jobs'], key=lambda entry: str(entry['job']))), location


def _readManifest(arguments: argparse.Namespace) -> Tuple[str, Dict[str, Any]]:
    """The manifest to verify, and what to call it: the file named, else from
    --manifest-connection, else from wherever the jobs file's `manifest` says --
    from a table, the latest run's unless --run names one.
    """

    location: Optional[Location]
    if arguments.manifest:
        location = Path(arguments.manifest)
        connectionConfiguration: Dict[str, ConnectionConfig] = {}
    elif arguments.manifest_connection:
        location = _resolveLocation(arguments, 'manifest')
        connectionConfiguration = _loadConnections(arguments)
    else:
        jobsFile, connectionConfiguration = _loadDataJobs(arguments)
        location = _resolveLocation(arguments, 'manifest', jobsFile.manifest)
        if location is None:
            raise UsageError('name the manifest to verify: a FILE, --manifest-connection ALIAS, or `manifest` in the jobs file')

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
        runId, manifest = DatabaseManifests(_settingsFor(location, connectionConfiguration, writing=False),
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

    jobsFile, connectionConfiguration = _loadDataJobs(arguments)

    problems = []
    for name, job in jobsFile.jobs.items():
        for column, references in job.sourceQueryColumnTransforms.items():
            for reference in references:
                try:
                    resolveTransformer(reference)
                except Exception as error:
                    problems.append('{}: {} -> {}'.format(name, column, error))

    for alias, settings in sorted(connectionConfiguration.items()):
        if isLake(settings):
            continue
        assert not isinstance(settings, (FilesConnection, IcebergConnection))
        try:
            DIALECTS[settings.type].connectArguments(settings, resolvePassword=False)
        except ConfigurationError as error:
            problems.append('{}: {}'.format(alias, error))

    from ..masking import effectiveMaskingThreads, requireNativeProblem

    try:
        effectiveMaskingThreads(jobsFile.maskingThreads)
    except ValueError as error:
        problems.append(str(error))
    if any(job.masking is not None and job.active for job in jobsFile.jobs.values()):
        problem = requireNativeProblem(jobsFile.requireNative)
        if problem:
            problems.append(problem)

    if problems:
        raise ConfigurationError('invalid configuration:\n' + '\n'.join(problems))

    print('configuration is valid: {} connection(s), {} job(s)'.format(len(connectionConfiguration), len(jobsFile.jobs)))
    jobsPath, _ = _resolveConfigurationPaths(arguments)
    document = _readJobs(jobsPath)
    if len(document.files) > 1:
        counts = {path: 0 for path in document.files}
        for path in document.origins.values():
            counts[path] += 1
        print('jobs from {} file(s): {}'.format(len(counts), ', '.join('{} ({})'.format(path, count) for path, count in counts.items())))
    _refuseReadOnlyLocations(arguments, jobsFile, connectionConfiguration)
    print('run state: {}'.format(_describeLocation(_memoryLocation(arguments, jobsFile))))
    for setting, missing in (('history', 'not recorded'), ('manifest', 'not written')):
        location = _resolveLocation(arguments, setting, getattr(jobsFile, setting))
        print('{}: {}'.format(setting, _describeLocation(location) if location else missing))

    from ..masking import MASKING_THREADS_VARIABLE, availableCores, maskingThreadsFor, nativeUnavailableReason, nativeVersion

    if any(job.masking is not None for job in jobsFile.jobs.values()):
        if nativeVersion() is None:
            print('masking: in Python, one thread per job, about ten times slower than the native masker: {}'.format(nativeUnavailableReason()))
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


def _checkTargetTables(name: str, job: DataJobConfig, connections: _Connections, log: Log, problems: List[str]) -> Optional[List[str]]:
    """A database target's columns, having checked it is there, keyed for an
    upsert, and has a stage table holding them all; None, with the problem
    added, when the target can't be read.
    """

    try:
        with connections.use(job.targetConnection) as database:
            columns = database.getAllColumnNames(table=job.targetTableFinal)
            log.logging.info('{}: target {} has {} column(s)'.format(name, job.targetTableFinal, len(columns)))

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
            log.logging.info('{}: stage table {} has {} column(s)'.format(name, job.targetTableStage, len(staged)))
            missing = [column for column in columns if column.upper() not in staged]
            if missing:
                problems.append('{}: stage table {} is missing column(s) {}, which the load writes'.format(
                    name, job.targetTableStage, ', '.join(missing)))
        except Exception as error:
            problems.append('{}: stage table {} is not readable -- {}'.format(name, job.targetTableStage, describeError(error)))

    return columns


def _dryRunDataJobs(jobsFile: DataJobsFile, connectionConfiguration: Dict[str, ConnectionConfig], log: Log) -> int:
    """Everything `validate` does, plus what needs a connection: that each alias
    connects or can be written, target tables exist, upsert targets have a
    primary key, and each query's columns fit its target and masking policy.
    """

    problems: List[str] = []
    unreachable = set()
    aliases = sorted({job.sourceConnection for job in jobsFile.jobs.values()} | {job.targetConnection for job in jobsFile.jobs.values()})

    with _Connections(connectionConfiguration) as connections:

        for alias in aliases:
            settings = connectionConfiguration[alias]
            if isLake(settings):
                from ..lake import checkWritable

                try:
                    checkWritable(settings)
                    log.logging.info('{}: writable ({})'.format(alias, settings.describeTarget()))
                except Exception as error:
                    unreachable.add(alias)
                    problems.append('{}: cannot write to {} -- {}'.format(alias, settings.describeTarget(), describeError(error)))
                continue
            try:
                with connections.use(alias) as database:
                    encrypted = {True: 'encrypted', False: 'NOT encrypted', None: 'encryption unknown'}[database.isEncrypted()]
                    log.logging.info('{}: connected ({}, {})'.format(alias, connectionConfiguration[alias].type.value, encrypted))
            except Exception as error:
                unreachable.add(alias)
                problems.append('{}: cannot connect to {} -- {}'.format(
                    alias, connectionConfiguration[alias].describeTarget(), describeError(error)))

        for name, job in jobsFile.jobs.items():
            if job.targetConnection in unreachable:
                continue

            # A lake has no table to read before the first run: it takes the
            # columns the query returns.
            lake = isLake(connectionConfiguration[job.targetConnection])
            columns = None if lake else _checkTargetTables(name, job, connections, log, problems)
            if (not lake and columns is None) or job.sourceConnection in unreachable:
                continue

            try:
                returned = _sourceQueryColumns(job, connections)
            except Exception as error:
                problems.append('{}: sourceQuery could not be checked -- {}'.format(name, describeError(error)))
                continue

            # The same comparison the load makes, made before it writes: a target
            # that gained or lost a column against a query that didn't is the
            # ordinary way a working job stops working.
            problem = _checkColumnCounts(name, job, returned, returned if columns is None else columns)
            if problem is None and job.masking is not None:
                problem = _checkMaskingCoverage(name, job, returned, log)
            if problem is None and job.partitions is not None and job.partitions.column is not None:
                from ..jobs.partitions import resolveColumn

                try:
                    resolveColumn(job.partitions.column, returned)
                except ConfigurationError as error:
                    problem = '{}: {}'.format(name, error)
            if problem:
                problems.append(problem)

    if problems:
        for problem in problems:
            log.logging.error(problem)
        return EXIT_JOBS_DID_NOT_SUCCEED

    print('dry run passed: {} connection(s), {} job(s), no rows moved'.format(len(aliases), len(jobsFile.jobs)))

    return EXIT_SUCCESS


def _commandHistory(arguments: argparse.Namespace, log: Log) -> int:
    """The latest outcomes `run` recorded, newest first, from the flags'
    history or else the jobs file's.
    """

    from ..jobs.reporting import renderHistory

    location: Optional[Location]
    if arguments.history:
        location, connectionConfiguration = _resolveLocation(arguments, 'history'), {}
    elif arguments.history_connection:
        location, connectionConfiguration = _resolveLocation(arguments, 'history'), _loadConnections(arguments)
    else:
        jobsFile, connectionConfiguration = _loadDataJobs(arguments)
        location = _resolveLocation(arguments, 'history', jobsFile.history)
        if location is None:
            raise UsageError('name the history to read: --history FILE, --history-connection ALIAS, or `history` in the jobs file')

    assert location is not None
    history = _history(location, connectionConfiguration, writing=False)

    records = history.read(limit=arguments.limit, job=arguments.job)
    sys.stdout.write(json.dumps(records, indent=2) + '\n' if arguments.format == 'json' else renderHistory(records))

    return EXIT_SUCCESS


def _commandJobs(arguments: argparse.Namespace, log: Log) -> int:
    """Prints the graph as the scheduler sees it now, including which jobs
    their refresh window holds back.
    """

    jobsFile, connectionConfiguration = _loadDataJobs(arguments)
    memory, _ = _memoryBackend(arguments, jobsFile, connectionConfiguration)
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

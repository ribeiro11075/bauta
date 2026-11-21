"""bauta's configuration, history and live runs, as Studio's screens show
them. Everything goes through bauta.project, as the `bauta` command does.

What leaves here is what `bauta audit` and `bauta history` already show:
names of jobs, tables, columns and connections, strategies, key
fingerprints, counts, times and errors as history keeps them. Never a
masking key, a password, or a value from a table.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple

from .. import ConfigurationError, Project, keyFingerprint, verifyManifest
from ..project import UsageError

from .settings import HISTORY_RUNS, Settings

MANIFEST_KEY_VARIABLE = 'BAUTA_MANIFEST_KEY'
STAGES = {'read': 'readSeconds', 'mask': 'maskSeconds', 'write': 'writeSeconds', 'wait': 'throttledSeconds'}
# The longest a loaded configuration is reused while none of its files has
# changed: what it reads besides them -- a new file an `include` pattern
# matches, the environment, a ${file:...} secret -- is read again this often.
PROJECT_REUSE_SECONDS = 30.0


class ConfigurationProblem(Exception):
    """The configuration doesn't load: every screen but the editor says why."""


class _Loaded(NamedTuple):
    project: Project
    stamp: Tuple[Tuple[str, Optional[str]], ...]
    at: float


_loaded: Dict[Path, _Loaded] = {}
_loading = threading.Lock()


def _stamp(project: Project) -> Tuple[Tuple[str, Optional[str]], ...]:
    """Each file the configuration was read from, with a digest of what it
    holds -- not when it changed, which some filesystems keep to the second
    -- or None for a file gone. Jobs files are small; reading them is cheap
    where validating what they say is not.
    """

    stamps: List[Tuple[str, Optional[str]]] = []
    for path in [project.connectionsPath, *project.document.files]:
        try:
            stamps.append((str(path), hashlib.sha256(Path(path).read_bytes()).hexdigest()))
        except OSError:
            stamps.append((str(path), None))

    return tuple(stamps)


def loadProject(settings: Settings) -> Project:
    """The configuration as it stands, reused while none of its files has
    changed and for at most PROJECT_REUSE_SECONDS: every request and every
    page's polling asked for it, and validating 500 masked jobs takes a fifth
    of a second. An edit made here changes its file, so the next request
    reads it.
    """

    with _loading:
        found = _loaded.get(settings.configDirectory)
    if found is not None and time.monotonic() - found.at < PROJECT_REUSE_SECONDS and _stamp(found.project) == found.stamp:
        return found.project

    try:
        project = Project.load(settings.configDirectory)
    except (ConfigurationError, UsageError) as error:
        raise ConfigurationProblem(str(error)) from error

    with _loading:
        _loaded[settings.configDirectory] = _Loaded(project, _stamp(project), time.monotonic())

    return project


# Jobs -------------------------------------------------------------------------

def _strategyName(policy: Any) -> str:
    """A column policy's strategy, never its options: an option could be
    anything a strategy takes.
    """

    if isinstance(policy, Mapping):
        return str(policy.get('strategy', ''))

    return str(policy)


def describeJob(project: Project, name: str) -> Dict[str, Any]:

    job = project.jobsFile.jobs[name]
    masking = None
    if job.masking is not None:
        masking = {'keyFingerprint': keyFingerprint(job.masking.key.get_secret_value()),
                   'columns': {column: _strategyName(policy) for column, policy in job.masking.columns.items()},
                   'defaultStrategy': _strategyName(job.masking.defaultStrategy) if job.masking.defaultStrategy is not None else None}
    partitions = None
    if job.partitions is not None:
        partitions = 'auto' if job.partitions.column is None else '{} on {}'.format(job.partitions.count, job.partitions.column)
    origin = project.document.origins.get(name)

    return {'name': name, 'active': job.active, 'sourceConnection': job.sourceConnection, 'sourceQuery': job.sourceQuery,
            'targetConnection': job.targetConnection, 'targetTable': job.targetTableFinal, 'stageTable': job.targetTableStage,
            'insertStrategy': job.insertStrategy.value, 'watermarkColumn': job.watermarkColumn, 'partitions': partitions,
            'predecessors': list(job.predecessors), 'refresh': str(job.refresh) if getattr(job, 'refresh', None) is not None else None,
            'unmasked': job.unmasked, 'masking': masking,
            'file': os.path.relpath(origin, project.directory) if origin is not None else None}


def jobs(project: Project, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every job, with its latest outcome in `history`."""

    latest: Dict[str, Dict[str, Any]] = {}
    for record in history:
        latest.setdefault(record['job'], record)

    return [dict(describeJob(project, name), lastRun=latest.get(name)) for name in sorted(project.jobsFile.jobs)]


# Runs -------------------------------------------------------------------------

def readHistory(project: Project, runs: int = HISTORY_RUNS) -> Tuple[bool, List[Dict[str, Any]], bool]:
    """Whether the jobs file keeps history; its newest records, enough to hold
    `runs` whole runs, since a run has at most one record per job; and
    whether that is all of it, so the oldest run read is whole.
    """

    history = project.history()
    if history is None:
        return False, [], True

    limit = (runs + 1) * max(1, len(project.jobsFile.jobs))
    records = history.read(limit=limit)

    return True, records, len(records) < limit


def _runStatus(jobs: List[Dict[str, Any]]) -> str:

    statuses = [job['status'] for job in jobs]
    if 'running' in statuses or 'waiting' in statuses:
        return 'running'
    failed = sum(status in ('failed', 'skipped') for status in statuses)
    if not failed:
        return 'succeeded'

    return 'failed' if failed == len(statuses) else 'partial'


def _stages(jobs: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:

    totals = {stage: sum(job.get(field) or 0.0 for job in jobs) for stage, field in STAGES.items()}

    return totals if any(totals.values()) else None


def summarizeRun(runId: str, jobs: List[Dict[str, Any]], live: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:

    started = [job['startedAt'] for job in jobs if job.get('startedAt')]
    finished = [job['finishedAt'] for job in jobs if job.get('finishedAt')]
    counts: Dict[str, int] = {}
    for job in jobs:
        counts[job['status']] = counts.get(job['status'], 0) + 1

    # A live run is running while its process is, even once every job is
    # decided: it has history and its manifest still to write.
    status = _runStatus(jobs) if live is None else 'stopped' if live.get('alive') is False else 'running'

    return {'runId': runId, 'status': status,
            'startedAt': (live or {}).get('startedAt') or (min(started) if started else None),
            'finishedAt': None if live is not None else (max(finished) if finished else None),
            'jobCount': len(jobs), 'counts': counts, 'rowCount': sum(job.get('rowCount') or 0 for job in jobs),
            'durationSeconds': _duration(jobs, live), 'stages': _stages(jobs), 'live': live is not None,
            'host': (live or {}).get('host'), 'pid': (live or {}).get('pid')}


def _duration(jobs: List[Dict[str, Any]], live: Optional[Dict[str, Any]]) -> Optional[float]:

    import datetime

    def parse(text: str) -> datetime.datetime:
        return datetime.datetime.fromisoformat(text)

    starts = [parse(job['startedAt']) for job in jobs if job.get('startedAt')]
    if live is not None and live.get('startedAt'):
        starts.append(parse(live['startedAt']))
    if not starts:
        return None
    ends = [parse(job['finishedAt']) for job in jobs if job.get('finishedAt')]
    end = datetime.datetime.now(datetime.timezone.utc) if live is not None else (max(ends) if ends else None)

    return None if end is None else max(0.0, (end - min(starts)).total_seconds())


def runs(project: Project, history: List[Dict[str, Any]], complete: bool) -> List[Dict[str, Any]]:
    """Live runs first, then history's, newest first. When history was cut
    short, the oldest run read may be missing jobs, so it is left out.
    """

    live = project.running()
    byRun: Dict[str, List[Dict[str, Any]]] = {}
    for record in history:
        byRun.setdefault(record['runId'], []).append(record)
    if not complete and history:
        byRun.pop(history[-1]['runId'], None)

    summaries = [summarizeRun(record['runId'], record['jobs'], live=record) for record in live]
    liveIds = {record['runId'] for record in live}
    summaries += [summarizeRun(runId, jobs) for runId, jobs in byRun.items() if runId not in liveIds]

    return sorted(summaries, key=lambda run: (not run['live'], _negated(run['startedAt'] or '')))


def _negated(text: str) -> Tuple[int, ...]:
    """A key sorting ISO timestamps newest first."""

    return tuple(-ord(character) for character in text)


def runDetail(project: Project, history: List[Dict[str, Any]], runId: str) -> Optional[Dict[str, Any]]:

    for record in project.running():
        if record['runId'] == runId:
            return dict(summarizeRun(runId, record['jobs'], live=record), jobs=record['jobs'])

    jobsOfRun = [record for record in history if record['runId'] == runId]
    if not jobsOfRun:
        return None

    # By start, and where history's whole seconds tie, in the order they
    # finished: history's, newest first, reversed.
    ordered = sorted(reversed(jobsOfRun), key=lambda job: job.get('startedAt') or '')

    return dict(summarizeRun(runId, jobsOfRun), jobs=ordered)


# Proof ------------------------------------------------------------------------

def manifestState(project: Project) -> Dict[str, Any]:
    """The last manifest `run` wrote, and whether it verifies: with the
    signing key, if Studio's environment has it, as `verify-manifest` does.
    """

    if project.location('manifest') is None:
        return {'state': 'not-kept'}
    try:
        found = project.readManifest()
    except (ValueError, OSError) as error:
        return {'state': 'unreadable', 'problem': str(error)}
    if found is None:
        return {'state': 'not-written'}

    where, manifest = found
    signingKey = os.environ.get(MANIFEST_KEY_VARIABLE)
    try:
        verification = verifyManifest(manifest, signingKey=signingKey)
    except ValueError as error:
        return {'state': 'unreadable', 'where': where, 'problem': str(error)}

    if not verification.digestValid:
        state = 'altered'
    elif not verification.signed:
        state = 'unsigned-with-key' if signingKey else 'intact-unsigned'
    elif signingKey is None:
        state = 'signed-unchecked'
    else:
        state = 'verified' if verification.signatureValid else 'signature-invalid'

    jobsOfManifest = manifest.get('jobs', [])

    return {'state': state, 'where': where, 'generatedAt': manifest.get('generatedAt'), 'maskedBy': manifest.get('maskedBy'),
            'tool': manifest.get('tool'), 'signingKeyFingerprint': verification.signingKeyFingerprint,
            'jobs': [{'job': entry.get('job'), 'status': entry.get('status'), 'rowCount': entry.get('rowCount'),
                      'keyFingerprint': entry.get('keyFingerprint'), 'targetTable': entry.get('targetTable'),
                      'columns': len(entry.get('columns') or [])} for entry in jobsOfManifest]}


# Connections ------------------------------------------------------------------

def connections(project: Project) -> List[Dict[str, Any]]:
    """Each alias as `describeTarget` names it -- host, database, path or
    bucket -- with the settings that say how it may be used. Never a
    password or a passwordCommand.
    """

    used: Dict[str, Dict[str, List[str]]] = {}
    for name, job in project.jobsFile.jobs.items():
        used.setdefault(job.sourceConnection, {'source': [], 'target': []})['source'].append(name)
        used.setdefault(job.targetConnection, {'source': [], 'target': []})['target'].append(name)

    described = []
    for alias, settings in sorted(project.connections.items()):
        described.append({'alias': alias, 'type': settings.type.value, 'target': settings.describeTarget(),
                          'readOnly': bool(getattr(settings, 'readOnly', False)), 'requireMasking': bool(getattr(settings, 'requireMasking', False)),
                          'requireEncryption': bool(getattr(settings, 'requireEncryption', False)),
                          'sourceOf': sorted(used.get(alias, {}).get('source', [])), 'targetOf': sorted(used.get(alias, {}).get('target', []))})

    return described


def testConnection(project: Project, alias: str) -> Dict[str, Any]:
    """Connects to `alias` and asks whether the session is encrypted, as
    `run --dry-run` does. A files or Iceberg connection is checked writable,
    or readable if jobs only read it.
    """

    from .. import Database
    from ..configuration import isLake
    from ..log.scrubbing import describeError

    settings = project.connections[alias]
    try:
        if isLake(settings):
            from ..lake import checkReadable, checkWritable

            writes = any(job.targetConnection == alias for job in project.jobsFile.jobs.values())
            onlyRead = any(job.sourceConnection == alias for job in project.jobsFile.jobs.values()) and not writes
            if not (getattr(settings, 'readOnly', False) or onlyRead):
                checkWritable(settings)
                return {'ok': True, 'detail': 'writable'}
            checkReadable(settings)
            return {'ok': True, 'detail': 'readable'}
        with Database(connectionSettings=settings) as database:
            encrypted = database.isEncrypted()
        return {'ok': True, 'encrypted': encrypted, 'detail': {True: 'connected, encrypted', False: 'connected, NOT encrypted',
                                                                None: 'connected, encryption unknown'}[encrypted]}
    except Exception as error:
        return {'ok': False, 'detail': describeError(error)}


# Comparing runs ---------------------------------------------------------------

def _masking(manifest: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """Each job's columns and the strategy each was masked with, from a run's
    manifest; empty without one.
    """

    return {entry.get('job'): {column['column']: column['strategy'] for column in entry.get('columns') or []}
            for entry in (manifest or {}).get('jobs', [])}


def compareRuns(earlier: Dict[str, Any], later: Dict[str, Any], earlierManifest: Optional[Dict[str, Any]] = None,
                laterManifest: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What changed between two runs, job by job: status, rows, duration and
    the busy time at each stage, and, where both runs' manifests are kept,
    the columns masked differently, added or gone.
    """

    def byJob(run: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {job['job']: job for job in run['jobs']}

    before, after = byJob(earlier), byJob(later)
    maskedBefore, maskedAfter = _masking(earlierManifest), _masking(laterManifest)
    comparable = earlierManifest is not None and laterManifest is not None
    jobs = []
    for name in sorted(set(before) | set(after)):
        one, two = before.get(name), after.get(name)
        stages = {}
        for stage, field in STAGES.items():
            stages[stage] = {'before': (one or {}).get(field), 'after': (two or {}).get(field)}
        columns: Dict[str, Any] = {}
        if comparable:
            old, new = maskedBefore.get(name, {}), maskedAfter.get(name, {})
            columns = {'added': sorted(set(new) - set(old)), 'removed': sorted(set(old) - set(new)),
                       'changed': [{'column': column, 'before': old[column], 'after': new[column]}
                                   for column in sorted(set(old) & set(new)) if old[column] != new[column]]}
        jobs.append({'job': name, 'status': {'before': (one or {}).get('status'), 'after': (two or {}).get('status')},
                     'rows': {'before': (one or {}).get('rowCount'), 'after': (two or {}).get('rowCount')},
                     'duration': {'before': (one or {}).get('durationSeconds'), 'after': (two or {}).get('durationSeconds')},
                     'stages': stages, 'columns': columns})

    return {'earlier': {key: earlier[key] for key in ('runId', 'startedAt', 'status', 'rowCount', 'durationSeconds')},
            'later': {key: later[key] for key in ('runId', 'startedAt', 'status', 'rowCount', 'durationSeconds')},
            'manifestsCompared': comparable, 'jobs': jobs}

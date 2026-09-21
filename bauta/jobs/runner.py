"""runDataJobs: cycles of jobs, each started as soon as its predecessors
finish and a worker slot is free, until the cycle is done or a signal
stops it. See "Single runs, not a daemon" in docs/design.md.
"""
from __future__ import annotations

import contextlib
import logging
import signal
import time
from multiprocessing.connection import wait as waitForAny
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, NamedTuple, Optional, Union

from ..configuration import ConfigurationError, DatabaseConnectionConfig, DataJobConfig, DataJobsFile
from ..log import LOGGER_NAME, Log
from ..log.scrubbing import describeError
from ..masking import availableCores, buildMaskingManifest, keyFingerprint, maskingThreadsFor
from ..masking import core as maskingModule
from .dependencyGraph import DependencyGraph, JobOutcome, JobStatus
from .keys import _requireUnchangedMaskingKeys
from .memory import MemoryBackend
from .workers import _JobProcess

logger = logging.getLogger(LOGGER_NAME)


# How late the run loop may notice SIGINT/SIGTERM. Completions and timeouts
# wake it on time.
SIGNAL_POLL_SECONDS = 1.0


@contextlib.contextmanager
def _terminationHandling() -> Iterator[Dict[str, bool]]:
    """Turns SIGINT/SIGTERM into a flag the run loop acts on, restoring the
    previous handlers on the way out. See "Stopping" in docs/design.md.

    The handler only sets the flag: teardown inside it would run on whatever
    frame was executing, possibly inside multiprocessing. Off the main thread,
    where signal.signal raises, no handlers are installed.
    """

    state = {'terminating': False}

    def handler(signalNumber: int, frame: Any) -> None:
        state['terminating'] = True
        logger.warning('Received {}, letting running jobs finish and shutting down'.format(signal.Signals(signalNumber).name))

    installed = []

    try:
        for signalNumber in (signal.SIGINT, signal.SIGTERM):
            installed.append((signalNumber, signal.signal(signalNumber, handler)))
    except ValueError:
        logger.debug('Not on the main thread; leaving signal handling to the caller')

    try:
        yield state
    finally:
        for signalNumber, previousHandler in installed:
            signal.signal(signalNumber, previousHandler)


def _sleepUnlessTerminated(seconds: float, termination: Dict[str, bool]) -> None:
    """Sleeps between --forever cycles, waking within SIGNAL_POLL_SECONDS of a
    SIGINT or SIGTERM. Python resumes a sleep once a signal's handler returns,
    so one long sleep outlived a container's grace period and was killed.
    """

    wakeAt = time.monotonic() + seconds

    while not termination['terminating']:
        remaining = wakeAt - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(SIGNAL_POLL_SECONDS, remaining))


class RunResult(NamedTuple):
    """What one call to runDataJobs did -- with runForever, its last cycle.
    `interrupted` says a signal ended the run.

    Returned rather than stored: MemoryBackend holds scheduler input, and this
    is output for the caller to act on.
    """

    outcomes: List[JobOutcome]
    interrupted: bool = False

    @property
    def completed(self) -> List[JobOutcome]:

        return [outcome for outcome in self.outcomes if outcome.status == JobStatus.COMPLETED]

    @property
    def failed(self) -> List[JobOutcome]:

        return [outcome for outcome in self.outcomes if outcome.status == JobStatus.FAILED]

    @property
    def skipped(self) -> List[JobOutcome]:

        return [outcome for outcome in self.outcomes if outcome.status == JobStatus.SKIPPED]

    @property
    def rowCount(self) -> int:

        return sum(outcome.rowCount for outcome in self.outcomes)

    @property
    def succeeded(self) -> bool:
        """True only if every active job completed; a skipped job counts as a
        failure, since its data isn't there.
        """

        return not self.failed and not self.skipped


    def maskingManifest(self, jobs: Mapping[str, DataJobConfig]) -> Dict[str, Any]:
        """See masking.buildMaskingManifest. Takes the configurations because a
        skipped job still belongs in the manifest but has no outcome to describe it.
        """

        return buildMaskingManifest(self.outcomes, _declaredMasking(jobs))


def _declaredMasking(jobs: Mapping[str, DataJobConfig]) -> Dict[str, Dict[str, Any]]:
    """What each masked job's configuration says, for the manifest."""

    return {
        name: {
            'sourceDatabase': job.sourceDatabase,
            'targetDatabase': job.targetDatabase,
            'targetTable': job.targetTableFinal,
            'keyFingerprint': keyFingerprint(job.masking.key.get_secret_value()),
            }
        for name, job in jobs.items() if job.masking is not None
        }


def _logCycleSummary(dependencyGraph: DependencyGraph) -> None:
    """One line per failed or skipped job, plus totals. The only place a
    skipped job is logged, since it never reaches a worker.
    """

    result = RunResult(outcomes=list(dependencyGraph.outcomes))

    for outcome in result.failed:
        logger.error('{} failed after {:.1f}s: {}'.format(outcome.job, outcome.durationSeconds, outcome.error),
                     extra={'job': outcome.job, 'status': outcome.status.value, 'error': outcome.error,
                            'attempts': outcome.attempts, 'durationSeconds': round(outcome.durationSeconds, 3)})

    for outcome in result.skipped:
        logger.warning('{} skipped: {}'.format(outcome.job, outcome.error),
                       extra={'job': outcome.job, 'status': outcome.status.value, 'error': outcome.error})

    logger.info('Cycle finished: {} completed, {} failed, {} skipped, {} row(s) moved'.format(
        len(result.completed), len(result.failed), len(result.skipped), result.rowCount),
        extra={'event': 'cycleFinished', 'completed': len(result.completed), 'failed': len(result.failed),
               'skipped': len(result.skipped), 'rowCount': result.rowCount})


def _runCycle(dependencyGraph: DependencyGraph, workers: int, databaseConfiguration: Dict[str, DatabaseConnectionConfig],
              memory: MemoryBackend, termination: Dict[str, bool], logLevel: int, maskingThreads: Union[str, int] = 1) -> None:
    """Runs one cycle's jobs to completion, each as soon as its predecessors
    finish and one of the `workers` slots is free.

    `maskingThreads` is shared out as each job starts, between it and the jobs
    that will run alongside it: those already running and those starting with
    it. So the last job of a cycle, running alone, gets every core. A running
    job keeps its share; cores freed after it started go to the next to start.
    """

    running: List[_JobProcess] = []
    native = maskingModule.nativeVersion() is not None

    try:
        while not dependencyGraph.finished:

            if termination['terminating']:
                dependencyGraph.skipNotStarted('the run was stopped by a signal before this job started')
            else:
                starting = dependencyGraph.takeReady(limit=workers - len(running))
                alongside = len(running) + len(starting)
                for job in starting:
                    jobConfig = dependencyGraph.activeJobs[job]
                    threads = maskingThreadsFor(maskingThreads, alongside)
                    if native and getattr(jobConfig, 'masking', None) is not None:
                        logger.info('{}: masking with {} thread(s) ({} job(s) running, {} core(s))'.format(job, threads, alongside, availableCores()),
                                    extra={'job': job})
                    running.append(_JobProcess(job, jobConfig, databaseConfiguration, memory, logLevel, threads))  # type: ignore[arg-type]

            if not running:
                # Nothing running and nothing startable means every job is
                # decided -- DependencyGraph refuses the cycles that could make
                # this false.
                assert dependencyGraph.finished, 'jobs remain, yet none is running or ready'
                continue

            timeout = SIGNAL_POLL_SECONDS
            deadlines = [process.deadline for process in running if process.deadline is not None]
            if deadlines:
                timeout = max(0.0, min(timeout, min(deadlines) - time.time()))

            waitForAny([waitable for process in running for waitable in process.waitables], timeout=timeout)

            now = time.time()
            for process in list(running):
                outcome = process.poll(now)
                if outcome is not None:
                    running.remove(process)
                    dependencyGraph.finish(outcome)
    finally:
        # Only reached with jobs still running if something above raised.
        for process in running:
            process.stop()


def runDataJobs(jobsFile: DataJobsFile, databaseConfiguration: Dict[str, DatabaseConnectionConfig], memory: MemoryBackend,
                logFile: Optional[Path] = None, runForever: bool = False, logLevel: int = logging.INFO,
                logFormat: str = 'text', acceptKeyChange: bool = False,
                onCycle: Optional[Callable[['RunResult'], None]] = None) -> RunResult:
    """Runs data jobs, honoring each job's `refresh` window and `predecessors`:
    one pass, or with runForever until SIGINT or SIGTERM. See "Single runs,
    not a daemon" in docs/design.md.

    `onCycle` receives each cycle's RunResult, for history or alerts;
    an exception from it is logged, not raised. A masked upsert job whose key
    changed stops the run before it starts, unless acceptKeyChange.

    `memory` stays the caller's to close; each job's process closes its own
    copy. Each job runs in its own process, so `memory` and each job's
    configuration are pickled. Processes are spawned, which re-imports the calling script:
    call this under `if __name__ == '__main__':`.
    """

    _requireUnchangedMaskingKeys(jobsFile, memory, acceptKeyChange, databaseConfiguration)

    if jobsFile.workers < 1:
        raise ConfigurationError('workers must be at least 1, got {}'.format(jobsFile.workers))
    try:
        maskingModule.effectiveMaskingThreads(jobsFile.maskingThreads)
    except ValueError as error:
        raise ConfigurationError(str(error)) from None

    Log(logFile=logFile, level=logLevel, logFormat=logFormat)
    logger.info('Starting data job runner with {} worker(s)'.format(jobsFile.workers))

    with _terminationHandling() as termination:

        while True:
            dependencyGraph = DependencyGraph(jobs=jobsFile.jobs, memory=memory.read())
            logger.info('Starting cycle with {} active job(s)'.format(len(dependencyGraph.activeJobs)))

            _runCycle(dependencyGraph, jobsFile.workers, databaseConfiguration, memory, termination, logLevel, jobsFile.maskingThreads)
            _logCycleSummary(dependencyGraph)

            if onCycle is not None:
                try:
                    onCycle(RunResult(outcomes=list(dependencyGraph.outcomes), interrupted=termination['terminating']))
                except Exception as error:
                    logger.error('Reporting on the cycle failed: {}'.format(describeError(error)), exc_info=error)

            if not runForever or termination['terminating']:
                break

            _sleepUnlessTerminated(jobsFile.cycleSleepSeconds, termination)

        logger.info('Finished data job runner')

    return RunResult(outcomes=list(dependencyGraph.outcomes), interrupted=termination['terminating'])

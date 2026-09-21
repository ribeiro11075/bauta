"""Each job in a process of its own, so it can be stopped or die alone, and
ends the moment the run that started it does. See "Workers" in
docs/design.md.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import signal
import threading
import time
from typing import Any, Dict, List, Optional

from ..configuration import DatabaseConnectionConfig, DataJobConfig
from ..log import LOGGER_NAME, ConnectionForwarder, forwardToConnection, handleForwardedRecord
from ..masking import setMaskingThreads
from .dependencyGraph import JobOutcome, JobStatus
from .memory import MemoryBackend
from .pipeline import _runDataJob

logger = logging.getLogger(LOGGER_NAME)


# Jobs run in processes started this way on every platform. `fork` -- Linux's
# default before Python 3.14 -- copies whatever locks the parent's threads
# happen to hold, which can deadlock the child.
PROCESS_CONTEXT = mp.get_context('spawn')


# How long a timed-out job gets to exit after SIGTERM before it is killed.
TERMINATE_GRACE_SECONDS = 5.0


# The exit code a job uses when it finds its parent gone. Nothing reads it --
# by then there is no parent to report to -- but it tells the two apart in a
# process listing or a core dump.
EXIT_ORPHANED = 3


# How long a job may take to exit once it has sent its outcome, before it is
# stopped. It has nothing left to do by then but close its connections.
EXIT_GRACE_SECONDS = 10.0


# Messages read from one job's pipe before looking at the others, so a job
# logging without pause can't starve the rest.
MESSAGES_PER_POLL = 500


def _exitWhenOrphaned(parentAlive: Any) -> None:
    """Ends this process the moment the run that started it is gone.

    `parentAlive` is a pipe the parent holds the other end of and never writes
    to, so it reads end-of-file exactly when the parent dies -- including under
    SIGKILL, which runs none of the parent's cleanup and used to leave the job
    loading rows and writing run state for as long as its query lasted. The run
    lock dies with the parent, so the next `bauta run` would start alongside it
    and the two would load over each other.

    os._exit, not sys.exit: an orphan must stop writing now, not unwind. Its
    connections die with it, so each server rolls back what it hadn't committed.
    """

    try:
        parentAlive.recv()
    except (EOFError, OSError):
        pass
    finally:
        os._exit(EXIT_ORPHANED)


def _initializeWorker(connection: Any, parentAlive: Any, logLevel: int) -> ConnectionForwarder:
    """Runs first in each job's process. Ignores Ctrl-C, which reaches the whole
    process group, so the parent decides how to stop; SIGTERM keeps its
    default, since that is how a timed-out job is stopped.
    """

    signal.signal(signal.SIGINT, signal.SIG_IGN)
    threading.Thread(target=_exitWhenOrphaned, args=(parentAlive,), name='bauta-parent', daemon=True).start()

    return forwardToConnection(connection, logLevel)


def _jobProcess(connection: Any, parentAlive: Any, logLevel: int, job: str, jobConfig: DataJobConfig,
                databaseConfiguration: Dict[str, DatabaseConnectionConfig], memory: MemoryBackend, maskingThreads: int = 1) -> None:
    """The whole life of one job's process: run the job, and send its log
    records and then its outcome back on `connection`, which it alone writes to.

    `parentAlive` ends the process if the run that started it dies; see
    _exitWhenOrphaned.
    """

    forwarder = _initializeWorker(connection, parentAlive, logLevel)
    setMaskingThreads(maskingThreads)

    try:
        forwarder.send('outcome', _runDataJob(job, jobConfig, databaseConfiguration, memory))
    finally:
        # After the outcome is sent, so closing never delays it.
        memory.close()
        connection.close()


class _JobProcess:
    """One job, running in a process of its own so it can be stopped or die
    alone; see "Workers" in docs/design.md.

    Log records and the outcome come back over a pipe only this job writes to.
    The child holds the only sending end, so its death shows up as end-of-file.
    """

    def __init__(self, job: str, jobConfig: DataJobConfig, databaseConfiguration: Dict[str, DatabaseConnectionConfig],
                 memory: MemoryBackend, logLevel: int, maskingThreads: int = 1) -> None:
        self.job = job
        self.startedAt = time.time()
        self.deadline = self.startedAt + jobConfig.timeoutSeconds if jobConfig.timeoutSeconds else None
        self._outcome: Optional[JobOutcome] = None
        self._closed = False

        self._connection, sendingEnd = PROCESS_CONTEXT.Pipe(duplex=False)
        # Nothing is ever sent on this second pipe: the child watches it for
        # the end-of-file that this end's closing -- or this process's death --
        # gives it. See _exitWhenOrphaned.
        childEnd, self._alive = PROCESS_CONTEXT.Pipe(duplex=False)
        self.process = PROCESS_CONTEXT.Process(
            target=_jobProcess, name='bauta {}'.format(job), daemon=True,
            args=(sendingEnd, childEnd, logLevel, job, jobConfig, databaseConfiguration, memory, maskingThreads))
        self.process.start()
        sendingEnd.close()
        childEnd.close()


    @property
    def waitables(self) -> List[Any]:

        return [self.process.sentinel] if self._closed else [self._connection, self.process.sentinel]


    def _read(self) -> None:
        """Handles what the job has sent, up to MESSAGES_PER_POLL messages.
        Marks the pipe closed at end-of-file.
        """

        for _ in range(MESSAGES_PER_POLL):
            if self._closed or not self._connection.poll():
                return
            try:
                kind, payload = self._connection.recv()
            except (EOFError, OSError):
                self._closed = True
                self._connection.close()
                return
            if kind == 'log':
                handleForwardedRecord(payload)
            elif kind == 'outcome':
                self._outcome = payload


    def poll(self, now: float) -> Optional[JobOutcome]:
        """The job's outcome once it is over -- finished, died or timed out --
        and None while it is still running.
        """

        self._read()

        if self._outcome is not None:
            # Nothing is left for it to do but exit; don't wait on it forever.
            self.process.join(EXIT_GRACE_SECONDS)
            if self.process.is_alive():
                logger.warning('{} sent its outcome but did not exit; stopping it'.format(self.job))
                self.stop()
            self._drain()
            return self._outcome

        if not self.process.is_alive():
            self._drain()
            if self._outcome is not None:
                return self._outcome
            return self._died()

        if self.deadline is not None and now >= self.deadline:
            self.stop()
            timeoutSeconds = self.deadline - self.startedAt
            logger.error('{} exceeded timeoutSeconds ({:g}) and was stopped'.format(self.job, timeoutSeconds),
                         extra={'job': self.job, 'status': JobStatus.FAILED.value})
            return self._failed('Timeout: stopped after exceeding timeoutSeconds ({:g})'.format(timeoutSeconds))

        return None


    def stop(self) -> None:
        """SIGTERM, then SIGKILL if it hasn't exited within the grace period.

        Its database connections close with it, so each server rolls back
        whatever the job had not committed.
        """

        self.process.terminate()
        self.process.join(TERMINATE_GRACE_SECONDS)

        if self.process.is_alive():
            self.process.kill()
            self.process.join()

        self._release()


    def _drain(self) -> None:
        """Handles whatever an exited job left in its pipe. Bounded, since an
        exited job can't write more.
        """

        while not self._closed:
            self._read()
            if not self._closed and not self._connection.poll():
                self._closed = True
                self._connection.close()

        self._release()


    def _release(self) -> None:
        """Closes both of the job's pipes, once the job is over. Idempotent, so
        a job that is stopped and then drained releases them once.
        """

        if not self._closed:
            self._closed = True
            self._connection.close()

        self._alive.close()


    def _died(self) -> JobOutcome:

        self.process.join()
        logger.error('{}: its process exited with code {} before reporting an outcome'.format(self.job, self.process.exitcode),
                     extra={'job': self.job, 'status': JobStatus.FAILED.value})

        return self._failed('WorkerDied: the job\'s process exited abruptly (code {}) -- killed, out of memory, or crashed'.format(
            self.process.exitcode))


    def _failed(self, error: str) -> JobOutcome:

        return JobOutcome(job=self.job, status=JobStatus.FAILED, error=error, startedAt=self.startedAt, finishedAt=time.time())

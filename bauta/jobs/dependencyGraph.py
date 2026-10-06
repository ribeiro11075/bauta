from __future__ import annotations

import time
from enum import Enum
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Set

from ..configuration import BaseJobConfig, ConfigurationError, findCycle


class JobStatus(str, Enum):
    COMPLETED = 'completed'
    FAILED = 'failed'
    SKIPPED = 'skipped'


class JobOutcome(NamedTuple):
    """What became of one job in one cycle. Crosses processes, so `error` is
    a string: driver exceptions don't reliably pickle.
    """

    job: str
    status: JobStatus
    rowCount: int = 0
    watermark: Any = None
    error: Optional[str] = None
    attempts: int = 1
    startedAt: float = 0.0
    finishedAt: float = 0.0
    # For a masked job that completed: the policy applied to each column, as
    # plain dicts so it pickles. See masking.buildMaskingManifest.
    masking: Optional[Dict[str, Any]] = None
    # For a job that completed: seconds busy reading, masking, writing and
    # waiting on a read limit. See throttle.StageTimes.
    stages: Optional[Dict[str, float]] = None

    @property
    def durationSeconds(self) -> float:

        return max(0.0, self.finishedAt - self.startedAt)


class DependencyGraph:
    """Which of one cycle's jobs may start, given which have finished.
    Bookkeeping only; it never waits. A cycle among the active jobs raises
    ConfigurationError.

    `connectionLimits` caps how many running jobs may use a connection alias
    at once (see ConnectionConfig.jobLimit). A job held back by one
    waits for a job on that connection to finish; the jobs behind it that
    use other connections start meanwhile.
    """

    def __init__(self, jobs: Mapping[str, BaseJobConfig], memory: Optional[Dict[str, float]] = None,
                 connectionLimits: Optional[Mapping[str, int]] = None) -> None:
        self.jobs = jobs
        self.memory = memory
        self.connectionLimits = dict(connectionLimits or {})
        self.activePredecessors: Dict[str, List[str]] = {}
        self.activeJobs = self._getActiveJobsWithActivePredecessors()
        self.outcomes: List[JobOutcome] = []
        self._notStarted: List[str] = list(self.activeJobs)
        self._running: Set[str] = set()
        # The places a job with `partitions: auto` was given as it started:
        # see reserve().
        self._reserved: Dict[str, int] = {}
        self._completed: Set[str] = set()
        self._unsuccessful: Set[str] = set()

        cycle = findCycle(self.activePredecessors)
        if cycle:
            raise ConfigurationError('predecessors form a cycle, so none of these jobs could ever start: {}'.format(' -> '.join(cycle)))


    def _getActiveJobsWithActivePredecessors(self) -> Dict[str, BaseJobConfig]:
        """Active jobs, excluding any still inside their `refresh` window per memory."""

        now = time.time()
        jobs = {
            name: job for name, job in self.jobs.items()
            if job.active and not (self.memory and name in self.memory and job.refresh and (now - self.memory[name]) / 60 < job.refresh)
            }

        for name, job in jobs.items():
            self.activePredecessors[name] = [predecessor for predecessor in job.predecessors if predecessor in jobs]

        return jobs


    @property
    def finished(self) -> bool:

        return not self._notStarted and not self._running


    def takeReady(self, limit: Optional[int] = None) -> List[str]:
        """Jobs that may start now -- at most `limit` of them -- marked as running.
        A job whose predecessor failed or was skipped is marked SKIPPED, which
        cascades in this same call.
        """

        ready: List[str] = []
        changed = True

        while changed:
            changed = False
            for job in list(self._notStarted):
                predecessors = self.activePredecessors[job]
                unsuccessful = [predecessor for predecessor in predecessors if predecessor in self._unsuccessful]

                if unsuccessful:
                    self._skip(job, 'predecessor(s) did not complete: {}'.format(', '.join(unsuccessful)))
                    changed = True
                elif ((limit is None or len(ready) < limit) and all(predecessor in self._completed for predecessor in predecessors)
                      and self._connectionsFree(job)):
                    self._notStarted.remove(job)
                    self._running.add(job)
                    ready.append(job)

        return ready


    def connections(self, job: str) -> Set[str]:
        """The connection aliases `job` uses, each once: a job reading and
        writing one connection takes one of its places, not two.
        """

        config = self.jobs[job]

        return {alias for alias in (getattr(config, 'sourceConnection', None), getattr(config, 'targetConnection', None)) if alias}


    def places(self, job: str) -> int:
        """How many of a limited connection's places `job` holds while it runs:
        one, or one for each of its partitions, each of which opens a
        connection of its own.
        """

        partitions = getattr(self.jobs[job], 'partitions', None)
        if partitions is None:
            return 1
        if partitions.automatic:
            # One to start, as any job; then what reserve() gave it.
            return self._reserved.get(job, 1)

        return int(partitions.count)


    def freePlaces(self, job: str) -> Optional[int]:
        """The most places `job` could hold on each limited connection it uses,
        beside the other running jobs, or None where none is limited.
        """

        free = [self.connectionLimits[alias] - sum(self.places(other) for other in self._running if other != job and alias in self.connections(other))
                for alias in self.connections(job) & set(self.connectionLimits)]

        return max(1, min(free)) if free else None


    def reserve(self, job: str, places: int) -> None:
        """Records how many places a just-started job with an automatic
        partition count holds, so the jobs started after it see them taken.
        The job may use fewer; it never uses more.
        """

        self._reserved[job] = places


    def _connectionsFree(self, job: str) -> bool:
        """A job needing more places than a connection has -- which validation
        refuses -- waits for the connection to be free of everything else
        rather than for ever.
        """

        for alias in self.connections(job) & set(self.connectionLimits):
            limit = self.connectionLimits[alias]
            inUse = sum(self.places(other) for other in self._running if alias in self.connections(other))
            if inUse and inUse + min(self.places(job), limit) > limit:
                return False

        return True


    def finish(self, outcome: JobOutcome) -> None:

        self._running.discard(outcome.job)
        self.outcomes.append(outcome)
        (self._completed if outcome.status == JobStatus.COMPLETED else self._unsuccessful).add(outcome.job)


    def skipNotStarted(self, reason: str) -> None:
        """Records every job that hasn't started as SKIPPED -- for a run that is
        shutting down and must not start anything new.
        """

        for job in list(self._notStarted):
            self._skip(job, reason)


    def _skip(self, job: str, reason: str) -> None:

        self._notStarted.remove(job)
        self.finish(JobOutcome(job=job, status=JobStatus.SKIPPED, error=reason))

"""What a job spends its time on, and how fast it may read: per-stage timings
for history, and the read limit a connection's maxRowsReadPerSecond sets.

A limit is one budget per connection, shared by every job and every
partition reading from it, so a run's processes share one schedule: the run
creates it, and each job's process is handed it as it starts. A job run
from Python, a test or `bauta bench` gets one of its own. See "Limiting what
a job reads" in docs/guides/make-it-faster.md.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Generator, Iterable, List, Mapping, Optional, Tuple

from ..configuration import ConnectionConfig

# How much reading may run ahead of the limit after a pause, in seconds of it:
# enough that a limit doesn't slow a job that reads below it on average, too
# little to let a burst past it.
BURST_SECONDS = 1.0

STAGES = ('read', 'mask', 'write', 'throttled')


class StageTimes:
    """Seconds a job's stages were busy: reading from the source (and nothing
    else), transforming and masking, writing to the target (with whatever
    the target does to finish, a swap or an upsert from the stage), and
    waiting on a read limit. Thread-safe, since a job's partitions and its
    masking thread add to one.

    Busy, not elapsed: reading, masking and writing overlap, so they add up
    to more than the job's time, and the largest is what sets its pace. A
    job's partitions add theirs together.
    """

    def __init__(self) -> None:
        self._seconds = dict.fromkeys(STAGES, 0.0)
        self._lock = threading.Lock()


    def add(self, stage: str, seconds: float) -> None:

        with self._lock:
            self._seconds[stage] += seconds


    def asDict(self) -> Dict[str, float]:

        with self._lock:
            return {stage: round(seconds, 3) for stage, seconds in self._seconds.items()}


class ReadLimit:
    """At most `rowsPerSecond` rows read through it, by whoever shares it.

    A schedule rather than a counter: `next` is when the rows already read
    are paid for, and each chunk moves it on by its rows' share of a second.
    A reader whose chunk lands it in the future sleeps until then. `next`
    never lags the clock by more than BURST_SECONDS, so a pause earns no more
    than that much credit.

    `shared` is a multiprocessing double, with its lock, where the limit
    spans processes; otherwise it is this process's own.
    """

    def __init__(self, rowsPerSecond: float, shared: Any = None) -> None:
        self.rowsPerSecond = rowsPerSecond
        self._shared = shared
        self._next = float('-inf')
        self._lock = shared.get_lock() if shared is not None else threading.Lock()


    def take(self, rows: int) -> float:
        """Charges `rows` against the limit, sleeping as long as they owe.
        Returns the seconds slept.
        """

        if rows <= 0:
            return 0.0

        with self._lock:
            # time.monotonic is the system's clock on Linux and macOS, the
            # same in every process, which is what makes a shared `next` mean
            # the same moment to each.
            now = time.monotonic()
            scheduled = self._shared.value if self._shared is not None else self._next
            paidUntil = max(scheduled, now - BURST_SECONDS) + rows / self.rowsPerSecond
            if self._shared is not None:
                self._shared.value = paidUntil
            else:
                self._next = paidUntil

        delay = paidUntil - now
        if delay <= 0:
            return 0.0

        time.sleep(delay)

        return delay


def sharedReadLimits(connectionConfiguration: Mapping[str, ConnectionConfig], context: Any) -> Dict[str, Any]:
    """Alias -> a value of `context` (a multiprocessing context) to share
    each limited connection's schedule between a run's processes, which
    readLimitFor takes up in each.
    """

    return {alias: context.Value('d', float('-inf')) for alias, settings in connectionConfiguration.items()
            if getattr(settings, 'maxRowsReadPerSecond', None) is not None}


_shared: Dict[str, Any] = {}
_limits: Dict[str, ReadLimit] = {}
_limitsLock = threading.Lock()


def setSharedReadLimits(shared: Mapping[str, Any]) -> None:
    """What the run handed this job's process, from sharedReadLimits."""

    with _limitsLock:
        _shared.clear()
        _shared.update(shared)
        _limits.clear()


def readLimitFor(alias: str, settings: ConnectionConfig) -> Optional[ReadLimit]:
    """The read limit on connection `alias`, or None where it has none: the
    run's, shared with its other jobs, or else one for this process alone.
    The same object for every caller in a process, so a job's partitions
    share it.
    """

    rowsPerSecond = getattr(settings, 'maxRowsReadPerSecond', None)
    if rowsPerSecond is None:
        return None

    with _limitsLock:
        # A caller in one process -- Python, a test -- may change the rate.
        if alias not in _limits or _limits[alias].rowsPerSecond != rowsPerSecond:
            _limits[alias] = ReadLimit(rowsPerSecond, _shared.get(alias))
        return _limits[alias]


def timedChunks(chunks: Iterable[List[Tuple[Any, ...]]], times: Optional[StageTimes],
                limit: Optional[ReadLimit] = None) -> Generator[List[Tuple[Any, ...]], None, None]:
    """`chunks`, each timed as reading and charged to `limit`, whose wait is
    timed apart from it. Closes `chunks` when it is closed, so a stream
    stopped early -- a failed write, `bauta bench` reading enough -- lets go
    of its cursor.
    """

    iterator = iter(chunks)
    try:
        while True:
            started = time.perf_counter()
            try:
                chunk = next(iterator)
            except StopIteration:
                return
            finally:
                if times is not None:
                    times.add('read', time.perf_counter() - started)
            if limit is not None:
                waited = limit.take(len(chunk))
                if times is not None and waited:
                    times.add('throttled', waited)
            yield chunk
    finally:
        getattr(chunks, 'close', lambda: None)()

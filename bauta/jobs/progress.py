"""The rows a running job has written so far, for the run to show while it
goes: counted as each chunk lands, by every partition of the job, and handed
to a sink at most once every PROGRESS_INTERVAL_SECONDS. In a job's process
the sink sends it to the run over the job's pipe; elsewhere there is none,
and counting costs a lock and an addition.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

PROGRESS_INTERVAL_SECONDS = 1.0

_lock = threading.Lock()
_rows = 0
_lastSent = 0.0
_sink: Optional[Callable[[int], None]] = None


def setProgressSink(sink: Optional[Callable[[int], None]]) -> None:
    """Where this process's count goes, starting again from nothing."""

    global _rows, _lastSent, _sink

    with _lock:
        _rows, _lastSent, _sink = 0, 0.0, sink


def addRows(count: int) -> None:
    """Counts `count` more rows written, and passes the total on if the last
    went long enough ago. A sink that fails is dropped: progress is for
    watching, and never fails a load.
    """

    global _rows, _lastSent, _sink

    with _lock:
        _rows += count
        now = time.monotonic()
        if _sink is None or now - _lastSent < PROGRESS_INTERVAL_SECONDS:
            return
        _lastSent = now
        try:
            _sink(_rows)
        except Exception:
            _sink = None

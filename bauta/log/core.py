from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from .scrubbing import foreignMessages, scrubForeignText, scrubWithin

LOGGER_NAME = 'bauta'

TEXT_FORMAT = '%(asctime)s.%(msecs)03d [%(levelname)s] :: %(message)s [%(filename)s:%(lineno)d]'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

# Everything LogRecord sets on itself. Anything *not* in here arrived through a
# log call's extra={...} and is a field the caller wanted in the output.
_RESERVED_RECORD_FIELDS = frozenset({
    'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename', 'funcName', 'levelname', 'levelno', 'lineno',
    'message', 'module', 'msecs', 'msg', 'name', 'pathname', 'process', 'processName', 'relativeCreated', 'stack_info',
    'taskName', 'thread', 'threadName',
    })


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for a log collector, carrying each record's
    extra={...} fields. Anything unserializable falls back to str().
    """

    def format(self, record: logging.LogRecord) -> str:

        payload: Dict[str, Any] = {
            'timestamp': self.formatTime(record, DATE_FORMAT) + '.{:03.0f}'.format(record.msecs),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'file': record.filename,
            'line': record.lineno,
            }

        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_FIELDS and not key.startswith('_'):
                payload[key] = value

        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload['exception'] = record.exc_text

        return json.dumps(payload, default=str)


# The `event` of the record a job's last failed attempt logs, with its
# traceback; the cycle's summary reports the same failure in a line.
ATTEMPT_FAILED = 'attemptFailed'


class _OneLineFormatter(logging.Formatter):
    """A record as `formatter` writes it, less any traceback. Text keeps
    only the level and message, since a line on a terminal or in cron mail
    needs no timestamp or source line.
    """

    def __init__(self, formatter: logging.Formatter) -> None:
        super().__init__()
        self._json = formatter if isinstance(formatter, JsonFormatter) else None

    def format(self, record: logging.LogRecord) -> str:

        record = copy.copy(record)
        record.exc_info = None
        record.exc_text = None

        if self._json is not None:
            return self._json.format(record)

        return '[{}] {}'.format(record.levelname, record.getMessage())


class _ErrorStreamHandler(logging.StreamHandler):
    """The handler addErrorStream adds, told apart from addStreamHandler's."""


class ScrubbingFilter(logging.Filter):
    """Removes quoted data values from a record's message and exception text.
    On the logger rather than its handlers, so it covers handlers a caller
    adds and records forwarded from job processes.

    Where the record carries an exception, the message of each error behind
    it that came from outside the package -- a driver's -- is scrubbed as
    scrubForeignText does, wherever it appears in the message or the traceback.
    """

    def filter(self, record: logging.LogRecord) -> bool:

        try:
            message = record.getMessage()
        except Exception:
            # A malformed call is the handler's to report, as it would be without this.
            return True

        error = record.exc_info[1] if record.exc_info else None
        record.msg = scrubWithin(message, error)
        record.args = None

        if record.exc_info:
            record.exc_text = scrubWithin(logging.Formatter().formatException(record.exc_info), error)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = scrubWithin(record.exc_text, None)

        return True


def _installScrubbing() -> None:

    logger = logging.getLogger(LOGGER_NAME)
    if not any(isinstance(existing, ScrubbingFilter) for existing in logger.filters):
        logger.addFilter(ScrubbingFilter())


# At import, so a job process -- which imports this and builds no Log -- scrubs
# its records before they are forwarded.
_installScrubbing()


# The loggers of the drivers and libraries that touch rows. Their records
# never reach the package's logger, so its filter can't see them: a driver
# logging a statement or an error goes wherever the host's logging sends it,
# or to Python's last resort on stderr.
DRIVER_LOGGERS = frozenset({'psycopg', 'psycopg_pool', 'oracledb', 'mysql', 'pymssql', 'duckdb', 'pyiceberg', 'pyarrow'})


def _scrubDriverRecord(record: logging.LogRecord) -> None:
    """A driver's record scrubbed as a foreign message, in place."""

    try:
        message = record.getMessage()
    except Exception:
        return

    error = record.exc_info[1] if record.exc_info else None
    record.msg = scrubForeignText(message)
    record.args = None
    if record.exc_info:
        traceback = logging.Formatter().formatException(record.exc_info)
        for foreign in foreignMessages(error):
            traceback = traceback.replace(foreign, scrubForeignText(foreign))
        record.exc_text = scrubForeignText(traceback)
        record.exc_info = None


def installDriverScrubbing() -> None:
    """Scrubs every record the drivers' own loggers make in this process, as
    it is made: a record factory, since a filter on a logger misses records
    made by its children. Installed by Log and in each job process, never at
    import, so a host program importing the package keeps its logging as it was.
    """

    previous = logging.getLogRecordFactory()
    if getattr(previous, 'scrubsDrivers', False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        if record.name.partition('.')[0] in DRIVER_LOGGERS:
            _scrubDriverRecord(record)
        return record

    factory.scrubsDrivers = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


def portableRecord(record: logging.LogRecord) -> logging.LogRecord:
    """A copy of `record` that can be pickled into another process: arguments
    resolved and the exception as text, but unformatted, so the receiver can
    still write JSON.
    """

    record = copy.copy(record)
    if record.exc_info:
        record.exc_text = logging.Formatter().formatException(record.exc_info)
    record.msg = record.getMessage()
    record.args = None
    record.exc_info = None

    return record


class ConnectionForwarder(logging.Handler):
    """Sends a job process's records to the main process over its own pipe --
    not a shared queue, whose lock a killed job could leave held.

    `send` shares the handler's lock, so the outcome never interleaves with a
    record.
    """

    def __init__(self, connection: Any) -> None:
        super().__init__()
        self.connection = connection


    def send(self, kind: str, payload: Any) -> None:

        self.acquire()
        try:
            self.connection.send((kind, payload))
        finally:
            self.release()


    def emit(self, record: logging.LogRecord) -> None:

        try:
            self.connection.send(('log', portableRecord(record)))
        except Exception:
            self.handleError(record)


def forwardToConnection(connection: Any, level: int) -> ConnectionForwarder:
    """Routes this process's package records to `connection`, and nowhere else.
    Called in each job process. Any existing handlers are detached, not closed.
    """

    installDriverScrubbing()
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    forwarder = ConnectionForwarder(connection)
    logger.addHandler(forwarder)
    logger.setLevel(level)
    logger.propagate = False

    return forwarder


def handleForwardedRecord(record: logging.LogRecord) -> None:
    """Writes a record forwarded from a job process with this process's own
    handlers.
    """

    logging.getLogger(LOGGER_NAME).handle(record)


class Log:

    def __init__(self, logFile: Optional[Path] = None, level: int = logging.INFO, logFormat: str = 'text') -> None:
        """Configures the package's own logger, which doesn't propagate, so a
        host application's logging is left alone.

        Handlers are deduplicated by destination, since the logger is
        process-wide: configuring one again only updates its level. logFormat
        is 'text' or 'json'.
        """

        formatter: logging.Formatter = JsonFormatter() if logFormat == 'json' else logging.Formatter(fmt=TEXT_FORMAT, datefmt=DATE_FORMAT)

        installDriverScrubbing()
        self.logging = logging.getLogger(LOGGER_NAME)
        self.logging.setLevel(level)
        self.logging.propagate = False
        self._formatter = formatter

        if logFile is None:
            return

        path = Path(logFile).resolve()
        existing = self._findHandler(path)

        if existing is not None:
            existing.setLevel(level)
            return

        handler = logging.FileHandler(filename=str(path))
        handler.setLevel(level)
        handler.setFormatter(formatter)
        self.logging.addHandler(handler)


    def addStreamHandler(self, stream: Any, level: int = logging.INFO) -> None:
        """Send records to a stream as well as, or instead of, a file.
        Deduplicated on the stream.
        """

        for handler in self.logging.handlers:
            if (isinstance(handler, logging.StreamHandler) and not isinstance(handler, (logging.FileHandler, _ErrorStreamHandler))
                    and handler.stream is stream):
                handler.setLevel(level)
                return

        handler = logging.StreamHandler(stream=stream)
        handler.setLevel(level)
        handler.setFormatter(self._formatter)
        self.logging.addHandler(handler)


    def addErrorStream(self, stream: Any) -> None:
        """What --quiet leaves on a stream: each error as one line, without
        its traceback, and a job's failure once rather than once per attempt
        and again in the cycle's summary. Under cron that is the mail sent
        when a run fails, and nothing when it succeeds. With no handler at all,
        Python's own last resort would print every warning and traceback.
        """

        # One per process, replacing the last: a command run again in the
        # same process -- a test, a host program -- may bring a new stream,
        # and the old one may be closed.
        for existing in [handler for handler in self.logging.handlers if isinstance(handler, _ErrorStreamHandler)]:
            self.logging.removeHandler(existing)

        handler = _ErrorStreamHandler(stream=stream)
        handler.setLevel(logging.ERROR)
        handler.setFormatter(_OneLineFormatter(self._formatter))
        handler.addFilter(lambda record: getattr(record, 'event', None) != ATTEMPT_FAILED)
        self.logging.addHandler(handler)


    def _findHandler(self, path: Path) -> Optional[logging.FileHandler]:
        """The handler already writing to `path`, compared through symlinks."""

        for handler in self.logging.handlers:
            if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).resolve() == path:
                return handler

        return None

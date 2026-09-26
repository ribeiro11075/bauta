from __future__ import annotations

import contextlib
import datetime
import decimal
import os
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple, TypeVar, Union

import yaml

from ..configuration import ConfigurationError, ConnectionConfig, DatabaseType
from ..database import Database

# Locks are taken on a separate, empty `.lock` file rather than on the data
# file itself, because the data file is replaced on every write: a lock held on
# the old inode would guard nothing once the new one is renamed into place.
if sys.platform == 'win32':
    import msvcrt

    def _lock(file: Any, blocking: bool = True) -> None:
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)

    def _unlock(file: Any) -> None:
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(file: Any, blocking: bool = True) -> None:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(file: Any) -> None:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def exclusiveLock(path: Path, blocking: bool = True) -> Iterator[None]:
    """Holds an exclusive lock on `path`, creating it if needed. Raises
    OSError at once if `blocking` is False and someone else holds it.
    """

    with open(path, 'a') as file:
        _lock(file, blocking=blocking)
        try:
            yield
        finally:
            _unlock(file)


class RunInProgressError(Exception):
    """Another run already holds the run lock."""


@contextlib.contextmanager
def exclusiveRun(lockFile: Union[str, Path]) -> Iterator[None]:
    """Holds `lockFile` for the life of a run, or raises RunInProgressError, so
    two runs sharing run state can't run the same jobs at once. The operating
    system releases it if the process dies.
    """

    try:
        with exclusiveLock(Path(lockFile), blocking=False):
            yield
    except OSError as error:
        raise RunInProgressError('another run is already using {} -- not starting a second one alongside it'.format(lockFile)) from error


_Backend = TypeVar('_Backend', bound='MemoryBackend')


class MemoryBackend(ABC):
    """Tracks each job's last-run time, wherever an implementation keeps it.

    Must be picklable, since each job's process gets a copy: hold the settings
    a resource is opened from rather than the resource. One that keeps a
    connection open between calls -- DatabaseMemory does -- leaves it out of
    what it pickles and opens its own in whichever process first needs it.

    Whoever creates a backend closes it, with close() or by using it as a
    context manager; each job's process closes its own copy when the job ends.
    """

    def close(self) -> None:
        """Releases whatever this process holds open. Idempotent; the default
        holds nothing.
        """

    def __enter__(self: _Backend) -> _Backend:

        return self

    def __exit__(self, *exception: Any) -> None:

        self.close()

    @abstractmethod
    def read(self) -> Dict[str, float]:
        """Every job's last-run time, keyed by job name."""

    @abstractmethod
    def recordRun(self, job: str) -> None:
        """Records that `job` just ran, now."""

    @abstractmethod
    def readWatermarks(self) -> Dict[str, Any]:
        """Every job's stored watermark, keyed by job name."""

    @abstractmethod
    def recordWatermark(self, job: str, value: Any) -> None:
        """Records the high-water mark `job` reached, for its next run to resume
        from.
        """

    @abstractmethod
    def readKeyFingerprints(self) -> Dict[str, str]:
        """The masking identity (see masking.maskingIdentity) each masked job
        last completed under, which is how a changed key is caught before it
        mixes two keys' masks in one target.
        """

    @abstractmethod
    def recordKeyFingerprint(self, job: str, fingerprint: Optional[str]) -> None:
        """Records the identity `job` completed under; None forgets it."""


class _MemoryLoader(yaml.SafeLoader):
    """SafeLoader that also reads `!decimal` scalars."""


class _MemoryDumper(yaml.SafeDumper):
    """SafeDumper that also writes Decimal, which Oracle returns for every
    NUMBER, exactly, as `!decimal`. As a float it would round, and a watermark
    rounded up skips the rows below it for good.
    """


def _representDecimal(dumper: yaml.SafeDumper, value: decimal.Decimal) -> yaml.ScalarNode:

    return dumper.represent_scalar('!decimal', str(value))


def _constructDecimal(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> decimal.Decimal:

    return decimal.Decimal(str(loader.construct_scalar(node)))


_MemoryDumper.add_representer(decimal.Decimal, _representDecimal)
_MemoryLoader.add_constructor('!decimal', _constructDecimal)


SECTIONS = ('lastRun', 'watermarks', 'maskingKeys')


class FileMemory(MemoryBackend):
    """The default MemoryBackend: a YAML file, safe to share across worker processes.

    Every write re-reads the file under `<file>.lock`, so concurrent workers
    don't lose each other's updates, and replaces it through a temporary file,
    so a process killed mid-write leaves the previous version. The document:

        lastRun:
          loadOrders: 1726400000.0
        watermarks:
          loadOrders: 2026-09-15 10:00:00
          loadEvents: !decimal '12345678901234567.1'
        maskingKeys:
          maskCustomers: d5930cf83dea

    A bare job -> timestamp mapping, from before watermarks, is read as lastRun.
    """

    def __init__(self, memoryFile: Union[str, Path]) -> None:
        self.memoryFile = Path(memoryFile)
        self._lockFile = self.memoryFile.with_name(self.memoryFile.name + '.lock')


    def _load(self) -> Dict[str, Any]:
        """A missing file means nothing has been recorded yet and reads as
        empty, rather than raising -- a fresh checkout has no memory file at all.
        """

        try:
            with open(self.memoryFile) as file:
                document = yaml.load(file, Loader=_MemoryLoader) or {}
        except FileNotFoundError:
            document = {}

        if document and 'lastRun' not in document and 'watermarks' not in document:
            return {'lastRun': document, 'watermarks': {}}

        for section in SECTIONS:
            document.setdefault(section, {})

        return document


    def _read(self, section: str) -> Dict[str, Any]:

        # Checked first so that reading never creates a lock file beside a
        # memory file that doesn't exist yet.
        if not self.memoryFile.exists():
            return {}

        with exclusiveLock(self._lockFile):
            return dict(self._load()[section])


    def _write(self, section: str, job: str, value: Any) -> None:

        temporary = self.memoryFile.with_name(self.memoryFile.name + '.tmp')
        self.memoryFile.parent.mkdir(parents=True, exist_ok=True)

        with exclusiveLock(self._lockFile):
            document = self._load()
            if value is None:
                document[section].pop(job, None)
            else:
                document[section][job] = value

            with open(temporary, 'w') as file:
                # The dumper refuses a type the loader couldn't read back, so an
                # unsupported watermark fails here rather than corrupting the file.
                yaml.dump(document, file, Dumper=_MemoryDumper, default_flow_style=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.memoryFile)


    def read(self) -> Dict[str, float]:

        return self._read('lastRun')


    def recordRun(self, job: str) -> None:

        self._write('lastRun', job, time.time())


    def readWatermarks(self) -> Dict[str, Any]:

        return self._read('watermarks')


    def recordWatermark(self, job: str, value: Any) -> None:

        self._write('watermarks', job, value)


    def readKeyFingerprints(self) -> Dict[str, str]:

        return self._read('maskingKeys')


    def recordKeyFingerprint(self, job: str, fingerprint: Optional[str]) -> None:

        self._write('maskingKeys', job, fingerprint)


DATABASE_MEMORY_SCHEMA = """CREATE TABLE bauta_memory (
    job VARCHAR(255) PRIMARY KEY,
    last_run DOUBLE PRECISION,
    watermark_value VARCHAR(255),
    watermark_type VARCHAR(32)
    )"""


KEY_FINGERPRINT_SUFFIX = '#maskingKey'
KEY_FINGERPRINT_TYPE = 'maskingKey'


class DatabaseMemory(MemoryBackend):
    """A MemoryBackend that keeps run state in a database table, for wherever
    no filesystem persists between runs and is shared by every worker. See
    "Where watermarks are kept" in docs/design.md.

    The table must already exist, shaped like DATABASE_MEMORY_SCHEMA. Each
    write upserts only its own columns, so recordRun and recordWatermark don't
    clobber each other -- and last_run is nullable, since a watermark is
    recorded first. A watermark is stored as text with a type tag, so it comes
    back as the type the source compares against.
    """

    def __init__(self, connectionSettings: ConnectionConfig, table: str = 'bauta_memory') -> None:
        if connectionSettings.type == DatabaseType.DUCKDB:
            # The run holds this connection open for as long as it lasts, and
            # DuckDB lets one process at a time open a file, so every job that
            # recorded its run would wait on the run itself, then fail.
            raise ConfigurationError('run state cannot be kept in DuckDB ({}): DuckDB lets one process at a time open a file, and the run '
                                     'and each job are processes of their own. Keep it in a file, or in a server database'.format(
                                         connectionSettings.describeTarget()))
        self.connectionSettings = connectionSettings
        self.table = table
        self._database: Optional[Database] = None
        self._pid: Optional[int] = None


    def __getstate__(self) -> Dict[str, Any]:
        """What is pickled into each job's process. A connection can't cross,
        so the copy that arrives opens its own on first use.
        """

        return dict(self.__dict__, _database=None, _pid=None)


    def _connected(self) -> Database:
        """This process's connection, opened the first time it is needed.

        Held rather than reopened per call: a completed masked incremental job
        records a watermark, a key fingerprint and a run, and reads its
        watermark on every attempt -- four connections, and four subprocesses
        besides wherever passwordCommand supplies a cloud IAM token.

        Keyed on the process id as well, so a copy that reached a worker some
        other way than the pickle above never writes through its parent's
        connection.
        """

        pid = os.getpid()

        if self._database is None or self._pid != pid:
            self.close()
            self._database = Database(connectionSettings=self.connectionSettings)
            self._pid = pid

        return self._database


    def close(self) -> None:
        """Closes the connection this process holds, if it holds one, so the
        server sees a clean disconnect rather than a dropped socket. Idempotent.
        """

        database, self._database, self._pid = self._database, None, None

        if database is not None:
            try:
                database.close()
            except Exception:
                pass


    def _run(self, work: 'Callable[[Database], Any]') -> Any:
        """`work` against this process's connection, once more on a fresh one if
        a connection that was already open failed -- a server restart, an idle
        timeout, an expired token.

        Only a reused connection is retried: a failure on one opened in this
        same call is the statement's, not the connection's, and nothing runs
        twice because a statement was wrong.
        """

        reused = self._database is not None and self._pid == os.getpid()

        try:
            return work(self._connected())
        except Exception:
            if not reused:
                raise
            self.close()
            return work(self._connected())


    def _query(self, statement: str) -> Any:

        def work(database: Database) -> Any:
            rows = database.query(statement.format(database.statementName(self.table)))
            # Reading opens a transaction on PostgreSQL, which on a held
            # connection would stay open for the life of the run -- idle in
            # transaction, which holds back vacuum and trips server timeouts.
            database.rollback()
            return rows

        return self._run(work)


    def _upsert(self, data: Any, columns: Any) -> None:

        self._run(lambda database: database.upsert(table=self.table, data=data, columns=columns))


    @staticmethod
    def _encodeWatermark(value: Any) -> Tuple[str, str]:

        if isinstance(value, datetime.datetime):
            return value.isoformat(), 'datetime'
        if isinstance(value, datetime.date):
            return value.isoformat(), 'date'
        if isinstance(value, datetime.time):
            return value.isoformat(), 'time'
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value), 'int'
        if isinstance(value, float):
            return repr(value), 'float'
        if isinstance(value, decimal.Decimal):
            return str(value), 'decimal'
        # SQL Server's rowversion, the usual way to track changes there.
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).hex(), 'bytes'

        return str(value), 'str'


    @staticmethod
    def _decodeWatermark(text: str, typeTag: str) -> Any:

        if typeTag == 'datetime':
            return datetime.datetime.fromisoformat(text)
        if typeTag == 'date':
            return datetime.date.fromisoformat(text)
        if typeTag == 'int':
            return int(text)
        if typeTag == 'float':
            return float(text)
        if typeTag == 'time':
            return datetime.time.fromisoformat(text)
        if typeTag == 'decimal':
            return decimal.Decimal(text)
        if typeTag == 'bytes':
            return bytes.fromhex(text)

        return text


    # The table is written into each statement the way a load already writes
    # it -- quoted, and folded as this database folds an unquoted name -- so a
    # table whose name needs quotes is read as well as written.

    def read(self) -> Dict[str, float]:
        """Skips rows whose last_run is still NULL: a watermark recorded
        before a run.
        """

        rows = self._query('SELECT job, last_run FROM {}')

        return {job: lastRun for job, lastRun in rows if lastRun is not None}


    def recordRun(self, job: str) -> None:

        self._upsert([(job, time.time())], ['job', 'last_run'])


    def readWatermarks(self) -> Dict[str, Any]:

        rows = self._query('SELECT job, watermark_value, watermark_type FROM {}')

        return {job: self._decodeWatermark(value, typeTag) for job, value, typeTag in rows
                if value is not None and typeTag != KEY_FINGERPRINT_TYPE}


    def readKeyFingerprints(self) -> Dict[str, str]:
        """Kept in rows of their own, named `<job>#maskingKey`, so the table
        needs no new column: their type tag keeps them out of readWatermarks,
        and their NULL last_run out of read().
        """

        rows = self._query("SELECT job, watermark_value FROM {{}} WHERE watermark_type = '{}'".format(KEY_FINGERPRINT_TYPE))

        return {job[:-len(KEY_FINGERPRINT_SUFFIX)]: value for job, value in rows if job.endswith(KEY_FINGERPRINT_SUFFIX) and value}


    def recordKeyFingerprint(self, job: str, fingerprint: Optional[str]) -> None:

        self._upsert([(job + KEY_FINGERPRINT_SUFFIX, fingerprint, KEY_FINGERPRINT_TYPE)],
                     ['job', 'watermark_value', 'watermark_type'])


    def recordWatermark(self, job: str, value: Any) -> None:

        text, typeTag = self._encodeWatermark(value)

        self._upsert([(job, text, typeTag)], ['job', 'watermark_value', 'watermark_type'])

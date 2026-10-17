"""Runs started from Studio: each a `bauta run` process, exactly as cron
would start it, so it takes the same lock, writes the same run state,
history and manifest, and a run started here and one from cron never
overlap on the same jobs.

The process is in a session of its own, so it finishes even if Studio is
restarted while it runs; its output goes to a log file in the data
directory. Studio runs as one process, which holds the handles.
"""
from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .db import LaunchedRun, User, utcnow
from .settings import Settings

# Read back for the run page: the end of a long log is what matters.
LOG_TAIL_BYTES = 256 * 1024
# How `bauta run` names each cycle it starts, in its own log.
CYCLE_STARTED = re.compile(r'Starting cycle ([0-9a-f-]{36}) with')


class Launcher:

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._processes: Dict[int, subprocess.Popen] = {}
        self._lock = threading.Lock()


    def command(self, jobs: List[str], fullRefresh: bool = False) -> List[str]:
        """`bauta run` for `jobs`, forced as `--job` forces them, or every job
        that is due when none are named.
        """

        command = list(self.settings.bautaCommand) + ['run', '--config', str(self.settings.configDirectory)]
        for job in jobs:
            command += ['--job', job]
        if fullRefresh:
            command.append('--full-refresh')

        return command


    def start(self, session: Session, user: User, jobs: List[str], fullRefresh: bool = False) -> LaunchedRun:

        self.settings.runLogDirectory.mkdir(parents=True, exist_ok=True)
        launched = LaunchedRun(userId=user.id, email=user.email, jobs=jobs, options={'fullRefresh': fullRefresh})
        session.add(launched)
        session.flush()

        logPath = self.settings.runLogDirectory / '{}.log'.format(launched.id)
        with open(logPath, 'ab') as log:
            process = subprocess.Popen(self.command(jobs, fullRefresh), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       cwd=str(self.settings.configDirectory.parent), start_new_session=True)
        launched.pid = process.pid
        launched.logPath = str(logPath)
        session.commit()

        with self._lock:
            self._processes[launched.id] = process

        return launched


    def refresh(self, session: Session, launched: LaunchedRun, liveRunIds: Dict[int, str]) -> None:
        """Records what has become of a launched run: its run id, once its
        live status names its process, and its exit code once it ends.
        """

        changed = False
        if launched.runId is None:
            launched.runId = liveRunIds.get(launched.pid) if launched.pid is not None else None
            if launched.runId is None:
                launched.runId = self._runIdFromLog(launched)
            changed = launched.runId is not None

        if launched.finishedAt is None:
            with self._lock:
                process = self._processes.get(launched.id)
            if process is not None:
                code = process.poll()
                if code is not None:
                    launched.exitCode, launched.finishedAt = code, utcnow()
                    with self._lock:
                        self._processes.pop(launched.id, None)
                    changed = True
            elif launched.pid is not None and not _alive(launched.pid):
                # Started before Studio restarted: it ended, but how is
                # only in its log.
                launched.finishedAt = utcnow()
                changed = True

        if changed:
            session.commit()


    def _runIdFromLog(self, launched: LaunchedRun) -> Optional[str]:
        """The run id its own log gives its cycle: what links a run that
        ended before its live status was seen. A --forever run has many;
        Studio starts single runs, which have one.
        """

        text = self.log(launched)
        found = CYCLE_STARTED.search(text or '')

        return found.group(1) if found else None


    def cancel(self, launched: LaunchedRun) -> bool:
        """Asks a run Studio started, and still holds, to stop: see cancelRun.
        False if Studio no longer holds it, having restarted since.
        """

        with self._lock:
            process = self._processes.get(launched.id)
        if process is None or process.poll() is not None:
            return False
        process.terminate()

        return True


    def log(self, launched: LaunchedRun) -> Optional[str]:

        path = Path(launched.logPath) if launched.logPath else None
        if path is None or not path.exists():
            return None
        with open(path, 'rb') as file:
            file.seek(max(0, path.stat().st_size - LOG_TAIL_BYTES))
            return file.read().decode('utf-8', errors='replace')


# The longest a benchmark may run before it is stopped.
BENCH_TIMEOUT_SECONDS = 1800


def startBench(settings: Settings, database: Any, email: str, job: str, rows: int) -> Dict[str, Any]:
    """Measures `job` as `bauta bench` does -- reading up to `rows` rows from
    its real source and masking them, writing nothing -- in a process of its
    own, since bench sets masking threads for the whole process. Kept as a
    check, `running` until it is done.
    """

    import json

    from .db import Check

    with database.open() as session:
        check = Check(kind='bench', email=email, result={'job': job, 'rows': rows, 'status': 'running'})
        session.add(check)
        session.commit()
        checkId = check.id
        started = check.asDict()

    command = list(settings.bautaCommand) + ['bench', '--config', str(settings.configDirectory), '--job', job, '--rows', str(rows),
                                             '--format', 'json', '--quiet']

    def measure() -> None:
        try:
            finished = subprocess.run(command, capture_output=True, text=True, timeout=BENCH_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
                                      cwd=str(settings.configDirectory.parent))
            report = json.loads(finished.stdout) if finished.stdout.strip() else None
            result = {'status': 'done', 'report': report} if report is not None else \
                {'status': 'failed', 'problem': (finished.stderr.strip() or 'bench exited {}'.format(finished.returncode))[-2000:]}
        except subprocess.TimeoutExpired:
            result = {'status': 'failed', 'problem': 'stopped after {} seconds'.format(BENCH_TIMEOUT_SECONDS)}
        except Exception as error:
            result = {'status': 'failed', 'problem': str(error)[:2000]}
        with database.open() as session:
            check = session.get(Check, checkId)
            if check is not None:
                check.result = dict(check.result, **result)
                session.commit()

    threading.Thread(target=measure, name='studio-bench-{}'.format(job), daemon=True).start()

    return started


class CancelRefused(Exception):
    """A run that can't be cancelled from here, and why."""


def commandOf(pid: int) -> List[str]:
    """The process's command line, as its arguments, or [] if it can't be
    read: what tells a run's process from another that took its id since.
    Linux's /proc first, since a slim container has no `ps`.
    """

    try:
        with open('/proc/{}/cmdline'.format(pid), 'rb') as file:
            return [part.decode('utf-8', 'replace') for part in file.read().split(b'\0') if part]
    except OSError:
        pass
    try:
        return subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, timeout=5).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []


def isBautaRun(arguments: List[str]) -> bool:
    """Whether a command line is `bauta run`: the `bauta` program, or
    `python -m bauta`, and then `run`.
    """

    for position, argument in enumerate(arguments):
        program = os.path.basename(argument) == 'bauta' or (argument == 'bauta' and position > 0 and arguments[position - 1] == '-m')
        if program and 'run' in arguments[position + 1:]:
            return True

    return False


def cancelRun(live: Dict[str, Any]) -> None:
    """Asks a live run to stop, as Ctrl-C or a container's stop would: SIGTERM
    to its process. bauta then starts nothing more -- jobs not yet started
    are skipped -- and lets the running ones finish, so no target is left
    half loaded. Only a run on this machine, still alive, and still bauta's.
    """

    if live.get('host') != socket.gethostname():
        raise CancelRefused('the run is on {}; cancel it there'.format(live.get('host')))
    pid = int(live['pid'])
    if not _alive(pid):
        raise CancelRefused('the run has already ended')
    if not isBautaRun(commandOf(pid)):
        raise CancelRefused('process {} is no longer the run'.format(pid))

    os.kill(pid, signal.SIGTERM)


def _alive(pid: int) -> bool:

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    return True

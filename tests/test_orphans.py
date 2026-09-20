"""A run whose process is killed outright takes its jobs with it.

SIGKILL runs none of the parent's cleanup, so its job processes used to be
orphaned and keep loading rows and writing run state -- while the run lock,
held by the dead parent, was already free, so the next `bauta run` started
alongside the orphan and the two loaded over each other.

POSIX only: the test kills a process and reads the process table.
"""
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='kills a process and reads the process table')

# Long enough that the job is still streaming when the parent is killed, and
# short enough that a test that fails to kill it doesn't hang for long.
ROWS = 3000000

# How long the job process may take to notice. It exits within milliseconds of
# the parent's death; before it watched for that, it ran on until the next log
# record it tried to send, which took seconds and thousands more rows.
ORPHAN_SECONDS = 3.0

DATABASES = """source:
  type: sqlite
  database: source.db
copy:
  type: sqlite
  database: copy.db
"""

JOBS = """workers: 1
jobs:
  slow:
    active: true
    sourceDatabase: source
    sourceQuery: |-
      WITH RECURSIVE counter(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM counter WHERE x < {rows})
      SELECT x, 'row ' || x FROM counter
    targetDatabase: copy
    targetTableFinal: rows_loaded
    insertStrategy: upsert
    chunkSize: 200
""".format(rows=ROWS)


def _jobProcesses(parent):
    """The pids of `parent`'s job processes, from the process table.

    Named by what a spawned process runs, so multiprocessing's own resource
    tracker -- also a child, and not one that writes anything -- is left out.
    """

    listing = subprocess.run(['ps', '-ax', '-o', 'pid=,ppid=,command='], capture_output=True, text=True, timeout=30).stdout
    processes = (line.split(maxsplit=2) for line in listing.splitlines() if len(line.split()) > 2)

    return [int(pid) for pid, parentPid, command in processes if int(parentPid) == parent and 'spawn_main' in command]


def _rowsLoaded(workspace):

    connection = sqlite3.connect(str(workspace / 'copy.db'))
    try:
        return connection.execute('SELECT count(*) FROM rows_loaded').fetchone()[0]
    finally:
        connection.close()


def _running(pid):

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    return True


def _waitFor(condition, seconds, message):

    deadline = time.time() + seconds
    while time.time() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(0.1)

    pytest.fail(message)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / 'database.yaml').write_text(DATABASES)
    (tmp_path / 'jobs.yaml').write_text(JOBS)
    for name in ('source.db', 'copy.db'):
        connection = sqlite3.connect(str(tmp_path / name))
        connection.execute('CREATE TABLE rows_loaded (id INT PRIMARY KEY, label TEXT)')
        connection.commit()
        connection.close()

    return tmp_path


def test_killing_a_run_outright_stops_the_job_it_started(workspace):
    environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    run = subprocess.Popen([sys.executable, '-c', 'from bauta.cli import main; main()',
                            'run', '--quiet', '--force', '--databases', 'database.yaml', '--jobs', 'jobs.yaml'],
                           cwd=str(workspace), env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        (worker,) = _waitFor(lambda: _jobProcesses(run.pid), 60, 'the run never started a job process')
        # Killed mid-load, which is the case that mattered: an orphan kept
        # writing rows for as long as its query lasted.
        loaded = _waitFor(lambda: _rowsLoaded(workspace), 60, 'the job loaded no rows before it was killed')

        os.kill(run.pid, signal.SIGKILL)
        run.wait(timeout=30)

        # Promptly, not eventually: an orphan does die at the next log record
        # it fails to send, but it can load a great many rows before then, and
        # the run lock it held is already free for the next run to take.
        _waitFor(lambda: not _running(worker), ORPHAN_SECONDS,
                 'the job process was still running {}s after the run that started it was killed'.format(ORPHAN_SECONDS))
        stopped = _rowsLoaded(workspace)
        time.sleep(1.0)

        assert loaded > 0
        assert _rowsLoaded(workspace) == stopped, 'rows were still being written after the run was killed'
    finally:
        for pid in [run.pid] + _jobProcesses(run.pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        run.wait(timeout=30)

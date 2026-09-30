"""What an orchestrator needs from bauta: the jobs and what each waits for, to
build its tasks from, and the command each task runs. Copy this file beside
your Airflow DAG or Dagster code; both examples here import it.

Every task runs the `bauta` command in a process of its own rather than
calling bauta from Python. runDataJobs starts each job in a new process,
which re-imports the calling program's __main__ -- in a worker, the
orchestrator's own -- and the command's exit code is already what a task
needs: 0 completed, 1 a job failed or another run holds it, 2 the
configuration is wrong, 130 interrupted.

Reading the graph expands nothing and validates nothing, so a scheduler
parsing the DAG needs neither bauta's secrets nor its databases. `bauta
validate`, as a task, is where the configuration is checked.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import yaml

from bauta import readJobsFile

# Where the configuration is: $BAUTA_CONFIG, as the command reads it, else
# the demo's own beside this file.
CONFIG_DIRECTORY = Path(os.environ.get('BAUTA_CONFIG') or Path(__file__).resolve().parent / 'configuration')


def _plain(path: Path) -> Any:
    """A YAML file as written: ${MASKING_KEY} stays text, so reading the
    graph needs no secret.
    """

    with open(path) as file:
        return yaml.safe_load(file)


def jobGraph(configDirectory: Path = CONFIG_DIRECTORY) -> Dict[str, List[str]]:
    """Each active job, with the active jobs it waits for, as a run decides
    them: a job left inactive is not run, and nothing waits for it. Files
    `include` names are read too.
    """

    document = readJobsFile(Path(configDirectory) / 'jobs.yaml', load=_plain).content
    defaults: Mapping[str, Any] = document.get('defaults') or {}
    jobs: Mapping[str, Mapping[str, Any]] = document.get('jobs') or {}

    active = {name for name, job in jobs.items() if job.get('active', defaults.get('active', True))}

    return {name: [predecessor for predecessor in jobs[name].get('predecessors') or [] if predecessor in active] for name in sorted(active)}


def bautaCommand() -> List[str]:
    """How to start bauta: $BAUTA_COMMAND, split as a shell would -- a
    virtualenv's bauta, or `docker run ... bauta` -- else this interpreter's
    own, which needs nothing on PATH.
    """

    configured = os.environ.get('BAUTA_COMMAND')

    return shlex.split(configured) if configured else [sys.executable, '-m', 'bauta']


def command(*arguments: str, configDirectory: Path = CONFIG_DIRECTORY, job: Optional[str] = None, fullRefresh: bool = False) -> List[str]:
    """The argv of one bauta command against `configDirectory`: `command('run',
    job='maskOrders')`. A job's own run holds only its own lock, so tasks for
    different jobs run side by side; --quiet leaves only errors on stderr,
    one line each, which is what a task's log should show.
    """

    argv = bautaCommand() + list(arguments) + ['--config', str(configDirectory), '--quiet']
    if job is not None:
        argv += ['--job', job]
    if fullRefresh:
        argv.append('--full-refresh')

    return argv


def shellCommand(argv: Sequence[str]) -> str:
    """`argv` as one shell line, quoted, for a task that takes a string."""

    return shlex.join(argv)


def runCommand(argv: Sequence[str], environment: Optional[Mapping[str, str]] = None) -> subprocess.CompletedProcess:
    """Runs `argv`, returning what it printed and its exit code."""

    return subprocess.run(list(argv), capture_output=True, text=True, env={**os.environ, **(environment or {})})

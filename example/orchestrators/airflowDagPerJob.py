"""bauta under Airflow, a task per bauta job: each job's own log, duration
and retry in the Airflow UI, and its predecessors as the task's upstream.
See airflowDag.py for one task a run, which is simpler.

What bauta decided for itself, Airflow decides here:

- How many jobs run at once: the `bauta_jobs` pool's slots stand in for
  `workers`. bauta's per-connection limits (`maxConcurrentJobs`) apply only
  within one run, and each task is a run of one job, so give a connection
  that needs a limit a pool of its own.
- What runs when: every task runs each time the DAG does, as `--job` ignores
  `refresh`, so schedule the DAG as often as the most frequent job needs.
- A failed job's dependents: Airflow marks them upstream_failed, as bauta
  skips them.

Tasks for different jobs run side by side, since `run --job` locks only its
own job. The weekly refresh and the regular DAG can reach the same job at
once, though, and the second then exits 1, so the tasks retry after a pause.

    airflow pools set bauta_jobs 4 "bauta jobs at once"
"""
from __future__ import annotations

import datetime
import re
from typing import Dict

import pendulum

try:  # Airflow 3
    from airflow.providers.standard.operators.bash import BashOperator
    from airflow.sdk import DAG
except ImportError:  # Airflow 2.4 and later
    from airflow import DAG
    from airflow.operators.bash import BashOperator

from bautaTasks import command, jobGraph, shellCommand

POOL = 'bauta_jobs'

START = pendulum.datetime(2026, 1, 1, tz='UTC')

ENVIRONMENT = {'MASKING_KEY': '{{ var.value.bauta_masking_key }}'}


def taskId(job: str) -> str:
    """A job's name as a task id, which takes letters, digits, -, . and _."""

    return re.sub(r'[^A-Za-z0-9_.-]', '_', job)


def jobTasks(fullRefresh: bool = False) -> Dict[str, BashOperator]:
    """A task per active bauta job, each downstream of its predecessors'."""

    graph = jobGraph()
    tasks = {
        job: BashOperator(task_id=taskId(job), bash_command=shellCommand(command('run', job=job, fullRefresh=fullRefresh)),
                          env=ENVIRONMENT, append_env=True, pool=POOL, retries=2, retry_delay=datetime.timedelta(minutes=5))
        for job in graph
        }
    for job, predecessors in graph.items():
        for predecessor in predecessors:
            tasks[predecessor] >> tasks[job]

    return tasks


with DAG(dag_id='bauta_jobs', schedule='*/15 * * * *', start_date=START, catchup=False, max_active_runs=1, tags=['bauta']):
    jobTasks()


with DAG(dag_id='bauta_jobs_full_refresh', schedule='0 3 * * 0', start_date=START, catchup=False, max_active_runs=1, tags=['bauta']):
    jobTasks(fullRefresh=True)

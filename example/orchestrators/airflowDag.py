"""bauta under Airflow, as one task a run: the simplest way, and the one that
keeps bauta's own `workers` and per-connection limits deciding what runs at
once. See airflowDagPerJob.py for a task per bauta job instead.

Two DAGs:

- `bauta` runs every 15 minutes: validate, then run. Each job's `refresh`
  still decides whether it's due.
- `bauta_full_refresh` runs weekly: audit and discover --update first, so a
  policy that no longer matches production fails a check rather than the
  refresh, then run --full-refresh, which takes rows deleted in production
  out of the incremental copies.

Copy this file and bautaTasks.py into your DAGs folder, and set $BAUTA_CONFIG
on the workers (or edit CONFIG_DIRECTORY there). Both DAGs' runs share one
slot of the `bauta` pool, so a refresh and a regular run never overlap,
which the run lock would otherwise turn into a failed task:

    airflow pools set bauta 1 "bauta runs"

The masking key comes from an Airflow Variable here, rendered into the
task's environment; a secrets backend behind Variables keeps it out of
Airflow's database. bauta's retries are its own (a job's `retries`), so the
tasks don't retry.
"""
from __future__ import annotations

from typing import List

import pendulum

try:  # Airflow 3
    from airflow.providers.standard.operators.bash import BashOperator
    from airflow.sdk import DAG
except ImportError:  # Airflow 2.4 and later
    from airflow import DAG
    from airflow.operators.bash import BashOperator

from bautaTasks import command, shellCommand

POOL = 'bauta'

START = pendulum.datetime(2026, 1, 1, tz='UTC')

# Rendered by Airflow when the task runs, and added to the worker's own
# environment (append_env), which is where BAUTA_CONFIG comes from.
ENVIRONMENT = {'MASKING_KEY': '{{ var.value.bauta_masking_key }}'}


def bautaTask(taskId: str, argv: List[str], pool: bool = False) -> BashOperator:
    """One bauta command as a task. Its exit code is the task's outcome; --quiet
    leaves only errors in the task's log, one line each.
    """

    return BashOperator(task_id=taskId, bash_command=shellCommand(argv), env=ENVIRONMENT, append_env=True, retries=0,
                        **({'pool': POOL} if pool else {}))


with DAG(dag_id='bauta', schedule='*/15 * * * *', start_date=START, catchup=False, max_active_runs=1, tags=['bauta']):
    bautaTask('validate', command('validate')) >> bautaTask('run', command('run'), pool=True)


with DAG(dag_id='bauta_full_refresh', schedule='0 3 * * 0', start_date=START, catchup=False, max_active_runs=1, tags=['bauta']):
    audit = bautaTask('audit', command('audit', '--strict'))
    drift = bautaTask('discover_update', command('discover', '--update'))
    refresh = bautaTask('full_refresh', command('run', fullRefresh=True), pool=True)

    audit >> drift >> refresh

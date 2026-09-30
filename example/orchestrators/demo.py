"""What the Airflow and Dagster examples beside it do, without either:

    python example/orchestrators/demo.py

Needs no server, no credentials and no orchestrator. It builds throwaway
SQLite databases in transaction/, reads the job graph the way the examples
build their tasks from it (bautaTasks.jobGraph), and runs a task per job the
way an orchestrator would: each a `bauta run --job` process of its own,
started as soon as its predecessors have finished, so jobs that wait for
nothing run side by side. It prints each command.

Then it deletes an order in production. An incremental run can't see that,
and leaves it in the copy; the weekly schedule's checks and full refresh
take it out.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Dict, List, Tuple

demoDirectory = Path(__file__).resolve().parent
repositoryDirectory = demoDirectory.parents[1]
sys.path[:0] = [str(demoDirectory), str(repositoryDirectory)]

from bautaTasks import command, jobGraph, runCommand, shellCommand  # noqa: E402

DEFAULT_WORKING_DIRECTORY = demoDirectory / 'transaction'

# A throwaway key for throwaway databases. A real one is random, comes from a
# secret store, and is never written into a script.
DEMO_MASKING_KEY = 'orchestrator-demo-masking-key-not-for-real-use'

SCHEMA = '''
CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT, full_name TEXT);
CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, price NUMERIC);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INT REFERENCES customers(id), product_id INT REFERENCES products(id),
                     shipping_phone TEXT, updated_at INT);
'''

# The table a full refresh loads before swapping it with orders: the same
# columns and key, and no foreign keys, as `bauta schema --stage-suffix` makes it.
STAGE = 'CREATE TABLE orders_stage (id INTEGER PRIMARY KEY, customer_id INT, product_id INT, shipping_phone TEXT, updated_at INT);'


def build(production: Path, staging: Path) -> None:
    """Production with a few customers, products and orders; staging with the
    same tables empty, and the stage table a full refresh swaps in.
    """

    with sqlite3.connect(production) as connection:
        connection.executescript(SCHEMA)
        connection.executemany('INSERT INTO customers VALUES (?, ?, ?)',
                               [(index, 'person{}@mail.example'.format(index), 'Person {}'.format(index)) for index in range(1, 6)])
        connection.executemany('INSERT INTO products VALUES (?, ?, ?)', [(1, 'kettle', 30), (2, 'teapot', 25)])
        connection.executemany('INSERT INTO orders VALUES (?, ?, ?, ?, ?)',
                               [(index, 1 + index % 5, 1 + index % 2, '+351 912 000 {:03d}'.format(index), index) for index in range(1, 11)])
    connection.close()

    with sqlite3.connect(staging) as connection:
        connection.executescript(SCHEMA + STAGE)
    connection.close()


def runTasks(graph: Dict[str, List[str]], environment: Dict[str, str], fullRefresh: bool = False) -> Dict[str, Tuple[float, float, int]]:
    """A task per job, each started once its predecessors have succeeded, as
    Airflow and Dagster start them: (started, finished, exit code) for each.
    """

    outcomes: Dict[str, Tuple[float, float, int]] = {}
    running: Dict[Future, str] = {}

    def task(job: str) -> Tuple[float, float, int]:
        started = time.monotonic()
        result = runCommand(command('run', job=job, fullRefresh=fullRefresh), environment)
        if result.returncode != 0:
            print('  {} failed: {}'.format(job, result.stderr.strip()))
        return started, time.monotonic(), result.returncode

    with ThreadPoolExecutor(max_workers=len(graph)) as pool:
        while len(outcomes) < len(graph):
            for job, predecessors in graph.items():
                started = job in outcomes or job in running.values()
                if not started and all(outcomes.get(predecessor, (0, 0, 1))[2] == 0 for predecessor in predecessors):
                    print(shown(command('run', job=job, fullRefresh=fullRefresh)))
                    running[pool.submit(task, job)] = job
            if not running:
                break
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                outcomes[running.pop(future)] = future.result()

    return outcomes


def shown(argv: List[str]) -> str:
    """A command as a person would type it from the repository root."""

    return '  $ ' + shellCommand(argv).replace(sys.executable, 'python').replace(str(repositoryDirectory) + os.sep, '')


def rows(database: Path, query: str) -> List[Any]:

    with sqlite3.connect(database) as connection:
        found = connection.execute(query).fetchall()
    connection.close()
    return found


def main(workingDirectory: Path = DEFAULT_WORKING_DIRECTORY) -> Dict[str, Any]:
    """Runs the demonstration, returning what the test checks.

    workingDirectory is a parameter so the test can point it at a temporary
    directory instead of writing into the source tree.
    """

    shutil.rmtree(workingDirectory, ignore_errors=True)
    workingDirectory.mkdir(parents=True)
    production, staging = workingDirectory / 'prod.db', workingDirectory / 'staging.db'
    build(production, staging)

    # What connections.yaml and jobs.yaml read, and the bauta of this checkout
    # for the processes the tasks start.
    environment = {
        'ORCHESTRATOR_DEMO_PROD_PATH': str(production),
        'ORCHESTRATOR_DEMO_STAGING_PATH': str(staging),
        'ORCHESTRATOR_DEMO_STATE': str(workingDirectory),
        'MASKING_KEY': os.environ.get('MASKING_KEY', DEMO_MASKING_KEY),
        'PYTHONPATH': os.pathsep.join(filter(None, [str(repositoryDirectory), os.environ.get('PYTHONPATH')])),
        }
    observed: Dict[str, Any] = {}

    graph = jobGraph()
    observed['graph'] = graph
    print('The job graph, as the DAGs and assets are built from it:')
    for job, predecessors in graph.items():
        print('  {:<14} waits for {}'.format(job, ', '.join(predecessors) or 'nothing'))

    print('\nEvery 15 minutes, a task per job, each once its predecessors have finished:')
    first = runTasks(graph, environment)
    observed['first'] = first
    (customersStart, customersEnd, _), (productsStart, productsEnd, _) = first['maskCustomers'], first['loadProducts']
    observed['sideBySide'] = customersStart < productsEnd and productsStart < customersEnd
    print('  maskCustomers and loadProducts ran {}.'.format('side by side' if observed['sideBySide'] else 'one after the other'))
    observed['copied'] = rows(staging, 'SELECT id FROM orders ORDER BY id')
    print('  staging holds {} orders; the first, masked: {}'.format(
        len(observed['copied']), rows(staging, 'SELECT id, shipping_phone FROM orders ORDER BY id LIMIT 1')[0]))

    with sqlite3.connect(production) as connection:
        connection.execute('DELETE FROM orders WHERE id = 3')
    connection.close()
    print('\nOrder 3 is deleted in production. The next incremental tasks:')
    observed['incremental'] = runTasks(graph, environment)
    observed['afterIncremental'] = rows(staging, 'SELECT id FROM orders ORDER BY id')
    print('  staging still holds order 3: {}'.format((3,) in observed['afterIncremental']))

    print('\nWeekly: the checks, then a full refresh, task by task:')
    checks = {}
    for name, argv in (('audit', command('audit', '--strict')), ('discover --update', command('discover', '--update'))):
        print(shown(argv))
        checks[name] = runCommand(argv, environment).returncode
    observed['checks'] = checks
    print('  checks exited {}'.format(', '.join('{} {}'.format(name, code) for name, code in checks.items())))
    observed['refresh'] = runTasks(graph, environment, fullRefresh=True)
    observed['afterRefresh'] = rows(staging, 'SELECT id FROM orders ORDER BY id')
    print('  staging holds order 3: {}'.format((3,) in observed['afterRefresh']))

    return observed


if __name__ == '__main__':
    main()

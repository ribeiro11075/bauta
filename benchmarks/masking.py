"""The benchmark behind docs/masking.md's speed figures.

    python benchmarks/masking.py [--rows 1000000] [--no-python]

Needs no server. It builds a SQLite table of `rows` rows -- an id and six
columns: a ten-character reference (`C` and nine digits), a ten-digit
integer, an email address, a twelve-character hex token, a session string
and a phone number -- and copies it into a second SQLite database once per
run, masked, through the same pipeline `bauta run` uses. It prints:

    The policies, native masker on one thread, reading, masking and
    writing overlapped (docs/masking.md, "Speed"):

        email, two hash, three keep
        two key columns, email, two hash, digits
        two fpe columns, email, two hash, digits
        five key columns, one hash

    The second policy under each masker (docs/masking.md, "The native
    masker"): Python, then Rust in turn, overlapped, and on every core.

Each run is a process of its own, since which masker is used, whether the
pipeline overlaps, and how many threads mask are each decided once per
process. Every run of a policy must produce the same copy, whichever masker
and however many threads: the script says so, or fails. Python runs take
about a minute each at the default million rows; --no-python leaves them out.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bauta.configuration import DataJobConfig, connectionConfig
from bauta.jobs.pipeline import _executeDataJob
from bauta.masking import core as maskingCore

# A throwaway key for a throwaway database.
BENCHMARK_KEY = 'benchmark-key-not-for-real-use'

COLUMNS = 'id INTEGER PRIMARY KEY, ref TEXT, account INTEGER, email TEXT, token TEXT, secret TEXT, phone TEXT'

POLICIES: Dict[str, Tuple[str, Dict[str, str]]] = {
    'cheap': ('email, two hash, three keep',
              {'ref': 'keep', 'account': 'keep', 'email': 'email', 'token': 'hash', 'secret': 'hash', 'phone': 'keep'}),
    'key': ('two key columns, email, two hash, digits',
            {'ref': 'key', 'account': 'key', 'email': 'email', 'token': 'hash', 'secret': 'hash', 'phone': 'digits'}),
    'fpe': ('two fpe columns, email, two hash, digits',
            {'ref': 'fpe', 'account': 'fpe', 'email': 'email', 'token': 'hash', 'secret': 'hash', 'phone': 'digits'}),
    'keys': ('five key columns, one hash',
             {'ref': 'key', 'account': 'key', 'email': 'key', 'token': 'key', 'secret': 'hash', 'phone': 'key'}),
    }

# (label, BAUTA_NATIVE, BAUTA_PIPELINE, BAUTA_MASKING_THREADS)
MASKERS = [
    ('Python', '0', '0', '1'),
    ('Rust, one thread, in turn', '1', '0', '1'),
    ('Rust, one thread, overlapped (the default)', '1', '1', '1'),
    ('Rust, all cores', '1', '1', 'auto'),
    ]


def buildSource(path: Path, rows: int) -> None:

    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE people ({})'.format(COLUMNS))
    connection.executemany('INSERT INTO people VALUES (?, ?, ?, ?, ?, ?, ?)', (
        (number, 'C{:09d}'.format(number * 7919 % 10 ** 9), 1_000_000_000 + number * 7919 % 9_000_000_000, 'person{}@corp.example'.format(number),
         '{:012x}'.format(number * 2654435761 % 16 ** 12), 'sess-{:x}'.format(number * 40503), '+1 555 {:03d} {:04d}'.format(number % 1000, number % 10000))
        for number in range(1, rows + 1)))
    connection.commit()
    connection.close()


def runOnce(source: Path, target: Path, policy: str) -> Dict[str, Any]:
    """One masked copy, in this process, as the environment configures it."""

    target.unlink(missing_ok=True)
    connection = sqlite3.connect(target)
    for table in ('people', 'people_stage'):
        connection.execute('CREATE TABLE {} ({})'.format(table, COLUMNS))
    connection.commit()
    connection.close()

    job = DataJobConfig(sourceConnection='source', sourceQuery='SELECT id, ref, account, email, token, secret, phone FROM people',
                        targetConnection='target', targetTableFinal='people', targetTableStage='people_stage', insertStrategy='swap',
                        masking={'key': BENCHMARK_KEY, 'columns': dict(POLICIES[policy][1], id='keep')})
    databases = {'source': connectionConfig(type='sqlite', path=str(source)),
                 'target': connectionConfig(type='sqlite', path=str(target))}
    maskingCore.setMaskingThreads(maskingCore.maskingThreadsFor(maskingCore.effectiveMaskingThreads(1), 1))

    started = time.perf_counter()
    outcome = _executeDataJob('benchmark', job, databases)
    seconds = time.perf_counter() - started

    digest = hashlib.sha256()
    connection = sqlite3.connect(target)
    for row in connection.execute('SELECT * FROM people ORDER BY id'):
        digest.update(repr(row).encode())
    connection.close()

    return {'rows': outcome.rowCount, 'seconds': seconds, 'digest': digest.hexdigest(), 'masker': maskingCore.maskingImplementation()}


def measure(workingDirectory: Path, source: Path, policy: str, native: str, pipeline: str, threads: str) -> Dict[str, Any]:
    """runOnce in a fresh process, so the environment it reads is this run's own."""

    environment = dict(os.environ, BAUTA_NATIVE=native, BAUTA_PIPELINE=pipeline, BAUTA_MASKING_THREADS=threads)
    completed = subprocess.run([sys.executable, __file__, '--one', str(source), str(workingDirectory / 'target.db'), policy],
                               env=environment, capture_output=True, text=True, check=True)

    return json.loads(completed.stdout.strip().splitlines()[-1])


def main(rows: int, python: bool, workingDirectory: Path) -> Dict[str, List[Tuple[str, Dict[str, Any]]]]:

    source = workingDirectory / 'source-{}.db'.format(rows)
    if not source.exists():
        buildSource(source, rows)

    if maskingCore.nativeVersion() is None:
        raise SystemExit('bauta-rs is not installed, and every figure but one is of it: pip install ./mask-rs/py')

    results: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {'policies': [], 'maskers': []}

    print('{:,} rows of six columns plus an id, SQLite to SQLite, {} core(s).\n'.format(rows, maskingCore.availableCores()))
    print('Native masker, one thread, overlapped:\n')
    print('{:<45} {:>14}'.format('Policy', 'Rows a second'))
    for policy, (label, _) in POLICIES.items():
        result = measure(workingDirectory, source, policy, '1', '1', '1')
        results['policies'].append((policy, result))
        print('{:<45} {:>14,.0f}'.format(label, result['rows'] / result['seconds']))

    print('\nThe second policy, "{}", under each masker:\n'.format(POLICIES['key'][0]))
    print('{:<45} {:>14}'.format('Masker', 'Rows a second'))
    for label, native, pipeline, threads in MASKERS:
        if native == '0' and not python:
            continue
        result = measure(workingDirectory, source, 'key', native, pipeline, threads)
        results['maskers'].append((label, result))
        print('{:<45} {:>14,.0f}'.format(label, result['rows'] / result['seconds']))

    copies = {result['digest'] for _, result in results['maskers']} | {result['digest'] for policy, result in results['policies'] if policy == 'key'}
    if len(copies) != 1:
        raise SystemExit('\nThe copies of the second policy differ between maskers -- please report this.')
    print('\nEvery copy of the second policy is identical, whichever masker made it ({}).'.format(results['policies'][0][1]['masker']))

    return results


if __name__ == '__main__':

    if len(sys.argv) == 5 and sys.argv[1] == '--one':
        print(json.dumps(runOnce(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])))
        raise SystemExit(0)

    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--rows', type=int, default=1_000_000)
    parser.add_argument('--no-python', dest='python', action='store_false', help='leave out the pure-Python run, the slowest')
    parser.add_argument('--directory', type=Path, help='where the databases go (default: a temporary directory)')
    arguments = parser.parse_args()

    if arguments.directory is not None:
        arguments.directory.mkdir(parents=True, exist_ok=True)
        main(arguments.rows, arguments.python, arguments.directory)
    else:
        with tempfile.TemporaryDirectory(prefix='bauta-benchmark-') as directory:
            main(arguments.rows, arguments.python, Path(directory))

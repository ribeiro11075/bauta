"""A demo for Studio: a small SQLite shop with personal data where real shops
keep it, jobs that copy and mask it into a second database, and a few runs
of history -- one with a job that fails -- so every screen has something to
show.

    python studio/demo/setup.py demo-workspace

then the two commands it prints: one to add your account, one to serve
Studio. The `payments` job fails on purpose, for the screens to show a
failure. Run it again to start over.
"""
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

MASKING_KEY = 'studio-demo-masking-key-0123456789abcdef'
MANIFEST_KEY = 'studio-demo-manifest-signing-key-0123456789'

CONNECTIONS = """production:
  type: sqlite
  path: ../production.db
staging:
  type: sqlite
  path: ../staging.db
  requireMasking: true
"""

JOBS = """# The demo shop's jobs: production into a masked staging copy.
workers: 2
history: ../history.jsonl
manifest: ../manifest.json

defaults:
  sourceConnection: production
  targetConnection: staging
  insertStrategy: upsert
  chunkSize: 500

jobs:
  customers:
    sourceQuery: SELECT id, name, email, phone, city, country FROM customers
    targetTableFinal: customers
    masking:
      key: ${MASKING_KEY}
      columns:
        id: keep
        name: fakeName
        email: email
        phone: hash
        city: keep
        country: keep

  orders:
    predecessors: [customers]
    sourceQuery: SELECT id, customer_id, total, placed_at FROM orders WHERE placed_at > {{ watermark }}
    targetTableFinal: orders
    # Incremental, with a stage table: a full refresh loads every row into
    # it, then swaps it with orders.
    targetTableStage: orders_stage
    watermarkColumn: placed_at
    watermarkInitial: '2000-01-01'
    masking:
      key: ${MASKING_KEY}
      columns:
        id: keep
        customer_id: keep
        total: keep
        placed_at: keep

  support_tickets:
    predecessors: [customers]
    sourceQuery: SELECT * FROM support_tickets
    targetTableFinal: support_tickets
    masking:
      key: ${MASKING_KEY}
      columns:
        id: keep
        customer_id: keep
        subject: keep
        body: redact

  countries:
    sourceQuery: SELECT code, name FROM countries
    targetTableFinal: countries
    masking:
      key: ${MASKING_KEY}
      columns:
        code: keep
        name: keep

  payments:
    predecessors: [orders]
    sourceQuery: SELECT id, order_id, card_number, amount FROM payments
    targetTableFinal: payments
    masking:
      key: ${MASKING_KEY}
      columns:
        id: keep
        order_id: keep
        card_number: hash
        amount: keep

acknowledged:
  production:
    audit_log: Internal access records; nothing downstream reads them.
"""

FIRST = ['Ann', 'Ben', 'Chloe', 'Dev', 'Elena', 'Femi', 'Grace', 'Hiro', 'Iris', 'Jonas']
LAST = ['Smith', 'Okafor', 'Novak', 'Garcia', 'Chen', 'Haddad', 'Kowalski', 'Silva']
CITIES = ['Lisbon', 'Porto', 'Boston', 'Austin', 'Lyon', 'Kraków']


def _production(path):
    random.seed(7)
    connection = sqlite3.connect(str(path))
    connection.executescript("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, email TEXT, phone TEXT, city TEXT, country TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), total INTEGER, placed_at TEXT);
        CREATE TABLE support_tickets (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), subject TEXT, body TEXT);
        CREATE TABLE countries (code TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE payments (id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders(id), card_number TEXT, amount INTEGER);
        CREATE TABLE audit_log (id INTEGER PRIMARY KEY, actor_email TEXT, action TEXT);
        CREATE TABLE user_sessions (id INTEGER PRIMARY KEY, client_ip TEXT, user_agent TEXT);
    """)
    customers = []
    for index in range(1, 2001):
        first, last = random.choice(FIRST), random.choice(LAST)
        customers.append((index, '{} {}'.format(first, last), '{}.{}{}@example.com'.format(first, last, index).lower(),
                          '+1 555 {:04d}'.format(random.randrange(10000)), random.choice(CITIES), random.choice(['PT', 'US', 'FR', 'PL'])))
    connection.executemany('INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?)', customers)
    connection.executemany('INSERT INTO orders VALUES (?, ?, ?, ?)', [(index, random.randrange(1, 2001), random.randrange(500, 50000),
                                                                      '2026-10-{:02d}T{:02d}:00:00'.format(1 + index % 15, index % 24)) for index in range(1, 6001)])
    connection.executemany('INSERT INTO support_tickets VALUES (?, ?, ?, ?)', [(index, random.randrange(1, 2001), 'Order question',
                                                                               'Call me on +1 555 01{:02d}, or write to me@example.com'.format(index % 100))
                                                                              for index in range(1, 801)])
    connection.executemany('INSERT INTO countries VALUES (?, ?)', [('PT', 'Portugal'), ('US', 'United States'), ('FR', 'France'), ('PL', 'Poland')])
    connection.executemany('INSERT INTO payments VALUES (?, ?, ?, ?)', [(index, index, '4111 1111 1111 {:04d}'.format(index % 10000), 100)
                                                                        for index in range(1, 3001)])
    connection.executemany('INSERT INTO audit_log VALUES (?, ?, ?)', [(index, 'admin@example.com', 'login') for index in range(1, 51)])
    connection.commit()
    connection.close()


def _staging(path, withPayments):
    connection = sqlite3.connect(str(path))
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY, name TEXT, email TEXT, phone TEXT, city TEXT, country TEXT);
        CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, customer_id INTEGER, total INTEGER, placed_at TEXT);
        CREATE TABLE IF NOT EXISTS orders_stage (id INTEGER PRIMARY KEY, customer_id INTEGER, total INTEGER, placed_at TEXT);
        CREATE TABLE IF NOT EXISTS support_tickets (id INTEGER PRIMARY KEY, customer_id INTEGER, subject TEXT, body TEXT);
        CREATE TABLE IF NOT EXISTS countries (code TEXT PRIMARY KEY, name TEXT);
    """)
    if withPayments:
        connection.execute('CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY, order_id INTEGER, card_number TEXT, amount INTEGER)')
    connection.commit()
    connection.close()


def _run(workspace, environment):
    """One `bauta run`, its output kept back: a job failing is what the demo
    arranges, so only anything else is shown, and stops it.
    """

    finished = subprocess.run([sys.executable, '-m', 'bauta', 'run', '--config', str(workspace / 'configuration'), '--quiet', '--force'],
                              env=environment, cwd=str(workspace), capture_output=True, text=True)
    if finished.returncode not in (0, 1):
        sys.exit('bauta run exited {}:\n{}{}'.format(finished.returncode, finished.stdout, finished.stderr))


def _runs(workspace):
    """Each run history recorded, oldest first: how many jobs completed of how
    many, and which failed.
    """

    runs = {}
    for line in (workspace / 'history.jsonl').read_text().splitlines():
        record = json.loads(line)
        runs.setdefault(record['runId'], []).append(record)

    return [(sum(record['status'] == 'completed' for record in records), len(records),
             sorted(record['job'] for record in records if record['status'] != 'completed')) for records in runs.values()]


def main():
    workspace = Path(sys.argv[1] if len(sys.argv) > 1 else 'demo-workspace').resolve()
    if workspace.exists():
        shutil.rmtree(workspace)
    (workspace / 'configuration').mkdir(parents=True)
    (workspace / 'configuration' / 'connections.yaml').write_text(CONNECTIONS)
    (workspace / 'configuration' / 'jobs.yaml').write_text(JOBS)
    _production(workspace / 'production.db')
    environment = dict(os.environ, MASKING_KEY=MASKING_KEY, BAUTA_MANIFEST_KEY=MANIFEST_KEY)

    # A first run whose payments target is missing, so one job fails and the
    # screens have a failure to show; then one that succeeds.
    _staging(workspace / 'staging.db', withPayments=False)
    _run(workspace, environment)
    _staging(workspace / 'staging.db', withPayments=True)
    _run(workspace, environment)
    # And the payments table gone again, so the latest run fails too.
    connection = sqlite3.connect(str(workspace / 'staging.db'))
    connection.execute('DROP TABLE payments')
    connection.commit()
    connection.close()
    _run(workspace, environment)

    # And a column production gained since the policies were written, for
    # discovery review to find.
    connection = sqlite3.connect(str(workspace / 'production.db'))
    connection.execute('ALTER TABLE support_tickets ADD COLUMN reporter_email TEXT')
    connection.execute("UPDATE support_tickets SET reporter_email = 'reporter' || id || '@example.com'")
    connection.commit()
    connection.close()

    print('Demo ready in {}\n'.format(workspace))
    for number, (completed, total, failed) in enumerate(_runs(workspace), start=1):
        print('  Run {}: {} of {} jobs completed{}'.format(number, completed, total,
                                                            '; {} failed, on purpose: its table is missing from staging'.format(', '.join(failed))
                                                            if failed else ''))
    print('  Since then production gained support_tickets.reporter_email, for discovery review to find.\n')
    configuration, data = workspace / 'configuration', workspace / 'studio-data'
    print('Add your account (it asks for a password, 12 characters or more):\n')
    print('  bauta studio create-user --config {} --data {} --email you@example.com\n'.format(configuration, data))
    print('Then serve Studio, with the environment the runs had, and open http://127.0.0.1:8710:\n')
    print('  MASKING_KEY={} BAUTA_MANIFEST_KEY={} BAUTA_STUDIO_INSECURE_COOKIES=1 \\\n    bauta studio serve --config {} --data {}'.format(
        MASKING_KEY, MANIFEST_KEY, configuration, data))


if __name__ == '__main__':
    main()

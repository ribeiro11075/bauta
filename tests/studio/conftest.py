"""A Studio over a small SQLite workspace, with a user of each role."""
import sqlite3
import sys

import pytest

pytest.importorskip('fastapi', reason='bauta Studio needs its extra: pip install "bauta[studio]"')

from fastapi.testclient import TestClient  # noqa: E402

from bauta.studio.app import createApp  # noqa: E402
from bauta.studio.auth import hashPassword  # noqa: E402
from bauta.studio.db import User  # noqa: E402
from bauta.studio.settings import Settings  # noqa: E402

KEY = 'a-studio-test-masking-key-never-shown-anywhere'
PASSWORD = 'correct horse battery'

JOBS_YAML = """# The demo's jobs: a comment that an edit must keep.
workers: 1
history: history.jsonl
manifest: manifest.json
jobs:
  copyPeople:
    sourceConnection: demo
    sourceQuery: SELECT id, email FROM people
    targetConnection: demo
    targetTableFinal: people_copy
    insertStrategy: upsert
    masking:
      key: {key}
      columns:
        id: keep
        email: email
  copyOrders:
    predecessors: [copyPeople]
    sourceConnection: demo
    sourceQuery: SELECT id, total FROM orders
    targetConnection: demo
    targetTableFinal: orders_copy
    insertStrategy: upsert
    unmasked: true
""".format(key=KEY)


@pytest.fixture
def settings(tmp_path):
    configuration = tmp_path / 'configuration'
    configuration.mkdir()
    connection = sqlite3.connect(str(tmp_path / 'demo.db'))
    for statement in ['CREATE TABLE people (id INT PRIMARY KEY, email TEXT)', 'CREATE TABLE people_copy (id INT PRIMARY KEY, email TEXT)',
                      'CREATE TABLE orders (id INT PRIMARY KEY, total INT)', 'CREATE TABLE orders_copy (id INT PRIMARY KEY, total INT)']:
        connection.execute(statement)
    connection.executemany('INSERT INTO people VALUES (?, ?)', [(index, 'p{}@example.com'.format(index)) for index in range(5)])
    connection.executemany('INSERT INTO orders VALUES (?, ?)', [(index, index * 10) for index in range(5)])
    connection.commit()
    connection.close()
    (configuration / 'connections.yaml').write_text('demo:\n  type: sqlite\n  path: ../demo.db\n')
    (configuration / 'jobs.yaml').write_text(JOBS_YAML)

    return Settings(configDirectory=configuration, dataDirectory=tmp_path / 'studio-data', databaseUrl='sqlite:///{}'.format(tmp_path / 'studio.db'),
                    secureCookies=False, bautaCommand=[sys.executable, '-m', 'bauta'])


@pytest.fixture
def app(settings):
    application = createApp(settings)
    with application.state.database.open() as session:
        for role in ('viewer', 'operator', 'admin'):
            session.add(User(email='{}@example.com'.format(role), role=role, passwordHash=hashPassword(PASSWORD)))
        session.commit()

    return application


@pytest.fixture
def signIn(app):
    """A client signed in as `role`, sending Studio's header on every request."""

    def client(role):
        signedIn = TestClient(app, headers={'X-Bauta-Studio': '1'})
        response = signedIn.post('/api/auth/login', json={'email': '{}@example.com'.format(role), 'password': PASSWORD})
        assert response.status_code == 200, response.text
        return signedIn

    return client

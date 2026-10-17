"""bauta.project: a configuration directory loaded as the command loads it,
and the command's checks returned as data, for a program built on it.
"""
import json
import sqlite3

import pytest

from bauta import ConfigurationError, Project
from bauta.cli import EXIT_SUCCESS, main
from bauta.configuration import TableLocation
from bauta.project import UsageError

KEY = 'a-project-test-masking-key-of-some-length'

JOBS_YAML = """workers: 1
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
    sourceConnection: demo
    sourceQuery: SELECT id, total FROM orders
    targetConnection: demo
    targetTableFinal: orders_copy
    insertStrategy: upsert
    unmasked: true
""".format(key=KEY)


@pytest.fixture
def directory(tmp_path):
    """A configuration directory over a seeded SQLite database, with every
    target table but orders_copy created.
    """
    configuration = tmp_path / 'configuration'
    configuration.mkdir()

    connection = sqlite3.connect(str(tmp_path / 'demo.db'))
    connection.execute('CREATE TABLE people (id INT PRIMARY KEY, email TEXT)')
    connection.execute('CREATE TABLE people_copy (id INT PRIMARY KEY, email TEXT)')
    connection.execute('CREATE TABLE orders (id INT PRIMARY KEY, total INT)')
    connection.execute('CREATE TABLE audit_log (id INT PRIMARY KEY, actor_email TEXT)')
    connection.executemany('INSERT INTO people VALUES (?, ?)', [(index, 'p{}@example.com'.format(index)) for index in range(3)])
    connection.commit()
    connection.close()

    (configuration / 'connections.yaml').write_text('demo:\n  type: sqlite\n  path: ../demo.db\n')
    (configuration / 'jobs.yaml').write_text(JOBS_YAML)

    return configuration


def test_a_project_loads_a_directory_and_keeps_its_files_beside_the_jobs_file(directory):
    project = Project.load(directory)

    assert sorted(project.jobsFile.jobs) == ['copyOrders', 'copyPeople']
    assert list(project.connections) == ['demo']
    assert project.location('history') == directory / 'history.jsonl'
    assert project.location('manifest') == directory / 'manifest.json'
    assert project.location('memory') == directory / 'memory.yaml'


def test_a_location_in_a_table_keeps_its_default_table_name(directory):
    (directory / 'jobs.yaml').write_text(JOBS_YAML.replace('history: history.jsonl', 'history:\n  connection: demo'))

    assert Project.load(directory).location('history') == TableLocation(connection='demo', table='bauta_history')


def test_a_missing_directory_or_an_invalid_file_is_refused(tmp_path, directory):
    with pytest.raises(UsageError, match='no such file'):
        Project.load(tmp_path / 'nowhere')

    (directory / 'jobs.yaml').write_text(JOBS_YAML.replace('insertStrategy: upsert', 'insertStrategy: sideways', 1))
    with pytest.raises(ConfigurationError):
        Project.load(directory)


def test_history_and_the_manifest_are_none_until_a_run_writes_them(directory):
    project = Project.load(directory)

    assert project.history().read() == []
    assert project.readManifest() is None


def test_history_and_the_manifest_read_back_what_run_wrote(directory, monkeypatch):
    monkeypatch.chdir(directory.parent)
    connection = sqlite3.connect(str(directory.parent / 'demo.db'))
    connection.execute('CREATE TABLE orders_copy (id INT PRIMARY KEY, total INT)')
    connection.commit()
    connection.close()
    assert main(['run', '--quiet']) == EXIT_SUCCESS

    project = Project.load(directory)
    records = project.history().read()
    where, manifest = project.readManifest()

    assert sorted(record['job'] for record in records) == ['copyOrders', 'copyPeople']
    assert len({record['runId'] for record in records}) == 1
    assert where == str(directory / 'manifest.json')
    assert [entry['job'] for entry in manifest['jobs']] == ['copyPeople']


def test_validation_finds_nothing_in_a_valid_configuration(directory):
    assert Project.load(directory).validationProblems() == []


def test_a_dry_run_names_the_target_that_is_not_there(directory):
    problems = Project.load(directory).dryRunProblems()

    assert len(problems) == 1 and problems[0].startswith('copyOrders: target orders_copy is not readable')


def test_a_dry_run_of_some_jobs_checks_only_those(directory):
    project = Project.load(directory)

    assert project.dryRunProblems({'copyPeople': project.jobsFile.jobs['copyPeople']}) == []


def test_the_audit_is_the_report_the_command_writes_and_never_holds_the_key(directory):
    report = Project.load(directory).audit()

    assert [job['job'] for job in report['jobs']] == ['copyOrders', 'copyPeople']
    assert {'error', 'warning', 'info'} <= set(report['summary'])
    assert KEY not in json.dumps(report, default=str)


def test_an_audit_of_a_job_that_does_not_exist_is_refused(directory):
    with pytest.raises(UsageError, match='no such job'):
        Project.load(directory).audit(jobs=['nobody'])


def test_coverage_finds_the_table_no_job_reads_and_its_personal_columns(directory):
    report = Project.load(directory).coverage()

    # The copies are in the same database, so they count as tables too.
    uncovered = {entry['table']: entry for entry in report['tables'] if entry['state'] == 'uncovered'}
    assert sorted(uncovered) == ['audit_log', 'people_copy']
    assert uncovered['audit_log']['personalDataColumns'] == ['actor_email']

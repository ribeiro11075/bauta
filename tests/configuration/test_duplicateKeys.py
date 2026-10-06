"""A key given twice in one YAML mapping is an error, not a silent override.

PyYAML keeps the last value. A policy listing `actor_email: email` and later
`actor_email: keep` validated, read as masked, and copied every address as
it stood; `audit` noticed only because the name said "email".
"""
import sqlite3

import pytest

from bauta.cli import EXIT_BAD_CONFIGURATION, main
from bauta.configuration import ConfigurationError, loadYamlFile, parseYaml


def test_a_key_given_twice_names_it_and_both_lines():
    with pytest.raises(Exception, match=r'"col7" is given twice in one mapping, first on line 3') as raised:
        parseYaml('columns:\n  id: keep\n  col7: email\n  col7: keep\n')

    assert 'line 4' in str(raised.value)


def test_an_override_of_a_merged_key_is_not_a_repeat():
    document = parseYaml('base: &base\n  chunkSize: 100\n  active: true\njob:\n  <<: *base\n  chunkSize: 500\n')

    assert document['job'] == {'chunkSize': 500, 'active': True}


def test_the_same_key_in_different_mappings_is_fine():
    assert parseYaml('a:\n  key: 1\nb:\n  key: 2\n') == {'a': {'key': 1}, 'b': {'key': 2}}


def test_a_file_with_a_repeated_key_is_invalid_configuration(tmp_path):
    path = tmp_path / 'connections.yaml'
    path.write_text('prod:\n  type: sqlite\n  path: prod.db\n  requireMasking: true\n  requireMasking: false\n')

    with pytest.raises(ConfigurationError, match='"requireMasking" is given twice'):
        loadYamlFile(path)


def test_validate_refuses_a_masking_policy_naming_a_column_twice(tmp_path, monkeypatch, caplog):
    configuration = tmp_path / 'configuration'
    configuration.mkdir()
    connection = sqlite3.connect(str(tmp_path / 'demo.db'))
    connection.execute('CREATE TABLE src (id INT PRIMARY KEY, col7 TEXT)')
    connection.execute('CREATE TABLE tgt (id INT PRIMARY KEY, col7 TEXT)')
    connection.execute("INSERT INTO src VALUES (1, 'real@example.com')")
    connection.commit()
    connection.close()
    (configuration / 'connections.yaml').write_text('demo:\n  type: sqlite\n  path: ../demo.db\n')
    (configuration / 'jobs.yaml').write_text('''workers: 1
jobs:
  copy:
    sourceConnection: demo
    targetConnection: demo
    sourceQuery: SELECT id, col7 FROM src
    targetTableFinal: tgt
    insertStrategy: upsert
    masking:
      key: a-duplicate-key-test-masking-key
      columns:
        id: keep
        col7: email
        col7: keep
''')
    monkeypatch.chdir(tmp_path)

    assert main(['validate', '--quiet']) == EXIT_BAD_CONFIGURATION
    assert main(['run', '--quiet']) == EXIT_BAD_CONFIGURATION
    assert '"col7" is given twice' in caplog.text

    connection = sqlite3.connect(str(tmp_path / 'demo.db'))
    assert connection.execute('SELECT count(*) FROM tgt').fetchone()[0] == 0
    connection.close()

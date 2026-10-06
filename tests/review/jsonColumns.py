"""The JSON columns that hid personal data from `discover` and `audit
--connect` at once, once PostgreSQL's json arrived as text: documents with
an email address and an SSN among a few nulls, and bare JSON strings each an
email address. Both proposed `keep`, "no sign of personal data", and the
audit of a job keeping them said nothing. Shared by the SQLite test, where
JSON is text with nothing to say so, and the PostgreSQL one, where the
column's type says so.
"""
import json
from pathlib import Path
from typing import Callable, List, Tuple

from bauta.cli import EXIT_JOBS_DID_NOT_SUCCEED, EXIT_SUCCESS, main

ROWS = 20


def rows() -> List[Tuple[int, str, str]]:
    """id, events, contact: one event in ten null, and one a JSON null."""

    made = []
    for index in range(ROWS):
        event = None if index % 10 == 3 else 'null' if index == 7 else json.dumps(
            {'kind': 'login', 'user': {'email': 'person{}@corp.example'.format(index), 'ssn': '219-09-{:04d}'.format(9000 + index)}})
        made.append((index, event, json.dumps('person{}@corp.example'.format(index))))

    return made


def assertBothCatchIt(workspace: Path, connectionsYaml: str, table: str, capsys: Callable) -> None:
    """discover proposes masking for both columns, and audit --connect warns
    about a job keeping them, from `table` in the connection `source`.
    """
    configuration = workspace / 'configuration'
    configuration.mkdir(exist_ok=True)
    (configuration / 'connections.yaml').write_text(connectionsYaml)
    (configuration / 'jobs.yaml').write_text('''workers: 1
jobs:
  copyEvents:
    sourceConnection: source
    sourceQuery: SELECT id, events, contact FROM {table}
    targetConnection: copy
    targetTableFinal: {table}
    insertStrategy: upsert
    masking:
      key: a-json-columns-test-masking-key-0123
      columns: {{id: keep, events: keep, contact: keep}}
'''.format(table=table))

    capsys.readouterr()
    assert main(['discover', '--quiet', '--config', str(configuration), '--connection', 'source', '--table', table, '--target', 'copy']) == EXIT_SUCCESS
    proposed = capsys.readouterr().out
    events = next(line for line in proposed.splitlines() if line.strip().startswith('events:'))
    contact = next(line for line in proposed.splitlines() if line.strip().startswith('contact:'))
    assert 'json' in events and 'user.email' in events and 'user.ssn' in events, events
    assert 'strategy: email' in contact, contact

    assert main(['audit', '--quiet', '--config', str(configuration), '--connect', '--strict']) == EXIT_JOBS_DID_NOT_SUCCEED
    audited = capsys.readouterr().out
    assert 'events' in audited and 'contact' in audited
    assert 'person1@corp.example' not in audited and '219-09' not in audited

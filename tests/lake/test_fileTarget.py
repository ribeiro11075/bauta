"""Jobs writing Parquet into a files connection, from a real SQLite source:
what a reader finds after a run, after a failed run, and after several.
"""
import datetime
import decimal
import json
import logging
import sqlite3

import pytest

pytest.importorskip('pyarrow', reason='a files connection writes with pyarrow (pip install -e ".[files]")')

import pyarrow  # noqa: E402
import pyarrow.dataset  # noqa: E402
import pyarrow.parquet  # noqa: E402

from bauta.configuration import ConfigurationError, connectionConfig  # noqa: E402
from bauta.database import Database  # noqa: E402
from bauta.lake import FileTarget, FileTypeError  # noqa: E402
from bauta.jobs.pipeline import _executeDataJob  # noqa: E402
from tests.jobConfigs import dataJob  # noqa: E402

ROWS = 1000


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'prod.db'
    connection = sqlite3.connect(path)
    try:
        connection.execute('create table customers (id integer primary key, email text, balance real, notes text)')
        connection.executemany('insert into customers values (?, ?, ?, ?)',
                               [(i, 'user{}@example.com'.format(i), i * 1.25, None) for i in range(1, ROWS + 1)])
        connection.commit()
    finally:
        connection.close()
    return path


def _run(tmp_path, source, job='j', lake=None, **overrides):
    settings = {
        'prod': connectionConfig(type='sqlite', path=str(source)),
        'lake': connectionConfig(type='files', root=str(tmp_path / 'lake'), **(lake or {})),
        }
    fields = dict(sourceConnection='prod', targetConnection='lake', sourceQuery='select id, email, balance, notes from customers',
                  targetTableFinal='crm/customers', insertStrategy='overwrite', chunkSize=100)
    fields.update(overrides)
    return _executeDataJob(job, dataJob(**fields), settings)


def _snapshots(tmp_path):
    return sorted((tmp_path / 'lake' / 'crm' / 'customers').glob('snapshot=*'))


def _read(directory):
    return pyarrow.dataset.dataset(str(directory), format='parquet').to_table()


def _visibleFiles(tmp_path):
    """What a reader globbing the root finds: everything but the hidden."""
    return sorted(str(path.relative_to(tmp_path / 'lake')) for path in (tmp_path / 'lake').rglob('*')
                  if path.is_file() and not any(part.startswith(('_', '.')) for part in path.relative_to(tmp_path / 'lake').parts))


def test_an_overwrite_publishes_a_complete_snapshot_saying_what_it_holds(tmp_path, source):
    outcome = _run(tmp_path, source)

    snapshot, = _snapshots(tmp_path)
    table = _read(snapshot)
    success = json.loads((snapshot / '_SUCCESS').read_text())

    assert outcome.rowCount == table.num_rows == success['rows'] == ROWS
    assert table.column('id').to_pylist() == list(range(1, ROWS + 1))
    assert success['columns'] == [{'name': 'id', 'type': 'int64'}, {'name': 'email', 'type': 'string'},
                                  {'name': 'balance', 'type': 'float64'}, {'name': 'notes', 'type': 'string'}]
    assert not list((tmp_path / 'lake' / '_bauta_staging').iterdir())


def test_overwrites_keep_the_newest_complete_snapshots_and_nothing_older(tmp_path, source):
    for _ in range(4):
        _run(tmp_path, source, lake={'keepSnapshots': 2})

    snapshots = _snapshots(tmp_path)

    assert len(snapshots) == 2
    assert all((snapshot / '_SUCCESS').exists() for snapshot in snapshots)


def test_an_incomplete_snapshot_left_by_a_crash_is_removed_by_the_next_overwrite(tmp_path, source):
    """A process killed between moving a snapshot's parts in and writing its
    _SUCCESS leaves a directory no run would otherwise ever finish.
    """
    crashed = tmp_path / 'lake' / 'crm' / 'customers' / 'snapshot=20000101T000000Z-000000'
    crashed.mkdir(parents=True)
    (crashed / 'part-x.parquet').write_bytes(b'')

    _run(tmp_path, source)

    assert not crashed.exists()
    assert len(_snapshots(tmp_path)) == 1


def test_appends_add_parts_beside_the_ones_already_there(tmp_path, source):
    first = _run(tmp_path, source, insertStrategy='append')
    second = _run(tmp_path, source, insertStrategy='append', sourceQuery='select id, email, balance, notes from customers where id <= 10')

    table = _read(tmp_path / 'lake' / 'crm' / 'customers')

    assert (first.rowCount, second.rowCount, table.num_rows) == (ROWS, 10, ROWS + 10)
    assert not _snapshots(tmp_path)


def test_an_append_that_finds_no_rows_writes_no_file(tmp_path, source):
    _run(tmp_path, source, insertStrategy='append', sourceQuery='select id, email, balance, notes from customers where id < 0')

    assert _visibleFiles(tmp_path) == []


def test_an_overwrite_of_no_rows_still_writes_the_tables_columns(tmp_path, source, caplog):
    """An empty snapshot is still the table's latest state; a reader should
    find no rows, not the previous snapshot, and still know the columns. With
    no rows to settle types by, there is nothing to warn about either.
    """
    with caplog.at_level(logging.WARNING, logger='bauta'):
        _run(tmp_path, source, sourceQuery='select id, email, balance, notes from customers where id < 0', targetColumnTypes={'id': 'int64'})

    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]

    table = _read(_snapshots(tmp_path)[0])

    assert table.num_rows == 0
    assert table.schema.field('id').type == pyarrow.int64()
    assert table.column_names == ['id', 'email', 'balance', 'notes']


def test_a_single_file_is_replaced_whole(tmp_path, source):
    _run(tmp_path, source, singleFile=True, lake={'fileSize': 1})
    _run(tmp_path, source, singleFile=True, sourceQuery='select id, email, balance, notes from customers where id <= 5')

    assert _visibleFiles(tmp_path) == ['crm/customers.parquet']
    assert pyarrow.parquet.read_table(str(tmp_path / 'lake' / 'crm' / 'customers.parquet')).num_rows == 5


def test_parts_roll_at_the_file_size_and_row_groups_at_the_row_group_size(tmp_path, source):
    _run(tmp_path, source, lake={'fileSize': '8KB', 'rowGroupSize': '4KB'})

    parts = sorted(_snapshots(tmp_path)[0].glob('part-*.parquet'))
    metadata = [pyarrow.parquet.read_metadata(str(part)) for part in parts]

    assert len(parts) > 1
    assert all(entry.num_row_groups >= 1 for entry in metadata)
    assert sum(entry.num_rows for entry in metadata) == ROWS
    assert max(entry.num_row_groups for entry in metadata) < ROWS // 100, 'a row group per chunk would be one per 100 rows'


def test_a_failed_job_leaves_nothing_a_reader_sees(tmp_path, source):
    """Parts are written in staging and published only at the end, so a job
    failing part-way leaves the table as it was.
    """
    _run(tmp_path, source, insertStrategy='append')
    before = _visibleFiles(tmp_path)

    with pytest.raises(FileTypeError):
        # Every tenth id is text after the first chunk: the column settled as
        # int64 can't take it.
        _run(tmp_path, source, insertStrategy='append', lake={'rowGroupSize': 1},
             sourceQuery="select case when id > 500 and id % 10 = 0 then 'x' else id end as id, email, balance, notes from customers")

    assert _visibleFiles(tmp_path) == before
    assert not list((tmp_path / 'lake' / '_bauta_staging').iterdir())


def _failingOnMove(monkeypatch, failure, at=3):
    """Store.move failing with `failure` on its `at`th call: a cloud copy
    going wrong, or the process being killed, part-way through publishing.
    """
    from bauta.lake import stores

    move, calls = stores.Store.move, []

    def failing(self, source, destination):
        calls.append(destination)
        if len(calls) == at:
            raise failure
        return move(self, source, destination)

    monkeypatch.setattr(stores.Store, 'move', failing)
    return calls


def test_an_append_failing_part_way_through_publishing_takes_back_what_it_moved(tmp_path, source, monkeypatch):
    """Parts are moved into the table one at a time, so an append that failed
    on its third move had published two: 12,000 of 20,000 rows were visible,
    and the next run, from the same watermark, appended them again.
    """
    calls = _failingOnMove(monkeypatch, OSError('the copy failed'))

    with pytest.raises(OSError):
        _run(tmp_path, source, insertStrategy='append', lake={'fileSize': '4KB', 'rowGroupSize': '2KB'})

    assert len(calls) == 3
    assert _visibleFiles(tmp_path) == []
    assert not list((tmp_path / 'lake' / '_bauta_staging').iterdir())


def test_the_next_run_takes_back_what_a_killed_append_had_published(tmp_path, source, monkeypatch, caplog):
    """A process killed among the moves runs no abort(): its intent file,
    left in staging, tells the next run of the table which files to remove
    before appending the same rows again.
    """
    class Killed(BaseException):
        pass

    _failingOnMove(monkeypatch, Killed())
    monkeypatch.setattr(FileTarget, 'abort', lambda self: None)
    with pytest.raises(Killed):
        _run(tmp_path, source, insertStrategy='append', lake={'fileSize': '4KB', 'rowGroupSize': '2KB'})
    assert _visibleFiles(tmp_path)
    monkeypatch.undo()

    # Another table's run leaves the killed run's files alone.
    _run(tmp_path, source, insertStrategy='append', targetTableFinal='crm/other')
    assert _read(tmp_path / 'lake' / 'crm' / 'customers').num_rows > 0

    with caplog.at_level(logging.WARNING, logger='bauta'):
        _run(tmp_path, source, insertStrategy='append')

    table = _read(tmp_path / 'lake' / 'crm' / 'customers')
    assert table.num_rows == len(set(table.column('id').to_pylist())) == ROWS
    assert 'removed the 2 part(s) it had published' in caplog.text
    assert not list((tmp_path / 'lake' / '_bauta_staging').iterdir())


def test_an_overwrite_writes_no_intent_so_a_complete_snapshot_is_never_taken_back(tmp_path, source):
    _run(tmp_path, source)
    _run(tmp_path, source, insertStrategy='append', targetTableFinal='crm/other')

    snapshot, = _snapshots(tmp_path)
    assert _read(snapshot).num_rows == ROWS


def test_a_float_is_never_written_into_an_integer_column_as_its_truncation(tmp_path, source):
    """pyarrow writes 1.5 into an int64 column as 1 without a word."""
    with pytest.raises(FileTypeError, match='column balance is written as int64, and a row holds float'):
        _run(tmp_path, source, targetColumnTypes={'balance': 'int64'})


def test_a_declared_decimal_takes_floats_by_their_text_and_refuses_lost_places(tmp_path, source):
    _run(tmp_path, source, targetColumnTypes={'balance': 'decimal(12,2)'})

    values = _read(_snapshots(tmp_path)[0]).column('balance').to_pylist()
    assert values[:3] == [decimal.Decimal('1.25'), decimal.Decimal('2.50'), decimal.Decimal('3.75')]

    with pytest.raises(FileTypeError, match='column balance is written as decimal\\(12,1\\).*doesn\'t fit'):
        _run(tmp_path, source, targetColumnTypes={'balance': 'decimal(12,1)'})


def test_a_column_null_throughout_the_first_row_group_is_text_and_takes_no_number_later(tmp_path, source, caplog):
    """A column with no value yet has no type to go by. It is written as text,
    said so, and a later number fails rather than becoming its text unseen.
    """
    with caplog.at_level(logging.WARNING, logger='bauta'):
        with pytest.raises(FileTypeError, match='column notes is written as string, and a row holds int'):
            _run(tmp_path, source, lake={'rowGroupSize': 1},
                 sourceQuery='select id, email, balance, case when id > 500 then id end as notes from customers')

    assert any('notes held no value in the first 100 row(s), so it is written as string' in record.getMessage() for record in caplog.records)


def test_values_parquet_has_no_type_for_are_written_as_bauta_writes_them_as_text(tmp_path):
    target = FileTarget('j', dataJob(sourceConnection='prod', targetConnection='lake', targetTableFinal='t', insertStrategy='overwrite'),
                        connectionConfig(type='files', root=str(tmp_path / 'lake')))
    target.begin(['meta', 'span', 'at', 'on', 'raw'])
    target.write([({'a': [1, 2]}, datetime.timedelta(hours=30, microseconds=5),
                   datetime.datetime(2026, 1, 1, 12, tzinfo=datetime.timezone(datetime.timedelta(hours=2))), datetime.date(2026, 1, 1),
                   bytearray(b'\x00\x01'))])
    target.finish(1)

    table = _read(next((tmp_path / 'lake' / 't').glob('snapshot=*')))

    assert table.to_pylist() == [{'meta': '{"a": [1, 2]}', 'span': '30:00:00.000005', 'at': datetime.datetime(2026, 1, 1, 10, tzinfo=datetime.timezone.utc),
                                  'on': datetime.date(2026, 1, 1), 'raw': b'\x00\x01'}]
    assert str(table.schema.field('at').type) == 'timestamp[us, tz=UTC]'


def test_a_decimal_takes_the_precision_and_scale_the_driver_reports(tmp_path):
    target = FileTarget('j', dataJob(sourceConnection='prod', targetConnection='lake', targetTableFinal='t', insertStrategy='overwrite'),
                        connectionConfig(type='files', root=str(tmp_path / 'lake')))
    target.begin(['pg', 'duck', 'bare'], [('pg', 1700, None, None, 12, 2, None), ('duck', 'DECIMAL(18,3)', None, None, None, None, None),
                                          ('bare', None, None, None, None, None, None)])
    target.write([(decimal.Decimal('1.20'), decimal.Decimal('1.200'), decimal.Decimal('1.2'))])
    target.finish(1)

    schema = _read(next((tmp_path / 'lake' / 't').glob('snapshot=*'))).schema

    assert [str(field.type) for field in schema] == ['decimal128(12, 2)', 'decimal128(18, 3)', 'decimal128(38, 10)']


@pytest.mark.parametrize('columns, targetColumns, message', [
    (['id', 'ID'], [], 'would hold two columns named id and ID'),
    (['id', 'email'], ['only'], 'targetColumns names 1'),
    ])
def test_columns_a_reader_could_not_tell_apart_are_refused_before_writing(tmp_path, columns, targetColumns, message):
    target = FileTarget('j', dataJob(sourceConnection='prod', targetConnection='lake', targetTableFinal='t', insertStrategy='overwrite',
                                     targetColumns=targetColumns),
                        connectionConfig(type='files', root=str(tmp_path / 'lake')))

    with pytest.raises(ConfigurationError, match=message):
        target.begin(columns)


def test_target_column_types_must_name_columns_the_table_has(tmp_path, source):
    with pytest.raises(ConfigurationError, match='targetColumnTypes names balanse which the table has no column for'):
        _run(tmp_path, source, targetColumnTypes={'balanse': 'float64'})


def test_a_files_connection_is_not_a_database(tmp_path):
    with pytest.raises(ConfigurationError, match='is a files connection, not a database'):
        Database(connectionSettings=connectionConfig(type='files', root=str(tmp_path)))


def test_another_job_appending_to_the_same_table_leaves_a_killed_jobs_parts_alone(tmp_path, source, monkeypatch):
    """Two jobs may append to one table, and run at once: the second taking
    back the first's intent while the first was still publishing deleted the
    parts it had just moved in. Only the job that left an intent takes it back.
    """
    class Killed(BaseException):
        pass

    _failingOnMove(monkeypatch, Killed())
    monkeypatch.setattr(FileTarget, 'abort', lambda self: None)
    with pytest.raises(Killed):
        _run(tmp_path, source, job='east', insertStrategy='append', lake={'fileSize': '4KB', 'rowGroupSize': '2KB'})
    left = _visibleFiles(tmp_path)
    monkeypatch.undo()

    _run(tmp_path, source, job='west', insertStrategy='append', sourceQuery='select id, email, balance, notes from customers where id <= 10')
    assert set(left) <= set(_visibleFiles(tmp_path))

    _run(tmp_path, source, job='east', insertStrategy='append')
    assert not set(left) & set(_visibleFiles(tmp_path))
    assert _read(tmp_path / 'lake' / 'crm' / 'customers').num_rows == ROWS + 10

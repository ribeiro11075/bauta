"""Jobs reading tables of files: what a job gets from each layout bauta writes
and other engines leave, and what a sourceQuery can't reach.
"""
import gzip
import json
import sqlite3

import pytest

pytest.importorskip('pyarrow', reason='a files connection reads with pyarrow (pip install -e ".[files]")')
pytest.importorskip('duckdb', reason='a files connection reads with DuckDB (pip install -e ".[files]")')

import pyarrow  # noqa: E402
import pyarrow.parquet  # noqa: E402

from bauta.configuration import ConfigurationError, connectionConfig  # noqa: E402
from bauta.database import openSource  # noqa: E402
from bauta.jobs.pipeline import _executeDataJob  # noqa: E402
from bauta.jobs.dependencyGraph import JobStatus  # noqa: E402
from bauta.lake.source import FileSource, checkReadable  # noqa: E402
from bauta.project import dryRunProblems  # noqa: E402
from bauta.review.bench import benchJob  # noqa: E402
from tests.jobConfigs import dataJob  # noqa: E402

KEY = 'a-file-source-test-masking-key-never-shown'


def _parquet(path, **columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    pyarrow.parquet.write_table(pyarrow.table(columns), path)


@pytest.fixture
def lake(tmp_path):
    root = tmp_path / 'lake'
    root.mkdir()
    return root


@pytest.fixture
def target(tmp_path):
    path = tmp_path / 'copy.db'
    connection = sqlite3.connect(path)
    connection.execute('create table customers (id integer primary key, email text)')
    connection.commit()
    connection.close()
    return path


def _connections(lake, target, **settings):
    return {'source': connectionConfig(type='files', root=str(lake), **settings), 'target': connectionConfig(type='sqlite', path=str(target))}


def _rows(target):
    connection = sqlite3.connect(target)
    try:
        return connection.execute('select id, email from customers order by id').fetchall()
    finally:
        connection.close()


def _read(lake, query, **settings):
    with FileSource(connectionConfig(type='files', root=str(lake), **settings)) as source:
        _, chunks = source.stream(query, chunkSize=1000)
        with chunks:
            return sorted(row for chunk in chunks for row in chunk)


def test_a_job_loads_a_directory_of_parquet_into_a_database(lake, target):
    _parquet(lake / 'customers' / 'part-1.parquet', id=[1, 2], email=['a@x.test', 'b@x.test'])
    _parquet(lake / 'customers' / 'part-2.parquet', id=[3], email=['c@x.test'])

    outcome = _executeDataJob('j', dataJob(unmasked=True), _connections(lake, target))

    assert outcome.status == JobStatus.COMPLETED and outcome.rowCount == 3
    assert _rows(target) == [(1, 'a@x.test'), (2, 'b@x.test'), (3, 'c@x.test')]


def test_a_job_reading_files_masks_as_any_other(lake, target):
    _parquet(lake / 'customers' / 'part.parquet', id=[1], email=['a@x.test'])
    job = dataJob(masking={'key': KEY, 'columns': {'id': 'keep', 'email': 'email'}})

    _executeDataJob('j', job, _connections(lake, target))

    ((identifier, masked),) = _rows(target)
    assert identifier == 1 and masked != 'a@x.test' and '@' in masked


def test_a_masking_policy_must_cover_every_column_of_the_files(lake, target):
    _parquet(lake / 'customers' / 'part.parquet', id=[1], email=['a@x.test'])
    job = dataJob(masking={'key': KEY, 'columns': {'id': 'keep'}})

    with pytest.raises(Exception, match='email'):
        _executeDataJob('j', job, _connections(lake, target))
    assert _rows(target) == []


def test_only_an_overwrite_tables_newest_complete_snapshot_is_read(lake):
    """Every snapshot holds the whole table, so reading them all multiplied it."""

    table = lake / 'customers'
    _parquet(table / 'snapshot=20260101T000000-a' / 'part.parquet', id=[1])
    (table / 'snapshot=20260101T000000-a' / '_SUCCESS').write_text('{}')
    _parquet(table / 'snapshot=20260102T000000-b' / 'part.parquet', id=[2])
    (table / 'snapshot=20260102T000000-b' / '_SUCCESS').write_text('{}')
    _parquet(table / 'snapshot=20260103T000000-c' / 'part.parquet', id=[3])

    assert _read(lake, 'select * from customers') == [(2,)]


def test_a_table_with_only_incomplete_snapshots_is_refused(lake):
    _parquet(lake / 'customers' / 'snapshot=20260103T000000-c' / 'part.parquet', id=[3])

    with pytest.raises(ConfigurationError, match='none is complete'):
        _read(lake, 'select * from customers')


def test_what_a_job_writes_is_what_a_job_reads(tmp_path, lake, target):
    """Written twice by overwrite, then read back: bauta's own layout."""

    source = tmp_path / 'prod.db'
    connection = sqlite3.connect(source)
    connection.execute('create table customers (id integer primary key, email text)')
    connection.executemany('insert into customers values (?, ?)', [(1, 'a@x.test'), (2, 'b@x.test')])
    connection.commit()
    connection.close()
    settings = {'source': connectionConfig(type='sqlite', path=str(source)), 'target': connectionConfig(type='files', root=str(lake))}
    write = dataJob(unmasked=True, insertStrategy='overwrite')
    _executeDataJob('w', write, settings)
    _executeDataJob('w', write, settings)

    _executeDataJob('r', dataJob(unmasked=True), _connections(lake, target))

    assert _rows(target) == [(1, 'a@x.test'), (2, 'b@x.test')]


def test_hidden_files_and_staging_are_not_read(lake):
    _parquet(lake / 'customers' / 'part.parquet', id=[1])
    _parquet(lake / 'customers' / '_temporary' / 'part.parquet', id=[2])
    _parquet(lake / 'customers' / '.hidden' / 'part.parquet', id=[3])

    assert _read(lake, 'select * from customers') == [(1,)]


def test_partition_directories_become_columns(lake):
    _parquet(lake / 'events' / 'year=2024' / 'part.parquet', id=[1])
    _parquet(lake / 'events' / 'year=2025' / 'part.parquet', id=[2])

    assert _read(lake, 'select id, year from events where year = 2025') == [(2, 2025)]


def test_a_nested_table_is_named_by_its_path_as_spelled(lake):
    """Case matters to a path, and matching folded names found no such directory."""

    _parquet(lake / 'Sales' / 'Orders' / 'part.parquet', id=[1])

    assert _read(lake, 'select * from "Sales/Orders"') == [(1,)]


def test_a_query_may_join_tables_and_use_common_table_expressions(lake):
    _parquet(lake / 'people' / 'part.parquet', id=[1, 2], name=['ann', 'bob'])
    _parquet(lake / 'orders' / 'part.parquet', person=[2], total=[9])

    query = 'with buyers as (select person, total from orders) select name, total from people join buyers on people.id = buyers.person'

    assert _read(lake, query) == [('bob', 9)]


def test_a_single_file_is_read_by_its_table_name_or_its_own(lake):
    _parquet(lake / 'customers.parquet', id=[1])

    assert _read(lake, 'select * from customers') == [(1,)]
    assert _read(lake, 'select * from "customers.parquet"') == [(1,)]


def test_csv_is_read_with_the_connections_delimiter(lake):
    (lake / 'people').mkdir()
    (lake / 'people' / 'part.csv').write_text('id;name\n1;ann\n')

    assert _read(lake, 'select id, name from people', format='csv', delimiter=';') == [(1, 'ann')]


def test_gzipped_csv_is_read(lake):
    (lake / 'people').mkdir()
    with gzip.open(lake / 'people' / 'part.csv.gz', 'wt') as file:
        file.write('id,name\n1,ann\n')

    assert _read(lake, 'select id, name from people', format='csv') == [(1, 'ann')]


def test_json_lines_are_read(lake):
    (lake / 'people').mkdir()
    (lake / 'people' / 'part.ndjson').write_text(json.dumps({'id': 1, 'name': 'ann'}) + '\n')

    assert _read(lake, 'select id, name from people', format='ndjson') == [(1, 'ann')]


def test_an_incremental_job_reads_past_its_watermark(lake, target):
    _parquet(lake / 'customers' / 'part.parquet', id=[1, 2, 3], email=['a', 'b', 'c'])
    job = dataJob(unmasked=True, sourceQuery='select * from customers where id > {{ watermark }}', watermarkColumn='id', watermarkInitial=0)

    outcome = _executeDataJob('j', job, _connections(lake, target), watermark=1)

    assert outcome.watermark == 3 and _rows(target) == [(2, 'b'), (3, 'c')]


def test_a_partitioned_job_reads_its_slices_each_on_its_own(lake, target):
    _parquet(lake / 'customers' / 'part.parquet', id=list(range(1, 101)), email=[str(index) for index in range(1, 101)])
    job = dataJob(unmasked=True, partitions={'column': 'id', 'count': 4})

    outcome = _executeDataJob('j', job, _connections(lake, target))

    assert outcome.rowCount == 100 and len(_rows(target)) == 100


@pytest.mark.parametrize('query', ["select * from read_csv('{secret}')", "select * from '{secret}'"])
def test_a_query_reaches_nothing_outside_the_root(tmp_path, lake, query):
    """Without this, a sourceQuery could read any file bauta's account can."""

    secret = tmp_path / 'secret.csv'
    secret.write_text('password\nhunter2\n')

    with pytest.raises(Exception) as caught:
        _read(lake, query.format(secret=secret))
    assert 'hunter2' not in str(caught.value)


@pytest.mark.parametrize('table', ['"../secret"', '"/etc"', '"_bauta_staging/run"'])
def test_a_table_must_be_a_visible_path_under_the_root(lake, table):
    with pytest.raises(ConfigurationError, match='sourceQuery reads'):
        _read(lake, 'select * from {}'.format(table))


def test_a_query_cannot_write(tmp_path, lake):
    with FileSource(connectionConfig(type='files', root=str(lake))) as source, pytest.raises(Exception, match='disabled|Permission'):
        source.cursor.execute("copy (select 1) to '{}'".format(tmp_path / 'written.csv'))
    assert not (tmp_path / 'written.csv').exists()


def test_a_query_cannot_turn_the_lockdown_off(lake):
    with FileSource(connectionConfig(type='files', root=str(lake))) as source, pytest.raises(Exception, match='external access'):
        source.cursor.execute('set enable_external_access = true')


def test_a_missing_table_is_named(lake):
    with pytest.raises(ConfigurationError, match="'customers'.*holds no such table"):
        _read(lake, 'select * from customers')


def test_a_query_that_does_not_balance_is_refused(lake):
    with pytest.raises(ConfigurationError, match='balance'):
        _read(lake, 'select (1 from customers')


def test_open_source_opens_a_database_as_a_database(target):
    with openSource(connectionConfig(type='sqlite', path=str(target))) as database:
        assert not isinstance(database, FileSource)


def test_a_missing_root_is_not_readable(tmp_path):
    with pytest.raises(FileNotFoundError):
        checkReadable(connectionConfig(type='files', root=str(tmp_path / 'nowhere')))


def test_a_dry_run_only_asks_a_source_to_be_readable(lake, target):
    """A source's credentials are often read-only, so asking to write failed it."""

    _parquet(lake / 'customers' / 'part.parquet', id=[1], email=['a'])
    lake.chmod(0o555)
    try:
        assert dryRunProblems({'j': dataJob(unmasked=True)}, _connections(lake, target)) == []
    finally:
        lake.chmod(0o755)


def test_a_dry_run_finds_a_table_that_is_not_there(lake, target):
    (problem,) = dryRunProblems({'j': dataJob(unmasked=True)}, _connections(lake, target))

    assert 'holds no such table' in problem


def test_a_job_reading_files_can_be_measured(lake, target):
    _parquet(lake / 'customers' / 'part.parquet', id=[1, 2], email=['a', 'b'])

    result = benchJob('j', dataJob(unmasked=True), _connections(lake, target), rows=10)

    assert result.error is None and result.rows == 2

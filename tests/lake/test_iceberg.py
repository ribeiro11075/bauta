"""Jobs writing Iceberg tables, through a SQL catalog in SQLite with its
warehouse on disk -- the catalog pyiceberg keeps without a server -- from a
real SQLite source: what a reader finds after a run, a failed run, and many.
"""
import datetime
import decimal
import sqlite3

import pytest

pytest.importorskip('pyiceberg', reason='an Iceberg connection writes with pyiceberg (pip install -e ".[iceberg]")')

import pyarrow  # noqa: E402

from bauta.configuration import Configuration, ConfigurationError, DataJobsFile, connectionConfig  # noqa: E402
from bauta.lake import FileTypeError  # noqa: E402
from bauta.lake.iceberg import catalogProperties, loadCatalog  # noqa: E402
from bauta.jobs.pipeline import _executeDataJob  # noqa: E402
from tests.jobConfigs import dataJob, dataJobFields  # noqa: E402

ROWS = 300


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'prod.db'
    connection = sqlite3.connect(path)
    try:
        connection.execute('create table customers (id integer primary key, email text, amount real, joined text)')
        connection.executemany('insert into customers values (?, ?, ?, ?)',
                               [(i, 'user{}@example.com'.format(i), i * 1.5, '2026-01-02') for i in range(1, ROWS + 1)])
        connection.commit()
    finally:
        connection.close()
    return path


def _lake(tmp_path, **settings):
    return connectionConfig(**{'type': 'iceberg', 'catalog': 'sql', 'uri': 'sqlite:///{}'.format(tmp_path / 'catalog.db'),
                               'warehouse': str(tmp_path / 'warehouse'), 'namespace': 'masked', **settings})


def _run(tmp_path, source, lake=None, **overrides):
    settings = {'prod': connectionConfig(type='sqlite', path=str(source)), 'lake': _lake(tmp_path, **(lake or {}))}
    fields = dict(sourceConnection='prod', targetConnection='lake', sourceQuery='select id, email, amount from customers',
                  targetTableFinal='customers', insertStrategy='append', chunkSize=50, unmasked=True)
    fields.update(overrides)
    return _executeDataJob('j', dataJob(**fields), settings)


def _table(tmp_path, name='customers'):
    with loadCatalog(_lake(tmp_path)) as catalog:
        return catalog.load_table(('masked', name))


def _rows(tmp_path, name='customers'):
    return sorted(_table(tmp_path, name).scan().to_arrow().to_pylist(), key=lambda row: row['id'])


def _dataFiles(tmp_path, name='customers'):
    return sorted(path.name for path in (tmp_path / 'warehouse' / 'masked' / name / 'data').glob('*.parquet'))


def test_a_run_creates_the_table_and_commits_its_rows_once(tmp_path, source):
    _run(tmp_path, source)

    table = _table(tmp_path)

    assert len(_rows(tmp_path)) == ROWS
    assert [(field.name, str(field.field_type)) for field in table.schema().fields] == [('id', 'long'), ('email', 'string'), ('amount', 'double')]
    assert len(table.snapshots()) == 1, 'a run of six chunks is one commit'
    assert table.current_snapshot().summary['bauta.job'] == 'j'


def test_appends_add_rows_and_an_overwrite_replaces_them_in_one_commit(tmp_path, source):
    _run(tmp_path, source)
    _run(tmp_path, source, sourceQuery='select id, email, amount from customers where id <= 10')
    before = len(_table(tmp_path).snapshots())

    _run(tmp_path, source, insertStrategy='overwrite', sourceQuery='select id, email, amount from customers where id <= 5')

    table = _table(tmp_path)
    overwrite = [snapshot for snapshot in table.snapshots()][before:]

    assert [row['id'] for row in _rows(tmp_path)] == [1, 2, 3, 4, 5]
    # The delete and the add are two snapshots, committed together: no
    # reader sees the table empty between them.
    assert {snapshot.summary['bauta.run'] for snapshot in overwrite if 'bauta.run' in snapshot.summary.additional_properties}


def test_an_overwrite_of_no_rows_empties_the_table(tmp_path, source):
    _run(tmp_path, source)

    _run(tmp_path, source, insertStrategy='overwrite', sourceQuery='select id, email, amount from customers where id < 0')

    assert _rows(tmp_path) == []


def test_an_upsert_merges_by_the_key_it_creates_the_table_with(tmp_path, source):
    _run(tmp_path, source, insertStrategy='upsert', targetKey=['id'])
    _run(tmp_path, source, insertStrategy='upsert', targetKey=['id'],
         sourceQuery="select id, 'changed' as email, amount from customers where id in (2, 3) union all select 999, 'new', 1.0")

    rows = _rows(tmp_path)
    table = _table(tmp_path)

    assert len(rows) == ROWS + 1
    assert [row['email'] for row in rows[:4]] == ['user1@example.com', 'changed', 'changed', 'user4@example.com']
    assert [table.schema().find_column_name(fieldId) for fieldId in table.schema().identifier_field_ids] == ['id']


def test_an_upsert_keeps_the_last_row_of_a_key_repeated_in_a_row_group(tmp_path, source):
    """pyiceberg refuses a merge that names a key twice."""
    _run(tmp_path, source, insertStrategy='upsert', targetKey=['id'],
         sourceQuery="select 1 as id, 'first' as email, 1.0 as amount union all select 1, 'last', 2.0")

    assert _rows(tmp_path) == [{'id': 1, 'email': 'last', 'amount': 2.0}]


def test_an_upsert_without_a_key_is_refused_before_anything_is_written(tmp_path, source):
    _run(tmp_path, source)

    with pytest.raises(ConfigurationError, match='has no identifier fields, so an upsert cannot match its rows; name the key columns in targetKey'):
        _run(tmp_path, source, insertStrategy='upsert')


def test_a_key_that_is_null_is_refused_naming_the_column(tmp_path, source):
    with pytest.raises(FileTypeError, match='column id is a key and a row holds no value in it'):
        _run(tmp_path, source, insertStrategy='upsert', targetKey=['id'], sourceQuery='select null as id, email, amount from customers')


def test_a_failed_run_leaves_the_table_and_its_directory_as_they_were(tmp_path, source):
    """Parts no commit names are never read, and deleted on the way out."""
    _run(tmp_path, source)
    before = (_rows(tmp_path), _dataFiles(tmp_path))

    with pytest.raises(FileTypeError):
        _run(tmp_path, source, lake={'rowGroupSize': 1},
             sourceQuery="select case when id > 150 then 'x' else id end as id, email, amount from customers")

    assert (_rows(tmp_path), _dataFiles(tmp_path)) == before


def test_an_existing_table_writes_each_column_as_the_table_has_it(tmp_path, source):
    """The query settles amount as a double; the table says decimal, and
    pyiceberg refuses a file whose types differ from the table's.
    """
    with loadCatalog(_lake(tmp_path)) as catalog:
        catalog.create_namespace('masked')
        catalog.create_table(('masked', 'customers'), schema=pyarrow.schema([
            pyarrow.field('ID', pyarrow.int32()), pyarrow.field('email', pyarrow.string()), pyarrow.field('amount', pyarrow.decimal128(10, 2))]))

    _run(tmp_path, source, sourceQuery='select id, email, amount from customers where id <= 2')

    rows = sorted(_table(tmp_path).scan().to_arrow().to_pylist(), key=lambda row: row['ID'])

    assert [(row['ID'], row['amount']) for row in rows] == [(1, decimal.Decimal('1.50')), (2, decimal.Decimal('3.00'))]


def test_a_column_the_table_lacks_is_refused_unless_the_schema_may_evolve(tmp_path, source):
    _run(tmp_path, source)
    wider = 'select id, email, amount, joined from customers where id <= 2'

    with pytest.raises(ConfigurationError, match='masked.customers has no column joined; add it to the table'):
        _run(tmp_path, source, sourceQuery=wider)

    _run(tmp_path, source, lake={'evolveSchema': True}, sourceQuery=wider)

    assert [field.name for field in _table(tmp_path).schema().fields] == ['id', 'email', 'amount', 'joined']
    assert sorted((row['joined'] is None) for row in _rows(tmp_path)) == [False] * 2 + [True] * ROWS


def test_a_declared_type_the_table_disagrees_with_is_refused(tmp_path, source):
    _run(tmp_path, source)

    with pytest.raises(ConfigurationError, match='targetColumnTypes declares amount as decimal\\(12,2\\), and the table has it as float64'):
        _run(tmp_path, source, targetColumnTypes={'amount': 'decimal(12,2)'})


def test_old_snapshots_go_and_so_do_the_files_only_they_referenced(tmp_path, source):
    """pyiceberg forgets an expired snapshot and leaves its files, where a row
    since overwritten -- someone's data, a mask under a rotated key -- stays.
    """
    for _ in range(4):
        _run(tmp_path, source, insertStrategy='overwrite', lake={'keepSnapshots': 2})

    table = _table(tmp_path)
    referenced = {path.rsplit('/', 1)[-1] for path in table.inspect.all_files().column('file_path').to_pylist()}

    assert len(table.snapshots()) == 2
    assert set(_dataFiles(tmp_path)) == referenced


def test_integers_iceberg_has_no_type_for_are_written_as_the_nearest_it_has(tmp_path, source):
    _run(tmp_path, source, sourceQuery='select id, email, id as big from customers', targetColumnTypes={'id': 'int16', 'big': 'uint64'})

    assert [str(field.field_type) for field in _table(tmp_path).schema().fields] == ['int', 'string', 'decimal(20, 0)']


def test_a_table_bauta_creates_deletes_old_metadata_as_it_commits(tmp_path, source):
    _run(tmp_path, source)

    assert _table(tmp_path).properties['write.metadata.delete-after-commit.enabled'] == 'true'


def test_the_cloud_settings_reach_pyiceberg_under_its_names():
    settings = connectionConfig(type='iceberg', catalog='glue', warehouse='s3://lake/warehouse', region='eu-west-1', accessKeyId='AKIA',
                                secretAccessKey='s3cret', properties={'glue.id': '123456789012'})

    properties = catalogProperties(settings)

    assert {name: properties[name] for name in ('type', 'warehouse', 's3.region', 'glue.region', 's3.access-key-id', 'glue.secret-access-key',
                                                'glue.id')} == {
        'type': 'glue', 'warehouse': 's3://lake/warehouse', 's3.region': 'eu-west-1', 'glue.region': 'eu-west-1', 's3.access-key-id': 'AKIA',
        'glue.secret-access-key': 's3cret', 'glue.id': '123456789012'}
    assert 's3cret' not in repr(settings)


@pytest.mark.parametrize('job, message', [
    ({'insertStrategy': 'swap', 'targetTableStage': 'stage'}, 'is an Iceberg connection, which takes insertStrategy append, overwrite or upsert'),
    ({'singleFile': True, 'insertStrategy': 'overwrite'}, 'singleFile is for a files connection, and it is an Iceberg connection'),
    ({'targetTableFinal': 'crm/customers'}, "targetTableFinal 'crm/customers' is not namespace.table"),
    ({'insertStrategy': 'upsert', 'preTargetAdhocQueries': ['select 1']}, 'preTargetAdhocQueries is for a database'),
    ])
def test_an_iceberg_job_is_checked_against_its_connection(tmp_path, job, message):
    jobsFile = Configuration.validateJobConfiguration({'jobs': {'j': dataJobFields(**{
        'sourceConnection': 'prod', 'targetConnection': 'lake', 'targetTableFinal': 'customers', 'insertStrategy': 'append', **job})}},
        DataJobsFile)
    connections = {'prod': connectionConfig(type='sqlite', path='prod.db'), 'lake': _lake(tmp_path)}

    with pytest.raises(ConfigurationError, match=message):
        Configuration.validateJobGraph(jobsFile.jobs, connections=connections)


@pytest.mark.parametrize('settings, message', [
    ({'catalog': 'sql', 'warehouse': 'w'}, 'a sql catalog needs uri'),
    ({'catalog': 'rest'}, 'a rest catalog needs uri'),
    ({'catalog': 'glue'}, 'a glue catalog needs warehouse'),
    ({'catalog': 'sql', 'uri': 'sqlite://', 'warehouse': 'w', 'token': 't'}, 'token is for a rest catalog'),
    ({'catalog': 'sql', 'uri': 'sqlite://', 'warehouse': 'gs://b/w', 'region': 'eu-west-1'}, 'region is for S3, and warehouse is on Google Cloud Storage'),
    ({'catalog': 'sql', 'uri': 'sqlite://', 'warehouse': 'gs://b/w', 'anonymous': True}, 'not something pyiceberg can write'),
    ({'catalog': 'sql', 'uri': 'sqlite://', 'warehouse': 'w', 'compression': 'none', 'namespace': 'a.b'}, 'namespace must be a plain identifier'),
    ])
def test_an_iceberg_connection_refuses_settings_that_could_not_work(settings, message):
    with pytest.raises(ConfigurationError, match=message):
        connectionConfig(type='iceberg', **settings)


def test_timestamps_keep_their_zone_as_iceberg_does(tmp_path):
    """A time zone-aware time is Iceberg's timestamptz, in UTC."""
    path = tmp_path / 'times.db'
    connection = sqlite3.connect(path)
    connection.execute('create table events (id integer primary key)')
    connection.execute('insert into events values (1)')
    connection.commit()
    connection.close()

    from bauta.lake.iceberg import IcebergTarget

    target = IcebergTarget('j', dataJob(sourceConnection='prod', targetConnection='lake', targetTableFinal='events', insertStrategy='append'),
                           _lake(tmp_path))
    try:
        target.begin(['id', 'at'])
        target.write([(1, datetime.datetime(2026, 1, 2, 5, tzinfo=datetime.timezone(datetime.timedelta(hours=2))))])
        target.finish(1)
    finally:
        target.close()

    table = _table(tmp_path, 'events')

    assert str(table.schema().find_field('at').field_type) == 'timestamptz'
    assert table.scan().to_arrow().column('at').to_pylist() == [datetime.datetime(2026, 1, 2, 3, tzinfo=datetime.timezone.utc)]

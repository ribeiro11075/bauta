"""Iceberg tables through each catalog, against docker-compose.yml's services:
a SQL catalog with its warehouse in each cloud's emulator, Glue as moto
serves it, and the Apache Iceberg project's REST catalog with its files in
moto's S3. What a reader finds after appends, an overwrite, an upsert and a
failed run, and that old snapshots' files go.
"""
import sqlite3
import uuid

import pytest

pytest.importorskip('pyiceberg', reason='an Iceberg connection writes with pyiceberg (pip install -e ".[iceberg]")')

import pyarrow.fs  # noqa: E402

from bauta.configuration import connectionConfig  # noqa: E402
from bauta.files import FileTypeError, checkWritable  # noqa: E402
from bauta.files.iceberg import loadCatalog  # noqa: E402
from bauta.jobs.pipeline import _executeDataJob  # noqa: E402
from tests.jobConfigs import dataJob  # noqa: E402

pytestmark = pytest.mark.integration

ROWS = 200

S3 = {'region': 'us-east-1', 'endpoint': 'http://127.0.0.1:5055', 'accessKeyId': 'test', 'secretAccessKey': 'test'}
AZURITE_KEY = 'Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=='


def _s3():
    return pyarrow.fs.S3FileSystem(access_key='test', secret_key='test', region='us-east-1', endpoint_override='http://127.0.0.1:5055',
                                   allow_bucket_creation=True)


def _gcs():
    return pyarrow.fs.GcsFileSystem(anonymous=True, scheme='http', endpoint_override='127.0.0.1:4443', project_id='bauta')


def _azure():
    return pyarrow.fs.AzureFileSystem(account_name='devstoreaccount1', account_key=AZURITE_KEY, blob_storage_authority='127.0.0.1:10000',
                                      blob_storage_scheme='http', dfs_storage_authority='127.0.0.1:10000', dfs_storage_scheme='http')


def _bucket(filesystem, name):
    try:
        filesystem.create_dir(name)
    except Exception as error:
        pytest.skip('the storage for this catalog is not available ({})'.format(error))


def _sql(tmp_path, scheme, settings, filesystem):
    bucket = 'bauta-{}'.format(uuid.uuid4().hex[:12])
    _bucket(filesystem, bucket)
    return {'catalog': 'sql', 'uri': 'sqlite:///{}'.format(tmp_path / 'catalog.db'), 'warehouse': '{}{}/warehouse'.format(scheme, bucket),
            **settings}, filesystem


def _glue(tmp_path):
    bucket = 'bauta-{}'.format(uuid.uuid4().hex[:12])
    _bucket(_s3(), bucket)
    return {'catalog': 'glue', 'warehouse': 's3://{}/warehouse'.format(bucket), **S3,
            'properties': {'glue.endpoint': 'http://127.0.0.1:5055'}}, _s3()


def _rest(tmp_path):
    filesystem = _s3()
    try:
        if filesystem.get_file_info('iceberg-rest').type == pyarrow.fs.FileType.NotFound:
            filesystem.create_dir('iceberg-rest')
        with loadCatalog(connectionConfig(type='iceberg', catalog='rest', uri='http://127.0.0.1:8181', **S3)) as catalog:
            catalog.list_namespaces()
    except Exception as error:
        pytest.skip('the REST catalog is not available ({})'.format(error))
    return {'catalog': 'rest', 'uri': 'http://127.0.0.1:8181', **S3}, filesystem


CATALOGS = {
    'sql-s3': lambda tmp_path: _sql(tmp_path, 's3://', S3, _s3()),
    'sql-gcs': lambda tmp_path: _sql(tmp_path, 'gs://', {'endpoint': 'http://127.0.0.1:4443',
                                                    # The emulator takes any token; pyiceberg wants one, with an expiry.
                                                    'properties': {'gcs.oauth2.token': 'test', 'gcs.oauth2.token-expires-at': '4102444800000'}}, _gcs()),
    'sql-azure': lambda tmp_path: _sql(tmp_path, 'az://', {'endpoint': 'http://127.0.0.1:10000', 'accountName': 'devstoreaccount1',
                                                           'accountKey': AZURITE_KEY}, _azure()),
    'glue': _glue,
    'rest': _rest,
    }


@pytest.fixture(params=sorted(CATALOGS))
def lake(request, tmp_path):
    settings, filesystem = CATALOGS[request.param](tmp_path)
    return connectionConfig(type='iceberg', namespace='masked_{}'.format(uuid.uuid4().hex[:8]), **settings), filesystem


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'prod.db'
    connection = sqlite3.connect(path)
    try:
        connection.execute('create table customers (id integer primary key, email text)')
        connection.executemany('insert into customers values (?, ?)', [(i, 'user{}@example.com'.format(i)) for i in range(1, ROWS + 1)])
        connection.commit()
    finally:
        connection.close()
    return path


def _run(lake, source, **overrides):
    settings, _ = lake
    fields = dict(sourceConnection='prod', targetConnection='lake', sourceQuery='select id, email from customers', targetTableFinal='customers',
                  insertStrategy='append', chunkSize=50, unmasked=True)
    fields.update(overrides)
    return _executeDataJob('j', dataJob(**fields), {'prod': connectionConfig(type='sqlite', path=str(source)), 'lake': settings})


def _table(lake):
    settings, _ = lake
    with loadCatalog(settings) as catalog:
        return catalog.load_table((settings.namespace, 'customers'))


def _ids(lake):
    return sorted(_table(lake).scan().to_arrow().column('id').to_pylist())


def _dataFiles(lake):
    """The table's data files in storage, whether or not a snapshot names them."""
    _, filesystem = lake
    location = _table(lake).location().split('://', 1)[1]
    if location.count('@'):
        # abfss://container@account.dfs.core.windows.net/path, as pyarrow names it: container/path.
        container, rest = location.split('@', 1)
        location = container + '/' + rest.split('/', 1)[1]
    selector = pyarrow.fs.FileSelector(location + '/data', recursive=True, allow_not_found=True)
    return sorted(info.path for info in filesystem.get_file_info(selector) if info.type == pyarrow.fs.FileType.File)


def test_appends_an_overwrite_and_an_upsert_each_commit_once(lake, source):
    _run(lake, source)
    _run(lake, source, sourceQuery='select id, email from customers where id <= 10')
    assert len(_ids(lake)) == ROWS + 10

    _run(lake, source, insertStrategy='overwrite', sourceQuery='select id, email from customers where id <= 5')
    assert _ids(lake) == [1, 2, 3, 4, 5]

    _run(lake, source, insertStrategy='upsert', targetKey=['id'], sourceQuery="select id, 'changed' as email from customers where id in (5, 6)")
    rows = sorted(_table(lake).scan().to_arrow().to_pylist(), key=lambda row: row['id'])
    assert [(row['id'], row['email']) for row in rows[-2:]] == [(5, 'changed'), (6, 'changed')]


def test_a_failed_run_leaves_no_file_behind(lake, source):
    _run(lake, source)
    before = (_ids(lake), _dataFiles(lake))

    settings, filesystem = lake
    small = (settings.model_copy(update={'rowGroupSize': 1}), filesystem)

    with pytest.raises(FileTypeError):
        # A row group a chunk: two parts are written before the third fails.
        _run(small, source, sourceQuery="select case when id > 100 then 'x' else id end as id, email from customers")

    assert (_ids(lake), _dataFiles(lake)) == before


def test_old_snapshots_go_and_so_do_the_files_only_they_referenced(lake, source):
    settings, filesystem = lake
    kept = (settings.model_copy(update={'keepSnapshots': 2}), filesystem)
    for _ in range(4):
        _run(kept, source, insertStrategy='overwrite')

    table = _table(lake)
    referenced = sorted(path.split('://', 1)[1].rsplit('/', 1)[-1] for path in table.inspect.all_files().column('file_path').to_pylist())

    assert len(table.snapshots()) == 2
    assert sorted(path.rsplit('/', 1)[-1] for path in _dataFiles(lake)) == referenced


def test_the_dry_run_check_reaches_the_catalog_and_its_warehouse(lake):
    settings, _ = lake

    checkWritable(settings)

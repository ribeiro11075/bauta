"""A files connection in each cloud, against the emulators docker-compose.yml
runs: moto for S3, fake-gcs-server for Google Cloud Storage, and Azurite for
Azure Blob Storage, without a hierarchical namespace. What a bucket holds
after a run, after a failed run, and after several.

Files are listed through pyarrow, which also reports a directory where an
empty object stands for one; the test that no such object is written lists
each store's keys with its own API instead, where the emulator allows it
without signing.
"""
import gzip
import json
import sqlite3
import urllib.request
import uuid
import xml.etree.ElementTree

import pytest

pytest.importorskip('pyarrow', reason='a files connection writes with pyarrow (pip install -e ".[files]")')

import pyarrow  # noqa: E402
import pyarrow.dataset  # noqa: E402
import pyarrow.fs  # noqa: E402

from bauta.configuration import connectionConfig  # noqa: E402
from bauta.lake import FileTypeError, checkWritable  # noqa: E402
from bauta.jobs.pipeline import _executeDataJob  # noqa: E402
from tests.jobConfigs import dataJob  # noqa: E402

pytestmark = pytest.mark.integration

ROWS = 500

# Azurite's one account, and its key, published in Azure's documentation.
AZURITE_ACCOUNT = 'devstoreaccount1'
AZURITE_KEY = 'Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=='


class Cloud:
    """One emulator: the settings a files connection reaches it with, a
    filesystem to read back through, and its keys, listed with its own API.
    """

    def __init__(self, name, scheme, settings, filesystem, rawKeys=None):
        self.name = name
        self.scheme = scheme
        self.settings = settings
        self.filesystem = filesystem
        self._rawKeys = rawKeys

    def files(self, bucket):
        """Every file's path under the bucket, relative to it, but staging's."""
        selector = pyarrow.fs.FileSelector(bucket, recursive=True)
        paths = sorted(info.path[len(bucket) + 1:] for info in self.filesystem.get_file_info(selector) if info.type == pyarrow.fs.FileType.File)
        return [path for path in paths if not path.startswith('lake/_bauta_staging/')]

    def rawKeys(self, bucket):
        if self._rawKeys is None:
            pytest.skip('{} lists its keys only to a signed request'.format(self.name))
        return self._rawKeys(bucket)


def _s3Keys(bucket):
    with urllib.request.urlopen('http://127.0.0.1:5055/{}?list-type=2'.format(bucket)) as response:
        listing = xml.etree.ElementTree.fromstring(response.read())
    return sorted(element.text for element in listing.iter() if element.tag.endswith('}Key'))


def _gcsKeys(bucket):
    with urllib.request.urlopen('http://127.0.0.1:4443/storage/v1/b/{}/o'.format(bucket)) as response:
        return sorted(item['name'] for item in json.load(response).get('items', []))


CLOUDS = {
    's3': lambda: Cloud(
        'S3', 's3://', {'region': 'us-east-1', 'endpoint': 'http://127.0.0.1:5055', 'accessKeyId': 'test', 'secretAccessKey': 'test'},
        pyarrow.fs.S3FileSystem(access_key='test', secret_key='test', region='us-east-1', endpoint_override='http://127.0.0.1:5055',
                                allow_bucket_creation=True),
        _s3Keys),
    'gcs': lambda: Cloud(
        'Google Cloud Storage', 'gs://', {'endpoint': 'http://127.0.0.1:4443', 'anonymous': True},
        pyarrow.fs.GcsFileSystem(anonymous=True, scheme='http', endpoint_override='127.0.0.1:4443', project_id='bauta'),
        _gcsKeys),
    'azure': lambda: Cloud(
        'Azure', 'az://', {'endpoint': 'http://127.0.0.1:10000', 'accountName': AZURITE_ACCOUNT, 'accountKey': AZURITE_KEY},
        pyarrow.fs.AzureFileSystem(account_name=AZURITE_ACCOUNT, account_key=AZURITE_KEY, blob_storage_authority='127.0.0.1:10000',
                                   blob_storage_scheme='http', dfs_storage_authority='127.0.0.1:10000', dfs_storage_scheme='http')),
    }


@pytest.fixture(params=sorted(CLOUDS))
def cloud(request):
    return CLOUDS[request.param]()


@pytest.fixture
def bucket(cloud):
    name = 'bauta-{}'.format(uuid.uuid4().hex[:12])
    try:
        cloud.filesystem.create_dir(name)
    except Exception as error:
        pytest.skip('{} is not available ({})'.format(cloud.name, error))
    return name


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


def _lake(cloud, bucket, **settings):
    return connectionConfig(type='files', root='{}{}/lake'.format(cloud.scheme, bucket), **cloud.settings, **settings)


def _run(cloud, bucket, source, lake=None, **overrides):
    settings = {'prod': connectionConfig(type='sqlite', path=str(source)), 'lake': _lake(cloud, bucket, **(lake or {}))}
    fields = dict(sourceConnection='prod', targetConnection='lake', sourceQuery='select id, email from customers',
                  targetTableFinal='crm/customers', insertStrategy='overwrite', chunkSize=100, unmasked=True)
    fields.update(overrides)
    return _executeDataJob('j', dataJob(**fields), settings)


def _read(cloud, path):
    return pyarrow.dataset.dataset(path, filesystem=cloud.filesystem, format='parquet').to_table()


def test_overwrites_publish_complete_snapshots_and_keep_the_newest(cloud, bucket, source):
    for _ in range(3):
        _run(cloud, bucket, source, lake={'keepSnapshots': 2})

    files = cloud.files(bucket)
    snapshots = sorted({path.split('/')[3] for path in files if path.startswith('lake/crm/customers/snapshot=')})

    assert len(snapshots) == 2
    assert all('lake/crm/customers/{}/_SUCCESS'.format(snapshot) in files for snapshot in snapshots)
    assert _read(cloud, '{}/lake/crm/customers/{}'.format(bucket, snapshots[-1])).num_rows == ROWS


def test_appends_add_parts_beside_the_ones_already_there(cloud, bucket, source):
    _run(cloud, bucket, source, insertStrategy='append')
    _run(cloud, bucket, source, insertStrategy='append')

    assert len([path for path in cloud.files(bucket) if path.startswith('lake/crm/customers/part-')]) == 2
    assert _read(cloud, '{}/lake/crm/customers'.format(bucket)).num_rows == 2 * ROWS


def test_a_tables_prefix_holds_its_files_and_no_empty_objects_standing_for_directories(cloud, bucket, source):
    """pyarrow writes an empty object for each directory it is asked to
    create, which an object store doesn't have, and which some readers list
    as a file. Emptying staging leaves one, under the root and hidden.
    """
    _run(cloud, bucket, source, insertStrategy='append')

    keys = [key for key in cloud.rawKeys(bucket) if key != 'lake/_bauta_staging/']

    assert not [key for key in keys if key.endswith('/')]
    assert [key for key in keys if key.startswith('lake/crm/')] == [path for path in cloud.files(bucket) if path.startswith('lake/crm/')]


def test_a_failed_job_leaves_no_object_a_reader_sees(cloud, bucket, source):
    """An upload pyarrow is made to stop is completed, not abandoned, in all
    three clouds, so a part written in place would appear cut short. Staged,
    it is removed.
    """
    _run(cloud, bucket, source, insertStrategy='append')
    before = cloud.files(bucket)

    with pytest.raises(FileTypeError):
        _run(cloud, bucket, source, insertStrategy='append', lake={'rowGroupSize': 1},
             sourceQuery="select case when id > 250 then 'x' else id end as id, email from customers")

    assert cloud.files(bucket) == before
    assert not [info for info in cloud.filesystem.get_file_info(pyarrow.fs.FileSelector(bucket, recursive=True))
                if info.type == pyarrow.fs.FileType.File and '/_bauta_staging/' in info.path]


def test_a_single_csv_file_is_replaced_whole(cloud, bucket, source):
    _run(cloud, bucket, source, singleFile=True, lake={'format': 'csv'})
    _run(cloud, bucket, source, singleFile=True, lake={'format': 'csv'}, sourceQuery='select id, email from customers where id <= 3')

    assert cloud.files(bucket) == ['lake/crm/customers.csv.gz']
    with cloud.filesystem.open_input_stream('{}/lake/crm/customers.csv.gz'.format(bucket), compression=None) as stream:
        assert gzip.decompress(stream.read()).decode().splitlines() == ['"id","email"', '1,"user1@example.com"', '2,"user2@example.com"',
                                                                         '3,"user3@example.com"']


def test_the_dry_run_check_writes_lists_and_leaves_nothing(cloud, bucket):
    checkWritable(_lake(cloud, bucket))

    assert cloud.files(bucket) == []


def test_the_dry_run_check_fails_for_a_bucket_that_is_not_there(cloud, bucket):
    with pytest.raises(OSError):
        checkWritable(_lake(cloud, 'bauta-missing-{}'.format(uuid.uuid4().hex[:8])))

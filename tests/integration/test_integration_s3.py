"""A files connection on S3, against the S3 docker-compose.yml runs (moto):
what a bucket holds after a run, after a failed run, and after several.

Keys are listed with S3's own API rather than pyarrow's, which reports a
directory whether or not an object stands for it: the point of one test is
that no such object is written.
"""
import gzip
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
from bauta.files import FileTypeError, checkWritable  # noqa: E402
from bauta.jobs.pipeline import _executeDataJob  # noqa: E402
from tests.jobConfigs import dataJob  # noqa: E402

pytestmark = pytest.mark.integration

ENDPOINT = 'http://127.0.0.1:5055'
CREDENTIALS = {'region': 'us-east-1', 'endpoint': ENDPOINT, 'accessKeyId': 'test', 'secretAccessKey': 'test'}
ROWS = 500


def _filesystem():
    return pyarrow.fs.S3FileSystem(access_key='test', secret_key='test', region='us-east-1', endpoint_override=ENDPOINT,
                                   allow_bucket_creation=True)


@pytest.fixture
def bucket():
    name = 'bauta-{}'.format(uuid.uuid4().hex[:12])
    try:
        _filesystem().create_dir(name)
    except Exception as error:
        pytest.skip('S3 is not available at {} ({})'.format(ENDPOINT, error))
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


# pyarrow keeps an empty object for a directory it empties, so the staging
# directory is left as one, under the root and hidden, never in a table.
STAGING_MARKER = 'lake/_bauta_staging/'


def _keys(bucket):
    """Every key but the staging directory's marker."""
    with urllib.request.urlopen('{}/{}?list-type=2'.format(ENDPOINT, bucket)) as response:
        listing = xml.etree.ElementTree.fromstring(response.read())
    return sorted(element.text for element in listing.iter() if element.tag.endswith('}Key') and element.text != STAGING_MARKER)


def _run(bucket, source, lake=None, **overrides):
    settings = {
        'prod': connectionConfig(type='sqlite', path=str(source)),
        'lake': connectionConfig(type='files', root='s3://{}/lake'.format(bucket), **CREDENTIALS, **(lake or {})),
        }
    fields = dict(sourceConnection='prod', targetConnection='lake', sourceQuery='select id, email from customers',
                  targetTableFinal='crm/customers', insertStrategy='overwrite', chunkSize=100, unmasked=True)
    fields.update(overrides)
    return _executeDataJob('j', dataJob(**fields), settings)


def _read(path, format='parquet'):
    return pyarrow.dataset.dataset(path, filesystem=_filesystem(), format=format).to_table()


def test_overwrites_on_s3_publish_complete_snapshots_and_keep_the_newest(bucket, source):
    for _ in range(3):
        _run(bucket, source, lake={'keepSnapshots': 2})

    keys = _keys(bucket)
    snapshots = sorted({key.split('/')[3] for key in keys if key.startswith('lake/crm/customers/snapshot=')})

    assert len(snapshots) == 2
    assert all('lake/crm/customers/{}/_SUCCESS'.format(snapshot) in keys for snapshot in snapshots)
    assert _read('{}/lake/crm/customers/{}'.format(bucket, snapshots[-1])).num_rows == ROWS
    assert not [key for key in keys if key.startswith('lake/_bauta_staging/')]


def test_a_tables_prefix_on_s3_holds_its_files_and_no_empty_objects_standing_for_directories(bucket, source):
    """pyarrow writes an empty object for each directory it is asked to
    create, which S3 doesn't have, and which some readers list as a file.
    """
    _run(bucket, source, insertStrategy='append')
    _run(bucket, source, insertStrategy='append')

    keys = _keys(bucket)

    assert not [key for key in keys if key.endswith('/')]
    assert len([key for key in keys if key.startswith('lake/crm/customers/part-')]) == 2
    assert _read('{}/lake/crm/customers'.format(bucket)).num_rows == 2 * ROWS


def test_a_failed_job_on_s3_leaves_no_object_a_reader_sees(bucket, source):
    """An upload pyarrow is made to stop is completed, not abandoned, so a
    part written in place would appear cut short. Staged, it is removed.
    """
    _run(bucket, source, insertStrategy='append')
    before = _keys(bucket)

    with pytest.raises(FileTypeError):
        _run(bucket, source, insertStrategy='append', lake={'rowGroupSize': 1},
             sourceQuery="select case when id > 250 then 'x' else id end as id, email from customers")

    assert _keys(bucket) == before


def test_a_single_csv_file_on_s3_is_replaced_whole(bucket, source):
    _run(bucket, source, singleFile=True, lake={'format': 'csv'})
    _run(bucket, source, singleFile=True, lake={'format': 'csv'}, sourceQuery='select id, email from customers where id <= 3')

    assert [key for key in _keys(bucket) if not key.startswith('lake/_bauta_staging/')] == ['lake/crm/customers.csv.gz']
    with _filesystem().open_input_stream('{}/lake/crm/customers.csv.gz'.format(bucket), compression=None) as stream:
        assert gzip.decompress(stream.read()).decode().splitlines() == ['"id","email"', '1,"user1@example.com"', '2,"user2@example.com"',
                                                                         '3,"user3@example.com"']


def test_the_dry_run_check_writes_lists_and_leaves_nothing(bucket):
    checkWritable(connectionConfig(type='files', root='s3://{}/lake'.format(bucket), **CREDENTIALS))

    assert _keys(bucket) == []


def test_the_dry_run_check_fails_for_a_bucket_that_is_not_there():
    with pytest.raises(OSError):
        checkWritable(connectionConfig(type='files', root='s3://bauta-missing-{}/lake'.format(uuid.uuid4().hex[:8]), **CREDENTIALS))

"""Validating a files connection, and the jobs that write to one, offline:
what `bauta validate` refuses before anything runs.
"""
import functools

import pytest

from bauta.configuration import Configuration, ConfigurationError, DataJobsFile, connectionConfig, parseColumnType
from tests.jobConfigs import dataJobFields

_job = functools.partial(dataJobFields, sourceConnection='prod', targetConnection='lake', targetTableFinal='crm/customers',
                         insertStrategy='overwrite')


def _connections(**lake):
    return Configuration.validateConnectionConfiguration({
        'prod': {'type': 'sqlite', 'path': 'prod.db'},
        'staging': {'type': 'sqlite', 'path': 'staging.db'},
        'lake': {'type': 'files', 'root': 'lake', **lake},
        })


def _validate(connections=None, **job):
    jobsFile = Configuration.validateJobConfiguration({'jobs': {'j': _job(**job)}}, DataJobsFile)
    Configuration.validateJobGraph(jobsFile.jobs, connections=connections if connections is not None else _connections())
    return jobsFile.jobs['j']


def test_a_files_connection_takes_sizes_as_text_or_bytes():
    settings = connectionConfig(type='files', root='lake', fileSize='64MiB', rowGroupSize=1000)

    assert (settings.fileSize, settings.rowGroupSize) == (64 * 2 ** 20, 1000)
    assert settings.describeTarget().startswith('files in /')


def test_a_files_connection_refuses_a_url_it_cannot_write_rather_than_reading_it_as_a_directory():
    """`gs://bucket` is a valid relative path on disk: without the check a run
    would write into ./gs:/bucket and report success.
    """
    with pytest.raises(ConfigurationError, match='gs:// is not supported yet'):
        connectionConfig(type='files', root='gs://bucket/lake')


def test_an_s3_root_takes_s3s_settings_and_keeps_its_keys_secret():
    settings = connectionConfig(type='files', root='s3://bucket/lake/', region='eu-west-1', accessKeyId='AKIA', secretAccessKey='s3cret')

    assert (settings.root, settings.describeTarget()) == ('s3://bucket/lake', 'files in s3://bucket/lake')
    assert 's3cret' not in repr(settings)


@pytest.mark.parametrize('setting, message', [
    ({'root': 's3://'}, 'names no bucket'),
    ({'root': 'lake', 'region': 'eu-west-1'}, 'region is for a root on S3'),
    ({'accessKeyId': 'AKIA'}, 'set accessKeyId and secretAccessKey together'),
    ({'sessionToken': 't'}, 'sessionToken goes with accessKeyId and secretAccessKey'),
    ({'fileSize': '6GiB'}, 'fileSize can be at most 5GiB on S3'),
    ])
def test_an_s3_root_refuses_settings_that_could_not_work(setting, message):
    with pytest.raises(ConfigurationError, match=message):
        connectionConfig(**{'type': 'files', 'root': 's3://bucket/lake', **setting})


@pytest.mark.parametrize('settings, compression', [
    ({'format': 'parquet'}, 'zstd'), ({'format': 'csv'}, 'gzip'), ({'format': 'ndjson'}, 'gzip'),
    ({'format': 'csv', 'compression': 'none'}, 'none'), ({'format': 'parquet', 'compression': 'snappy'}, 'snappy'),
    ])
def test_each_format_compresses_as_it_says_or_by_its_default(settings, compression):
    assert connectionConfig(type='files', root='lake', **settings).effectiveCompression().value == compression


@pytest.mark.parametrize('setting, message', [
    ({'format': 'csv', 'compression': 'zstd'}, 'compression zstd is not one for csv; choose from gzip, none'),
    ({'format': 'ndjson', 'compression': 'snappy'}, 'compression snappy is not one for ndjson'),
    ({'format': 'parquet', 'delimiter': ';'}, 'delimiter is for format: csv'),
    ({'format': 'csv', 'delimiter': ';;'}, 'delimiter must be one character'),
    ({'format': 'csv', 'delimiter': '"'}, 'other than a quote or a line break'),
    ])
def test_a_formats_settings_must_be_ones_it_has(setting, message):
    with pytest.raises(ConfigurationError, match=message):
        connectionConfig(type='files', root='lake', **setting)


@pytest.mark.parametrize('setting, message', [
    ({'path': 'lake'}, 'path is a setting of duckdb and sqlite connections, not files'),
    ({'options': {'x': 1}}, 'options is a setting of'),
    ({'keepSnapshots': 0}, 'keepSnapshots'),
    ({'fileSize': 0}, 'fileSize: must be more than 0 bytes'),
    ({'compression': 'lz4'}, 'compression'),
    ])
def test_a_files_connection_refuses_what_it_does_not_take(setting, message):
    with pytest.raises(ConfigurationError, match=message):
        connectionConfig(type='files', root='lake', **setting)


def test_a_file_job_validates_with_the_files_connection():
    job = _validate(targetColumnTypes={'balance': 'decimal(12,2)'})

    assert job.writesFiles()
    assert parseColumnType(job.targetColumnTypes['balance']) == ('decimal', 12, 2)


@pytest.mark.parametrize('job, message', [
    ({'insertStrategy': 'swap', 'targetTableStage': 'customers_stage', 'targetTableFinal': 'customers'},
     'files connection, which takes insertStrategy append or overwrite, not swap'),
    ({'insertStrategy': 'upsert', 'targetTableFinal': 'customers'}, 'not upsert'),
    ])
def test_a_database_strategy_into_a_files_connection_is_refused(job, message):
    with pytest.raises(ConfigurationError, match=message):
        _validate(**job)


def test_a_file_strategy_into_a_database_is_refused():
    with pytest.raises(ConfigurationError, match='is a sqlite database, and insertStrategy: append writes files'):
        _validate(insertStrategy='append', targetConnection='staging')


def test_a_files_connection_is_refused_as_a_source():
    with pytest.raises(ConfigurationError, match='sourceConnection "lake" is a files connection, which can only be written to'):
        _validate(sourceConnection='lake')


@pytest.mark.parametrize('job, message', [
    ({'targetTableStage': 'stage'}, 'targetTableStage is for a table in a database'),
    ({'preTargetAdhocQueries': ['select 1'], 'postTargetAdhocQueries': ['select 1']},
     'preTargetAdhocQueries and postTargetAdhocQueries are for a table in a database'),
    ({'insertStrategy': 'append', 'singleFile': True}, 'singleFile needs insertStrategy: overwrite'),
    ({'insertStrategy': 'overwrite', 'sourceQuery': 'select * from t where u > {{ watermark }}', 'watermarkColumn': 'u',
      'watermarkInitial': 0}, 'watermarkColumn requires insertStrategy: upsert, or append for a file target -- overwrite'),
    ({'targetColumnTypes': {'balance': 'money'}}, "balance: 'money' is not a column type"),
    ({'targetColumnTypes': {'balance': 'decimal(40,2)'}}, 'decimal precision must be from 1 to 38'),
    ({'targetColumnTypes': {'balance': 'decimal(2,4)'}}, 'scale 4 is larger than its precision 2'),
    ({'targetColumnTypes': {'Balance': 'int64', 'BALANCE': 'int32'}}, 'differs only in case'),
    ])
def test_a_file_job_refuses_settings_that_would_be_ignored_or_are_wrong(job, message):
    with pytest.raises(ConfigurationError, match=message):
        Configuration.validateJobConfiguration({'jobs': {'j': _job(**job)}}, DataJobsFile)


@pytest.mark.parametrize('job', [{'targetColumnTypes': {'id': 'int64'}}, {'singleFile': True}])
def test_file_settings_on_a_database_job_are_refused(job):
    with pytest.raises(ConfigurationError, match='for a file target'):
        Configuration.validateJobConfiguration({'jobs': {'j': _job(insertStrategy='upsert', targetTableFinal='customers', **job)}},
                                               DataJobsFile)


@pytest.mark.parametrize('path, message', [
    ('/abs/customers', 'not an absolute one'),
    ('../customers', 'without empty, . or .. parts'),
    ('crm//customers', 'without empty, . or .. parts'),
    ('_private/customers', 'which engines reading a directory tree skip as hidden'),
    ('crm/.customers', 'skip as hidden'),
    ])
def test_a_file_tables_path_must_stay_under_the_root_and_be_visible(path, message):
    """A table named _x would be skipped by an engine discovering the tree,
    as bauta's own staging is meant to be.
    """
    with pytest.raises(ConfigurationError, match=message):
        Configuration.validateJobConfiguration({'jobs': {'j': _job(targetTableFinal=path)}}, DataJobsFile)


def test_a_files_connection_that_requires_masking_refuses_an_unmasked_job():
    with pytest.raises(ConfigurationError, match='requireMasking'):
        _validate(connections=_connections(requireMasking=True))


@pytest.mark.parametrize('text, parsed', [
    ('INT64', ('int64', None, None)), ('text', ('string', None, None)), ('double', ('float64', None, None)),
    ('decimal( 18 , 2 )', ('decimal', 18, 2)), ('timestamptz', ('timestamptz', None, None)),
    ])
def test_column_types_are_read_in_any_case_and_by_their_other_names(text, parsed):
    assert tuple(parseColumnType(text)) == parsed


def test_a_files_connections_default_sizes_are_byte_counts():
    """pydantic doesn't validate a default, so a default written as text
    stayed text, and comparing a part's size with it failed at the first write.
    """
    settings = connectionConfig(type='files', root='lake')

    assert (settings.fileSize, settings.rowGroupSize) == (256 * 10 ** 6, 128 * 10 ** 6)

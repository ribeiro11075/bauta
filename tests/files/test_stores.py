"""What each store holds a files connection to, by the pyarrow installed:
settings that need a newer one are refused naming the version, rather than
failing inside pyarrow.
"""
import pytest

pytest.importorskip('pyarrow', reason='a files connection writes with pyarrow (pip install -e ".[files]")')

from bauta.configuration import ConfigurationError, connectionConfig  # noqa: E402
from bauta.files import FileTarget  # noqa: E402
from bauta.files import stores  # noqa: E402
from tests.jobConfigs import dataJob  # noqa: E402

AZURE = {'type': 'files', 'root': 'az://container/lake', 'accountName': 'acct', 'endpoint': 'http://127.0.0.1:10000'}


def _target(**settings):
    return FileTarget('j', dataJob(sourceConnection='prod', targetConnection='lake', targetTableFinal='t', insertStrategy='overwrite'),
                      connectionConfig(**{**AZURE, **settings}))


@pytest.mark.parametrize('setting, version', [({'sasToken': 't'}, '20.0'), ({'clientId': 'i', 'clientSecret': 's', 'tenantId': 't'}, '21.0')])
def test_an_azure_setting_older_pyarrow_lacks_is_refused_naming_the_version(monkeypatch, setting, version):
    monkeypatch.setattr(stores, 'pyarrowVersion', lambda: (18, 0))

    with pytest.raises(ConfigurationError, match='needs pyarrow {} or newer, and 18.0 is installed'.format(version)):
        _target(**setting)


def test_azure_before_pyarrow_19_holds_a_part_to_what_one_copy_takes(monkeypatch):
    """pyarrow 18 copies a blob with the request that takes 256 MiB at most,
    and a part is published by copying it.
    """
    monkeypatch.setattr(stores, 'pyarrowVersion', lambda: (18, 0))

    with pytest.raises(ConfigurationError, match='fileSize is past 256 MiB'):
        _target(accountKey='k', fileSize='300MiB')

    assert _target(accountKey='k', fileSize='256MiB').store.largestMove() == 256 * 2 ** 20


def test_azure_from_pyarrow_19_copies_any_size(monkeypatch):
    monkeypatch.setattr(stores, 'pyarrowVersion', lambda: (19, 0))

    assert _target(accountKey='k', fileSize='4GiB').store.largestMove() is None


@pytest.mark.parametrize('root, settings, location', [
    ('s3://bucket/lake', {'region': 'us-east-1', 'endpoint': 'http://127.0.0.1:5055'}, 's3://bucket/lake/crm/t/'),
    ('gs://bucket/lake', {'anonymous': True, 'endpoint': 'http://127.0.0.1:4443'}, 'gs://bucket/lake/crm/t/'),
    ('abfss://c@acct.dfs.core.windows.net/lake', {'accountKey': 'k', 'endpoint': 'http://127.0.0.1:10000'},
     'abfss://c@acct.dfs.core.windows.net/lake/crm/t/'),
    ])
def test_a_tables_location_is_spelled_as_its_root_was_written(root, settings, location):
    target = FileTarget('j', dataJob(sourceConnection='prod', targetConnection='lake', targetTableFinal='crm/t', insertStrategy='overwrite'),
                        connectionConfig(type='files', root=root, **settings))

    assert target.loadName == location

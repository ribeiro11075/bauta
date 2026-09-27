"""How a table's location is spelled in each store."""
import pytest

pytest.importorskip('pyarrow', reason='a files connection writes with pyarrow (pip install -e ".[files]")')

from bauta.configuration import connectionConfig  # noqa: E402
from bauta.lake import FileTarget  # noqa: E402
from tests.jobConfigs import dataJob  # noqa: E402

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

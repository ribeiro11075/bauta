"""Copies from each database into Parquet, read back with pyarrow: that each
driver's values arrive exact, and that a decimal takes the precision and
scale the driver reports where it reports them.

SQLite and DuckDB need no server, so their runs are part of the default
`pytest`; the five servers' are marked `integration`.
"""
import datetime
import decimal
import uuid

import pytest

from bauta.configuration import connectionConfig
from bauta.database import Database
from bauta.jobs.pipeline import _executeDataJob
from tests.integration.servers import EMBEDDED, SERVERS, serverSettings
from tests.jobConfigs import dataJob

pytest.importorskip('pyarrow', reason='a files connection writes with pyarrow (pip install -e ".[files]")')

import pyarrow  # noqa: E402
import pyarrow.dataset  # noqa: E402

DATABASES = [pytest.param(name) for name in EMBEDDED] + [pytest.param(name, marks=pytest.mark.integration) for name in sorted(SERVERS)]

# A timestamp with microseconds, spelled for each database's DDL.
TIMESTAMP = {'mysql': 'DATETIME(6)', 'mariadb': 'DATETIME(6)', 'mssql': 'DATETIME2', 'sqlite': 'TEXT'}

# The databases whose driver reports a decimal's precision and scale.
REPORTS_DECIMALS = {'postgresql', 'oracle', 'duckdb'}

AT = datetime.datetime(2026, 1, 2, 3, 4, 5, 123456)


def _copy(tmp_path, source, query, **job):
    settings = {'source': source, 'lake': connectionConfig(type='files', root=str(tmp_path / 'lake'))}
    _executeDataJob('j', dataJob(sourceConnection='source', targetConnection='lake', sourceQuery=query, targetTableFinal='t',
                                 insertStrategy='overwrite', unmasked=True, **job), settings)

    snapshot, = (tmp_path / 'lake' / 't').glob('snapshot=*')
    return pyarrow.dataset.dataset(str(snapshot), format='parquet').to_table()


@pytest.mark.parametrize('databaseName', DATABASES)
def test_each_databases_decimals_and_times_arrive_exact(databaseName, tmp_path):
    settings = serverSettings(databaseName, tmp_path)
    table = 'files_{}'.format(uuid.uuid4().hex[:8])

    with Database(connectionSettings=settings) as database:
        database.alter('CREATE TABLE {} (id INT PRIMARY KEY, amount DECIMAL(12,2), name VARCHAR(50), stamp {})'.format(
            table, TIMESTAMP.get(databaseName, 'TIMESTAMP')))
        database.insert(table, [(1, decimal.Decimal('12.30'), 'ann', AT), (2, None, None, None)], columns=['id', 'amount', 'name', 'stamp'])

    try:
        copied = _copy(tmp_path, settings, 'SELECT id, amount, name, stamp FROM {} ORDER BY id'.format(table))
    finally:
        with Database(connectionSettings=settings) as database:
            database.alter('DROP TABLE {}'.format(table))

    amount = copied.schema.field(1).type
    rows = [list(row.values()) for row in copied.to_pylist()]

    if databaseName == 'sqlite':
        # A DECIMAL column has NUMERIC affinity there: 12.30 is stored, and
        # returned, as the float 12.3.
        assert (amount, rows[0][:3]) == (pyarrow.float64(), [1, 12.3, 'ann'])
    else:
        assert pyarrow.types.is_decimal(amount)
        assert (amount.precision, amount.scale) == ((12, 2) if databaseName in REPORTS_DECIMALS else (38, 10))
        assert rows[0][:3] == [1, decimal.Decimal('12.30'), 'ann']
    assert rows[1] == [2, None, None, None]
    # SQLite holds a timestamp as the text it was given.
    assert rows[0][3] == (str(AT) if databaseName == 'sqlite' else AT)


@pytest.mark.integration
def test_postgresqls_own_types_arrive_as_their_text_or_their_instant(tmp_path):
    settings = serverSettings('postgresql', tmp_path)

    copied = _copy(tmp_path, settings, """
        SELECT 42::bigint AS big, 1.5::numeric AS bare, 1.250::numeric(10,3) AS exact,
               '2026-01-02 03:04:05+02'::timestamptz AS instant, '{"a": [1, 2]}'::jsonb AS doc,
               'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::uuid AS id, interval '30 hours' AS span
        """)

    assert [str(field.type) for field in copied.schema] == ['int64', 'decimal128(38, 10)', 'decimal128(10, 3)', 'timestamp[us, tz=UTC]',
                                                            'string', 'string', 'string']
    assert copied.to_pylist() == [{'big': 42, 'bare': decimal.Decimal('1.5000000000'), 'exact': decimal.Decimal('1.250'),
                                   'instant': datetime.datetime(2026, 1, 2, 1, 4, 5, tzinfo=datetime.timezone.utc),
                                   'doc': '{"a": [1, 2]}', 'id': 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', 'span': '30:00:00'}]

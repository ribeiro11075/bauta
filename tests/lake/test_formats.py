"""CSV and JSON Lines as a files connection writes them, read back the way a
reader that isn't pyarrow would: Python's own csv, json and gzip.
"""
import base64
import csv
import datetime
import decimal
import gzip
import io
import json

import pytest

pytest.importorskip('pyarrow', reason='a files connection writes with pyarrow (pip install -e ".[files]")')

from bauta.configuration import connectionConfig  # noqa: E402
from bauta.lake import FileTarget  # noqa: E402
from tests.jobConfigs import dataJob  # noqa: E402

UTC = datetime.timezone.utc

COLUMNS = ['id', 'name', 'amount', 'ratio', 'at', 'instant', 'on', 'raw', 'doc', 'flag']
ROWS = [
    (1, 'a,"quoted"', decimal.Decimal('12.30'), 1.5, datetime.datetime(2026, 1, 2, 3, 4, 5, 6),
     datetime.datetime(2026, 1, 2, 5, 4, 5, tzinfo=datetime.timezone(datetime.timedelta(hours=2))), datetime.date(2026, 1, 2), b'\x00\xff',
     {'tags': ['x'], 'n': 1}, True),
    (2, '', None, float('nan'), None, None, None, None, None, None),
    ]


# What a driver reports of each column: amount as DECIMAL(12,2).
DESCRIPTION = [(name, None, None, None, 12, 2, None) if name == 'amount' else (name,) for name in COLUMNS]


def _write(tmp_path, rows=ROWS, chunk=100, **settings):
    """Writes `rows` a chunk at a time, as a job does."""
    target = FileTarget('j', dataJob(sourceConnection='prod', targetConnection='lake', targetTableFinal='t', insertStrategy='overwrite'),
                        connectionConfig(type='files', root=str(tmp_path / 'lake'), **settings))
    target.begin(COLUMNS, DESCRIPTION)
    for start in range(0, len(rows), chunk):
        target.write(list(rows[start:start + chunk]))
    target.finish(len(rows))

    snapshot, = (tmp_path / 'lake' / 't').glob('snapshot=*')
    return sorted(snapshot.glob('part-*'))


def _text(path):
    raw = path.read_bytes()
    return (gzip.decompress(raw) if path.name.endswith('.gz') else raw).decode('utf-8')


def test_csv_tells_null_from_empty_text_and_spells_each_type_one_way(tmp_path):
    part, = _write(tmp_path, format='csv')

    header, first, second = list(csv.reader(io.StringIO(_text(part))))
    raw = part.read_bytes()

    assert part.name.endswith('.csv.gz')
    assert header == COLUMNS
    assert first == ['1', 'a,"quoted"', '12.30', '1.5', '2026-01-02 03:04:05.000006', '2026-01-02 03:04:05.000000Z', '2026-01-02',
                     base64.b64encode(b'\x00\xff').decode(), '{"tags": ["x"], "n": 1}', 'true']
    # An empty string is quoted, a null is not: csv.reader reads both as '',
    # so the difference is checked in the text itself.
    assert gzip.decompress(raw).decode().splitlines()[2].startswith('2,"",,nan,,')
    assert second[:2] == ['2', '']


def test_csv_takes_its_delimiter_and_can_go_uncompressed(tmp_path):
    part, = _write(tmp_path, format='csv', delimiter=';', compression='none')

    assert part.name.endswith('.csv')
    assert part.read_text().splitlines()[0] == '"id";"name";"amount";"ratio";"at";"instant";"on";"raw";"doc";"flag"'


def test_json_lines_nest_documents_and_keep_decimals_exact(tmp_path):
    part, = _write(tmp_path, format='ndjson')

    first, second = [json.loads(line) for line in _text(part).splitlines()]

    assert part.name.endswith('.ndjson.gz')
    assert first == {'id': 1, 'name': 'a,"quoted"', 'amount': '12.30', 'ratio': 1.5, 'at': '2026-01-02 03:04:05.000006',
                     'instant': '2026-01-02 03:04:05.000000Z', 'on': '2026-01-02', 'raw': base64.b64encode(b'\x00\xff').decode(),
                     'doc': {'tags': ['x'], 'n': 1}, 'flag': True}
    # JSON has no NaN: it is a string, as BigQuery and Snowflake read it.
    assert second == {'id': 2, 'name': '', 'amount': None, 'ratio': 'NaN', 'at': None, 'instant': None, 'on': None, 'raw': None,
                      'doc': None, 'flag': None}


def test_json_lines_write_text_that_only_looks_like_a_document_as_text(tmp_path):
    rows = [(1, 'x', None, None, None, None, None, None, {'a': 1}, None),
            (2, 'y', None, None, None, None, None, None, '{not json', None)]

    part, = _write(tmp_path, rows=rows, format='ndjson', compression='none')

    assert [json.loads(line)['doc'] for line in part.read_text().splitlines()] == [{'a': 1}, '{not json']


@pytest.mark.parametrize('format', ['csv', 'ndjson'])
def test_every_text_part_reads_on_its_own(tmp_path, format):
    """A part is closed at fileSize and the next begun, each with its own
    header and whole lines, so a reader given any one of them can read it.
    Uncompressed, since gzip holds back tens of kilobytes before writing any.
    """
    rows = [(index, 'name{}'.format(index), None, None, None, None, None, None, None, None) for index in range(2000)]

    parts = _write(tmp_path, rows=rows, format=format, compression='none', fileSize='2KB', rowGroupSize='1KB')

    assert len(parts) > 1
    if format == 'csv':
        assert all(_text(part).startswith('"id","name"') for part in parts)
        assert sum(len(_text(part).splitlines()) - 1 for part in parts) == 2000
    else:
        assert sum(len([json.loads(line) for line in _text(part).splitlines()]) for part in parts) == 2000


def test_parquet_takes_another_codec(tmp_path):
    import pyarrow.parquet

    part, = _write(tmp_path, compression='snappy')

    assert pyarrow.parquet.read_metadata(str(part)).row_group(0).column(0).compression == 'SNAPPY'

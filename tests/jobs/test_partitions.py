"""`partitions`: a job read, masked and written as several slices at once.

The slicing itself is pure and tested as such; the rest runs against real
SQLite files, which need no server, as test_masking_end_to_end.py does. What
every database does with the predicates is in
tests/integration/test_integration_databases.py.
"""
import contextlib
import decimal
import sqlite3
import threading

import pytest

from bauta.configuration import Configuration, ConfigurationError, connectionConfig, DatabaseType, DataJobConfig, DataJobsFile
from bauta.database import Database
from bauta.jobs import targets
from bauta.jobs.memory import FileMemory
from bauta.jobs.partitions import boundsQuery, integerBound, resolveColumn, slicePredicates, splitPoints, wrappedQuery
from bauta.jobs.pipeline import _executeDataJob, _preparer
from bauta.jobs.runner import runDataJobs
from bauta.transform import Transform

KEY = 'a-partitioned-masking-test-key'


def test_split_points_divide_the_range_as_evenly_as_integers_can():
    assert splitPoints(1, 100, 4) == [26, 51, 76]
    assert splitPoints(0, 9, 4) == [2, 5, 7]
    assert splitPoints(-50, 49, 2) == [0]


def test_split_points_are_fewer_where_there_are_fewer_values_than_slices():
    assert splitPoints(5, 5, 8) == []
    assert splitPoints(5, 7, 8) == [6, 7]


def test_every_value_falls_in_exactly_one_slice():
    """Asked of SQL itself, beyond both bounds and between integers."""
    values = [None, -100.5, -4, -3, 0, 2.5, 7, 39.99, 40, 41, 10 ** 12]
    predicates = slicePredicates('"id"', splitPoints(-3, 40, 6))

    with contextlib.closing(sqlite3.connect(':memory:')) as connection:
        connection.execute('CREATE TABLE t (id NUMERIC)')
        connection.executemany('INSERT INTO t VALUES (?)', [(value,) for value in values])
        read = [row for predicate in predicates for row in connection.execute(wrappedQuery('SELECT id FROM t', predicate))]

    assert len(predicates) == 6
    assert sorted(read, key=lambda row: (row[0] is not None, row[0] or 0)) == [(value,) for value in values]


def test_the_first_slice_takes_the_nulls_and_the_last_is_open_ended():
    assert slicePredicates('"id"', [10, 20]) == ['("id" < 10 OR "id" IS NULL)', '"id" >= 10 AND "id" < 20', '"id" >= 20']


def test_one_slice_reads_the_query_as_it_is():
    assert slicePredicates('"id"', []) == [None]


def test_the_query_is_wrapped_so_a_predicate_applies_to_whatever_it_returns():
    assert wrappedQuery('select id from t;  ') == 'SELECT * FROM (select id from t) bauta_partition'
    assert wrappedQuery('select id from t', '"id" >= 3') == 'SELECT * FROM (select id from t) bauta_partition WHERE "id" >= 3'
    assert boundsQuery('select id from t;', '"id"') == 'SELECT MIN("id"), MAX("id") FROM (select id from t) bauta_partition'


def test_the_column_is_found_as_the_query_spells_it():
    assert resolveColumn('id', ['ID', 'NAME']) == 'ID'
    assert resolveColumn('Id', ['id', 'Id']) == 'Id'

    with pytest.raises(ConfigurationError, match='"code" is not among the columns sourceQuery returns'):
        resolveColumn('code', ['id', 'name'])


@pytest.mark.parametrize('value, bound', [(None, None), (7, 7), (-7, -7), (decimal.Decimal('12'), 12), (decimal.Decimal('2.5'), 2),
                                          (3.9, 3), (-0.5, -1)])
def test_a_bound_is_an_integer_to_slice_at(value, bound):
    assert integerBound(value, 'id') == bound


@pytest.mark.parametrize('value', ['secret@example.com', b'raw', True, float('nan'), decimal.Decimal('Infinity')])
def test_a_bound_that_is_no_number_is_refused_by_its_type_never_its_value(value):
    with pytest.raises(ConfigurationError) as error:
        integerBound(value, 'email')

    assert 'email' in str(error.value)
    assert str(value) not in str(error.value).replace('"email"', '')


class _RecordingMasking:

    def __init__(self):
        self.chunkIndexes = []

    def apply(self, rows, chunkIndex=None):
        self.chunkIndexes.append(chunkIndex)
        return rows


def test_each_partition_numbers_its_chunks_apart_from_the_others_for_shuffle():
    """`shuffle` seeds each chunk's permutation with its position, so two
    chunks with the same position would be shuffled alike.
    """
    seen = []
    for partition in range(3):
        masking = _RecordingMasking()
        prepare = _preparer(Transform(columns=['id'], columnTransforms={}), masking, partition, 3)
        for chunkIndex in range(4):
            prepare(chunkIndex, [(1,)])
        seen.extend(masking.chunkIndexes)

    assert sorted(seen) == list(range(12))


# Ids spread unevenly, a few groups of them, and a nullable column of others.
ROWS = [(index * index % 997 + index * 13, 'person{}@corp.example'.format(index), '+1 555 01{:05d}'.format(index),
         None if index % 9 == 0 else index * 3 - 40) for index in range(1, 401)]

POLICY = {'id': {'strategy': 'key', 'domain': 'customer'}, 'email': 'email', 'phone': 'digits', 'ref': 'keep'}


@pytest.fixture
def connections(tmp_path):
    settings = {alias: connectionConfig(type=DatabaseType.SQLITE, path=str(tmp_path / '{}.db'.format(alias))) for alias in ('prod', 'copy')}

    with Database(connectionSettings=settings['prod'], create=True) as database:
        database.alter('CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT, phone TEXT, ref INTEGER)')
        database.insert(table='customers', data=ROWS, chunkSize=500)

    with Database(connectionSettings=settings['copy'], create=True) as database:
        for table in ('customers', 'customers_stage', 'customers_whole', 'customers_whole_stage'):
            database.alter('CREATE TABLE {} (id INTEGER PRIMARY KEY, email TEXT, phone TEXT, ref INTEGER)'.format(table))

    return settings


def _job(**overrides):
    fields = dict(active=True, sourceConnection='prod', sourceQuery='SELECT id, email, phone, ref FROM customers', targetConnection='copy',
                  targetTableFinal='customers', targetTableStage='customers_stage', insertStrategy='swap', chunkSize=17,
                  masking={'key': KEY, 'columns': POLICY}, partitions={'column': 'id', 'count': 4})
    fields.update(overrides)

    return DataJobConfig(**{name: value for name, value in fields.items() if value is not None})


def _rows(settings, query):
    with Database(connectionSettings=settings) as database:
        return database.query(query)


def test_a_partitioned_job_masks_byte_for_byte_as_one_stream_does(connections, caplog):
    """Masking keys on each value, not on the row's position or the chunk it
    arrived in, so how the rows were divided changes nothing in the copy.
    """
    whole = _executeDataJob('whole', _job(partitions=None, targetTableFinal='customers_whole', targetTableStage='customers_whole_stage'),
                            connections)
    with caplog.at_level('INFO', logger='bauta'):
        partitioned = _executeDataJob('partitioned', _job(), connections)

    assert 'as 4 partition(s) of id' in caplog.text
    assert caplog.text.count('Partition ') == 4
    assert whole.rowCount == partitioned.rowCount == len(ROWS)
    assert partitioned.masking == whole.masking
    copied = _rows(connections['copy'], 'SELECT id, email, phone, ref FROM customers ORDER BY id')
    assert copied == _rows(connections['copy'], 'SELECT id, email, phone, ref FROM customers_whole ORDER BY id')
    assert not {row[1] for row in copied} & {row[1] for row in ROWS}


def test_a_partition_column_with_nulls_loads_every_row_once(connections):
    outcome = _executeDataJob('byRef', _job(partitions={'column': 'REF', 'count': 5}), connections)

    assert outcome.rowCount == len(ROWS)
    assert _rows(connections['copy'], 'SELECT count(*), count(DISTINCT id), count(ref) FROM customers') == [
        (len(ROWS), len(ROWS), sum(1 for row in ROWS if row[3] is not None))]


def test_a_stage_less_upsert_loads_each_partition_straight_into_the_target(connections):
    outcome = _executeDataJob('upsert', _job(insertStrategy='upsert', targetTableStage=None, masking=None), connections)

    assert outcome.rowCount == len(ROWS)
    assert _rows(connections['copy'], 'SELECT id, email, phone, ref FROM customers ORDER BY id') == sorted(ROWS)


def test_an_empty_source_is_one_partition_reading_nothing(connections):
    outcome = _executeDataJob('empty', _job(sourceQuery='SELECT id, email, phone, ref FROM customers WHERE id < 0'), connections)

    assert outcome.rowCount == 0
    assert _rows(connections['copy'], 'SELECT count(*) FROM customers') == [(0,)]


def test_the_watermark_is_the_highest_over_every_partition(connections):
    job = _job(insertStrategy='upsert', targetTableStage=None, masking=None, watermarkColumn='id', watermarkInitial=-1,
               sourceQuery='SELECT id, email, phone, ref FROM customers WHERE id > {{ watermark }}')

    outcome = _executeDataJob('incremental', job, connections, watermark=-1)

    assert outcome.watermark == max(row[0] for row in ROWS)
    assert outcome.rowCount == len(ROWS)

    again = _executeDataJob('incremental', job, connections, watermark=outcome.watermark)
    assert (again.rowCount, again.watermark) == (0, None)


def test_a_failed_partition_fails_the_job_before_the_swap_and_stops_the_others(connections, monkeypatch):
    with Database(connectionSettings=connections['copy']) as database:
        database.insert(table='customers', data=[(1, 'kept', 'kept', 1)], chunkSize=10)

    write = targets.ChunkWriter.write
    failing = threading.Event()

    def failOnce(self, rows):
        if not failing.is_set():
            failing.set()
            raise RuntimeError('the network went away')
        return write(self, rows)

    monkeypatch.setattr(targets.ChunkWriter, 'write', failOnce)

    with pytest.raises(RuntimeError, match='the network went away'):
        _executeDataJob('failing', _job(chunkSize=5), connections)

    # Not swapped: the target holds what it held, and the stage less than all.
    assert _rows(connections['copy'], 'SELECT * FROM customers') == [(1, 'kept', 'kept', 1)]
    assert _rows(connections['copy'], 'SELECT count(*) FROM customers_stage')[0][0] < len(ROWS)


def test_a_partition_column_the_query_does_not_return_fails_before_the_target_is_touched(connections):
    with Database(connectionSettings=connections['copy']) as database:
        database.insert(table='customers_stage', data=[(1, 'kept', 'kept', 1)], chunkSize=10)

    with pytest.raises(ConfigurationError, match='partitions.column "code" is not among the columns sourceQuery returns'):
        _executeDataJob('missing', _job(partitions={'column': 'code', 'count': 2}), connections)

    assert _rows(connections['copy'], 'SELECT count(*) FROM customers_stage') == [(1,)]


def test_a_text_partition_column_is_refused_naming_its_type_and_no_value(connections):
    with pytest.raises(ConfigurationError) as error:
        _executeDataJob('text', _job(partitions={'column': 'email', 'count': 2}), connections)

    assert 'holds str values' in str(error.value)
    assert '@' not in str(error.value)


def test_partitions_run_through_a_whole_run_and_record_one_watermark(connections, tmp_path):
    raw = {'workers': 1, 'jobs': {'copyCustomers': {
        'active': True, 'sourceConnection': 'prod', 'targetConnection': 'copy', 'insertStrategy': 'upsert', 'chunkSize': 25,
        'targetTableFinal': 'customers', 'targetTableStage': 'customers_stage', 'watermarkColumn': 'id', 'watermarkInitial': -1,
        'sourceQuery': 'SELECT id, email, phone, ref FROM customers WHERE id > {{ watermark }}',
        'masking': {'key': KEY, 'columns': {**POLICY, 'id': 'keep'}}, 'partitions': {'column': 'id', 'count': 3},
        }}}
    jobsFile = Configuration.validateJobConfiguration(raw, DataJobsFile)
    Configuration.validateJobGraph(jobsFile.jobs, connections=connections)
    memory = FileMemory(memoryFile=tmp_path / 'memory.yaml')

    result = runDataJobs(jobsFile=jobsFile, connectionConfiguration=connections, memory=memory, logFile=tmp_path / 'run.log')

    assert result.succeeded, result.outcomes
    assert result.rowCount == len(ROWS)
    assert memory.readWatermarks() == {'copyCustomers': max(row[0] for row in ROWS)}
    assert 'as 3 partition(s) of id' in (tmp_path / 'run.log').read_text()
    assert _rows(connections['copy'], 'SELECT count(DISTINCT email) FROM customers') == [(len(ROWS),)]

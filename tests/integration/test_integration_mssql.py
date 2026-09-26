"""What only SQL Server does, against a real server: rowversion watermarks,
and its multi-row VALUES loads, where one type per column is chosen for the
whole list, so a chunk mixing types, or a decimal spelled with an exponent,
must go row by row instead, and times must keep their microseconds. What every
database does is in test_integration_databases.py.

Needs the `mssql` service from docker-compose.yml and pymssql. The tests use
the always-present `master` database, since the official image has no setting
to create another, and every table is uniquely named. Run with
`pytest -m integration`; skipped with the reason if either is missing.
"""
import datetime
import decimal
import uuid

import pytest

pytest.importorskip('pymssql', reason='pymssql is not installed (pip install -e ".[mssql]")')

from bauta.jobs.memory import DatabaseMemory
from bauta.configuration import Configuration, DataJobsFile
from bauta.jobs.runner import runDataJobs

pytestmark = pytest.mark.integration

DATABASE = 'mssql'


def test_a_rowversion_watermark_survives_database_backed_memory(connectionSettings, liveDatabase, memoryTable, tmp_path):
    """rowversion is how SQL Server tracks changes, and arrives as bytes. Stored
    as text, the second run compared binary with a string and failed.
    """
    suffix = uuid.uuid4().hex[:8]
    source, target = 'rv_source_{}'.format(suffix), 'rv_target_{}'.format(suffix)
    liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, name NVARCHAR(20), rv ROWVERSION)'.format(source))
    liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, name NVARCHAR(20), source_version VARBINARY(8))'.format(target))
    try:
        liveDatabase.alter("INSERT INTO {} (id, name) VALUES (1, 'a'), (2, 'b')".format(source))
        jobsFile = Configuration.validateJobConfiguration({'workers': 1, 'jobs': {'job1': {
            'active': True, 'sourceConnection': 'db', 'targetConnection': 'db', 'insertStrategy': 'upsert', 'chunkSize': 10,
            'sourceQuery': 'SELECT id, name, rv FROM {} WHERE rv > {{{{ watermark }}}}'.format(source), 'watermarkColumn': 'rv',
            'watermarkInitial': 0, 'targetTableFinal': target,
            }}}, DataJobsFile)
        with DatabaseMemory(connectionSettings=connectionSettings, table=memoryTable) as memory:
            def run():
                result = runDataJobs(jobsFile=jobsFile, connectionConfiguration={'db': connectionSettings}, logFile=tmp_path / 'runner.log',
                                     memory=memory, runForever=False)
                (outcome,) = result.outcomes
                assert outcome.error is None
                return outcome.rowCount

            assert run() == 2
            assert isinstance(memory.readWatermarks()['job1'], bytes)
            liveDatabase.alter("UPDATE {} SET name = 'B' WHERE id = 2".format(source))
            assert run() == 1
            assert liveDatabase.query('SELECT id, name FROM {} ORDER BY id'.format(target)) == [(1, 'a'), (2, 'B')]
            assert memory.readWatermarks()['job1'] == liveDatabase.query('SELECT MAX(rv) FROM {}'.format(source))[0][0]
    finally:
        for table in (source, target):
            liveDatabase.alter('DROP TABLE {}'.format(table))


def test_bulk_loads_round_trip_awkward_values(liveDatabase):
    """Multi-row statements carry values quoted into the SQL by pymssql: quotes,
    percent signs, format markers, Unicode and bytes must all arrive intact.
    """
    import datetime
    import decimal

    table = 'bulk_{}'.format(uuid.uuid4().hex[:8])
    liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, t NVARCHAR(100), n DECIMAL(12,3), d DATETIME2, b VARBINARY(10))'.format(table))
    try:
        rows = [(1, "it's 100% ünï %s %(x)s", decimal.Decimal('1.250'), datetime.datetime(2026, 1, 2, 3, 4, 5), b'\x00\xff'),
                (2, None, None, None, None)]
        liveDatabase.insert(table=table, data=rows)
        assert liveDatabase.query('SELECT * FROM {} ORDER BY id'.format(table)) == rows

        liveDatabase.upsert(table=table, data=[(1, 'first', None, None, None), (3, 'new', None, None, None), (1, 'last', None, None, None)])
        assert liveDatabase.query('SELECT id, t FROM {} ORDER BY id'.format(table)) == [(1, 'last'), (2, None), (3, 'new')]

        many = [(index, 'n{}'.format(index), None, None, None) for index in range(10, 2510)]
        liveDatabase.insert(table=table, data=many, chunkSize=2500)
        assert liveDatabase.query('SELECT count(*) FROM {}'.format(table)) == [(2503,)]
    finally:
        liveDatabase.alter('DROP TABLE {}'.format(table))


@pytest.fixture
def oddTable(liveDatabase):
    name = 't_{}'.format(uuid.uuid4().hex[:8])
    liveDatabase.alter('CREATE TABLE {} (id INT NOT NULL PRIMARY KEY, code NVARCHAR(50), exact DECIMAL(38,10), '
                       'moment DATETIME2(6), clock TIME(6))'.format(name))

    yield name

    liveDatabase.alter('DROP TABLE {}'.format(name))


def test_a_chunk_mixing_text_and_numbers_keeps_the_text(liveDatabase, oddTable):
    """One VALUES list takes one type per column, so an integer beside '00001'
    used to store it as '1'. Such a chunk goes row by row instead.
    """
    liveDatabase.insert(table=oddTable, data=[(1, '00001', None, None, None), (2, 0, None, None, None)], chunkSize=10)

    assert [row[0] for row in liveDatabase.query('SELECT code FROM {} ORDER BY id'.format(oddTable))] == ['00001', '0']


def test_a_decimal_spelled_with_an_exponent_does_not_round_its_neighbours(liveDatabase, oddTable):
    """'1E-10' in a VALUES list types the column float, which rounded the
    28-digit value beside it.
    """
    big, small = decimal.Decimal('9999999999999999999999999999.9999999999'), decimal.Decimal('0.0000000001')
    liveDatabase.insert(table=oddTable, data=[(1, None, big, None, None), (2, None, small, None, None)], chunkSize=10)

    assert [row[0] for row in liveDatabase.query('SELECT exact FROM {} ORDER BY id'.format(oddTable))] == [big, small]


def test_microseconds_survive_an_insert_and_an_upsert(liveDatabase, oddTable):
    """pymssql renders a bound datetime with milliseconds only; ISO text converts exactly."""
    moment, clock = datetime.datetime(2026, 1, 1, 10, 0, 7, 123456), datetime.time(23, 59, 59, 999999)
    liveDatabase.insert(table=oddTable, data=[(1, None, None, moment, clock)], chunkSize=10)
    liveDatabase.upsert(table=oddTable, data=[(2, None, None, moment, clock)], chunkSize=10)

    assert liveDatabase.query('SELECT moment, clock FROM {} ORDER BY id'.format(oddTable)) == [(moment, clock), (moment, clock)]

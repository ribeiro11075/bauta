"""What only Oracle does, against a real server: NUMBER columns with a scale,
fetched as exact decimals, and a swap Oracle can't make atomic, since it commits
each rename on its own, undone when it stops part-way. What every database
does is in test_integration_databases.py.

Needs the `oracle` service from docker-compose.yml (gvenzl/oracle-free, with no
Oracle registry login) and oracledb in thin mode, with no Oracle client. Run
with `pytest -m integration`; skipped with the reason if either is missing.
"""
import decimal
import uuid

import pytest

pytest.importorskip('oracledb', reason='oracledb is not installed (pip install -e ".[oracle]")')

from bauta.database import Database

pytestmark = pytest.mark.integration

DATABASE = 'oracle'


def test_a_number_with_a_scale_is_fetched_exactly(liveDatabase):
    """oracledb's default is a float, which dropped digits a NUMBER holds and
    made large values unloadable anywhere, including back into Oracle.
    """
    table = 'num_{}'.format(uuid.uuid4().hex[:8])
    liveDatabase.alter('CREATE TABLE {} (id NUMBER(10), scaled NUMBER(19,4), wide NUMBER(38,10), plain NUMBER, '
                       'approx BINARY_DOUBLE)'.format(table))

    try:
        liveDatabase.alter('INSERT INTO {} VALUES (1, 123456789012345.6789, '
                           '9999999999999999999999999999.9999999999, 42.75, 1.5)'.format(table))

        (row,) = liveDatabase.query('SELECT id, scaled, wide, plain, approx FROM {}'.format(table))

        assert row == (1, decimal.Decimal('123456789012345.6789'), decimal.Decimal('9999999999999999999999999999.9999999999'),
                       decimal.Decimal('42.75'), 1.5)
        assert [type(value).__name__ for value in row] == ['int', 'Decimal', 'Decimal', 'Decimal', 'float']
    finally:
        liveDatabase.alter('DROP TABLE {}'.format(table))


def test_a_swap_stopped_part_way_leaves_both_tables_where_they_were(connectionSettings, liveDatabase, peopleTable):
    """Oracle commits every DDL statement, so there is no transaction to roll
    back. A rename blocked by another session -- ORA-00054 -- used to leave the
    stage table under the temporary name, so it was gone, and every run after
    that failed with ORA-00942 until someone renamed it back by hand.
    """
    stageTable = peopleTable + '_stage'
    liveDatabase.alter('CREATE TABLE {} (id INT PRIMARY KEY, name VARCHAR(50), amount INT)'.format(stageTable))
    liveDatabase.insert(table=peopleTable, data=[(1, 'old', 1)])
    liveDatabase.insert(table=stageTable, data=[(2, 'new', 2)])

    blocker = Database(connectionSettings=connectionSettings)
    try:
        # Held uncommitted, so the rename of the target cannot take its lock.
        blocker.cursor.execute('LOCK TABLE {} IN EXCLUSIVE MODE'.format(peopleTable))

        with pytest.raises(Exception, match='ORA-00054'):
            liveDatabase.swap(targetTable=peopleTable, stageTable=stageTable)
    finally:
        blocker.connection.rollback()
        blocker.close()

    assert liveDatabase.query('SELECT id FROM {}'.format(peopleTable)) == [(1,)]
    assert liveDatabase.query('SELECT id FROM {}'.format(stageTable)) == [(2,)]
    assert not liveDatabase.tableExists(peopleTable + '_tmp')

    # And the next run swaps, rather than failing on a stage table that is gone.
    liveDatabase.swap(targetTable=peopleTable, stageTable=stageTable)

    assert liveDatabase.query('SELECT id FROM {}'.format(peopleTable)) == [(2,)]
    liveDatabase.alter('DROP TABLE IF EXISTS {}'.format(stageTable))

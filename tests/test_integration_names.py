"""Tables whose names can only be written in quotes, against SQLite and the five servers.

A reserved word (`group`), a name whose case the database would fold, a column
called `select`: each of the six spells these differently, and a copy used to
fail on all of them -- the catalog lookups bound the quotes along with the
name, so the table looked like it had no columns and no primary key, and the
swap built a temporary name its own parser refused.

Servers that aren't reachable are skipped. Run with `pytest -m integration`.
"""
import importlib
import uuid

import pytest

from bauta.configuration import ConfigurationError, DatabaseConnectionConfig, DatabaseType
from bauta.database import Database
from bauta.databaseDialects import IDENTIFIER_LIMITS, quoteFoldedTable, quoteIdentifier
from servers import SERVERS

pytestmark = pytest.mark.integration

NAMES = ['sqlite'] + sorted(SERVERS)

# A reserved word and a space: neither can be written without quotes on any of
# the six. The space keeps one test's table apart from another's.
RESERVED = 'group'


@pytest.fixture(params=NAMES)
def database(request, tmp_path):
    if request.param == 'sqlite':
        settings = DatabaseConnectionConfig(type=DatabaseType.SQLITE, database=str(tmp_path / 'names.db'))
    else:
        driver, settings = SERVERS[request.param]
        try:
            importlib.import_module(driver)
            Database(connectionSettings=settings).close()
        except Exception as error:
            pytest.skip('{} is not available ({})'.format(request.param, error))

    with Database(connectionSettings=settings) as database:
        created = []
        database.created = created  # type: ignore[attr-defined]
        yield database
        for table in reversed(created):
            try:
                database.alter('DROP TABLE {}'.format(database.statementName(table)))
            except Exception:
                database.connection.rollback()


def _create(database, name):
    """A table named as a person would have to write it, with a reserved-word
    column, created so the plain name still means it.
    """

    table = '{} {}'.format(name, uuid.uuid4().hex[:6])
    text = 'VARCHAR(20)' if database.type != DatabaseType.ORACLE else 'VARCHAR2(20 CHAR)'
    database.alter('CREATE TABLE {} (id INT NOT NULL PRIMARY KEY, {} {})'.format(
        quoteFoldedTable(database.type, table), quoteIdentifier(database.type, 'select'), text))
    database.created.append(table)

    return table


def test_a_table_named_for_a_reserved_word_is_described_and_loaded(database):
    """Its primary key used to come back empty, so an upsert was refused with
    "has no primary key" although the table had one.
    """
    table = _create(database, RESERVED)

    assert [column.upper() for column in database.getPrimaryColumnNames(table)] == ['ID']
    assert [column.upper() for column in database.catalogColumns(table)] == ['ID', 'SELECT']
    assert database.tableExists(table)
    assert [definition.name.upper() for definition in database.getColumnDefinitions(table)] == ['ID', 'SELECT']


def test_rows_upsert_into_a_reserved_word_table_without_duplicating_the_key(database):
    table = _create(database, RESERVED)

    database.insert(table=table, data=[(1, 'first'), (2, 'second')])
    database.upsert(table=table, data=[(1, 'changed'), (3, 'third')])

    rows = database.query('SELECT id, {} FROM {} ORDER BY id'.format(
        quoteIdentifier(database.type, 'select'), database.statementName(table)))

    assert [(int(identifier), text) for identifier, text in rows] == [(1, 'changed'), (2, 'second'), (3, 'third')]


def test_a_reserved_word_table_is_swapped_with_its_stage_table(database):
    """The temporary name was built by adding `_tmp` outside the quotes, which
    SQL Server refused outright and the others took as a different table.
    """
    table = _create(database, RESERVED)
    stage = _create(database, RESERVED + ' stage')
    database.insert(table=table, data=[(1, 'old')])
    database.insert(table=stage, data=[(2, 'new')])

    database.swap(targetTable=table, stageTable=stage)

    final = database.query('SELECT id FROM {}'.format(database.statementName(table)))
    staged = database.query('SELECT id FROM {}'.format(database.statementName(stage)))

    assert ([int(row[0]) for row in final], [int(row[0]) for row in staged]) == ([2], [1])


def test_a_table_the_database_would_fold_is_found_when_it_is_named_in_quotes(database):
    """Quoting is the only way to name a lower-case table on Oracle, or a
    mixed-case one on PostgreSQL, and every lookup of one reported nothing.
    """
    if database.type not in (DatabaseType.ORACLE, DatabaseType.POSTGRESQL):
        pytest.skip('only Oracle and PostgreSQL fold an unquoted name')

    # A plain identifier, so writing it without quotes does mean a folded name.
    name = 'Folded{}'.format(uuid.uuid4().hex[:6])
    quoted = quoteIdentifier(database.type, name)
    database.alter('CREATE TABLE {} (id INT NOT NULL PRIMARY KEY)'.format(quoted))
    database.created.append(quoted)

    assert database.tableExists(quoted)
    assert [column.upper() for column in database.getPrimaryColumnNames(quoted)] == ['ID']
    # The same name without quotes means the folded one, which isn't there.
    assert not database.tableExists(name)


def test_a_name_the_database_would_cut_short_is_refused(database):
    """PostgreSQL keeps 63 bytes and says nothing, so two jobs whose targets
    differ only past that loaded the same table, and the second swap renamed
    over the first's rows while both jobs reported failure.
    """
    limit = IDENTIFIER_LIMITS.get(database.type)
    if limit is None:
        pytest.skip('SQLite has no name length limit')

    length, _ = limit
    tooLong = 'bauta_' + 'x' * length

    with pytest.raises(ConfigurationError, match='Load it into a shorter name'):
        database.tableExists(tooLong)
    with pytest.raises(ConfigurationError, match='keeps only {}'.format(length)):
        database.insert(table=tooLong, data=[(1,)])

    # A name exactly at the limit is kept whole, so it is accepted and works.
    atTheLimit = tooLong[:length]
    database.alter('CREATE TABLE {} (id INT NOT NULL PRIMARY KEY)'.format(quoteFoldedTable(database.type, atTheLimit)))
    database.created.append(atTheLimit)
    database.insert(table=atTheLimit, data=[(1,)])

    assert database.tableExists(atTheLimit)
    assert [int(row[0]) for row in database.query('SELECT id FROM {}'.format(database.statementName(atTheLimit)))] == [1]

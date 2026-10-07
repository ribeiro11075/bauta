from unittest.mock import MagicMock

import pytest

from bauta.configuration import ConfigurationError, connectionConfig, DatabaseType
from bauta.database import Database


def _mockedDatabase(dbType: DatabaseType) -> Database:

    if dbType in (DatabaseType.SQLITE, DatabaseType.DUCKDB):
        settings = connectionConfig(type=dbType, path=':memory:')
    elif dbType == DatabaseType.ORACLE:
        settings = connectionConfig(type=dbType, user='u', password='p', host='h', port=1234, serviceName='svc')
    else:
        settings = connectionConfig(type=dbType, user='u', password='p', database='d', host='h', port=1234)

    database = Database.__new__(Database)
    database.connectionSettings = settings
    database.type = dbType
    from bauta.database import DIALECTS
    database.dialect = DIALECTS[dbType]
    database.cursor = MagicMock()
    database.connection = MagicMock()
    database.primaryKeyCache = {}
    database._streams = set()
    database.columnNameCache = {}
    database.columnTypeCache = {}
    database._preparers = {}
    database.getAllColumnNames = MagicMock(return_value=['id', 'name'])
    database.getPrimaryColumnNames = MagicMock(return_value=['id'])

    return database


def _copies(cursor):
    """Each COPY statement psycopg ran."""

    return [call.args[0] for call in cursor.copy.call_args_list]


def _copiedRows(cursor):
    """Every row written into any COPY, in order."""

    return [call.args[0] for call in cursor.copy.return_value.__enter__.return_value.write_row.call_args_list]


def test_get_all_column_names_and_types_use_a_bounded_query():
    """SELECT * with no WHERE clause is an unbounded/full-table-shaped query just to
    read cursor.description -- WHERE 1=0 is the fix, and is valid across all 3 dialects.
    """
    database = _mockedDatabase(DatabaseType.MYSQL)
    database.getAllColumnNames = Database.getAllColumnNames.__get__(database)
    database.getAllColumnTypes = Database.getAllColumnTypes.__get__(database)
    database.cursor.description = [('id', 'INT'), ('name', 'VARCHAR')]

    database.getAllColumnNames(table='people')
    database.getAllColumnTypes(table='people')

    for callArgs in database.cursor.execute.call_args_list:
        assert 'WHERE 1=0' in callArgs[0][0]


@pytest.mark.parametrize('dbType', [DatabaseType.MYSQL, DatabaseType.POSTGRESQL, DatabaseType.SQLITE, DatabaseType.DUCKDB])
def test_an_upsert_into_a_table_without_a_primary_key_fails_rather_than_guessing(dbType):
    """With no key there's nothing to match rows on. MySQL used to insert a
    duplicate of every row on every run; the others generated invalid SQL.
    """
    from bauta.configuration import ConfigurationError

    database = _mockedDatabase(dbType)
    database.getPrimaryColumnNames = MagicMock(return_value=[])

    with pytest.raises(ConfigurationError, match='no primary key'):
        database.upsert(table='people', data=[(1, 'a')])

    database.cursor.executemany.assert_not_called()


@pytest.mark.parametrize('target,stage,expectedTarget,expectedStage,expectedTemp', [
    ('people', 'people_stage', '`people`', '`people_stage`', '`people_tmp`'),
    ('sales.people', 'sales.people_stage', '`sales`.`people`', '`sales`.`people_stage`', '`sales`.`people_tmp`'),
    ])
def test_swap_puts_the_temporary_table_in_the_stage_tables_schema(target, stage, expectedTarget, expectedStage, expectedTemp):
    database = _mockedDatabase(DatabaseType.MYSQL)
    database.dialect = MagicMock()

    database.swap(targetTable=target, stageTable=stage)

    database.dialect.swap.assert_called_once_with(database.cursor, targetTable=expectedTarget, stageTable=expectedStage, tempTable=expectedTemp)


def test_postgresql_inserts_through_copy_one_batch_at_a_time():
    database = _mockedDatabase(DatabaseType.POSTGRESQL)

    database.insert(table='people', data=[(1, 'a'), (2, 'b'), (3, 'c')], chunkSize=2, columns=['id', 'name'])

    assert _copies(database.cursor) == ['COPY "people" ("id", "name") FROM STDIN'] * 2
    assert _copiedRows(database.cursor) == [(1, 'a'), (2, 'b'), (3, 'c')]
    database.cursor.executemany.assert_not_called()
    assert database.connection.commit.call_count == 2


def test_a_copied_upsert_sends_only_the_last_row_of_each_key():
    """One INSERT ... ON CONFLICT can't touch a row twice; applying the rows in
    turn, as executemany does, leaves the last one -- so that one is sent.
    """
    database = _mockedDatabase(DatabaseType.POSTGRESQL)

    database.upsert(table='people', data=[(1, 'a'), (2, 'b'), (1, 'c')])

    assert len(_copies(database.cursor)) == 1
    assert _copiedRows(database.cursor) == [(1, 'c'), (2, 'b')]
    statements = [call.args[0] for call in database.cursor.execute.call_args_list]
    assert statements[0].startswith('CREATE TEMPORARY TABLE IF NOT EXISTS bauta_upsert_')
    assert 'ON COMMIT DELETE ROWS AS SELECT "id", "name" FROM "people" WITH NO DATA' in statements[0]
    assert statements[1].startswith('INSERT INTO "people" ("id", "name") SELECT "id", "name" FROM bauta_upsert_')
    assert statements[1].endswith('ON CONFLICT("id") DO UPDATE SET "name"=excluded."name"')


def test_a_copied_upsert_creates_its_staging_table_once_per_connection():
    """The table outlives each chunk's commit, so creating it every chunk was a
    round trip each for nothing.
    """
    database = _mockedDatabase(DatabaseType.POSTGRESQL)

    for _ in range(3):
        database.upsert(table='people', data=[(1, 'a'), (2, 'b')])

    statements = [call.args[0] for call in database.cursor.execute.call_args_list]
    assert sum(statement.startswith('CREATE TEMPORARY TABLE') for statement in statements) == 1
    assert sum(statement.startswith('INSERT INTO') for statement in statements) == 3


def test_a_staging_table_created_by_a_failed_chunk_is_created_again():
    """A failed chunk's rollback undoes the CREATE it ran, so the next chunk on
    the same connection can't assume the table is there.
    """
    database = _mockedDatabase(DatabaseType.POSTGRESQL)
    database.cursor.copy.side_effect = [RuntimeError('COPY failed'), MagicMock(), MagicMock()]

    with pytest.raises(RuntimeError):
        database.upsert(table='people', data=[(1, 'a')])
    database.upsert(table='people', data=[(1, 'a')])

    statements = [call.args[0] for call in database.cursor.execute.call_args_list]
    assert sum(statement.startswith('CREATE TEMPORARY TABLE') for statement in statements) == 2


@pytest.mark.parametrize('dbType,quoted', [(DatabaseType.MYSQL, '`rank`'), (DatabaseType.MSSQL, '[rank]'), (DatabaseType.ORACLE, '"RANK"')])
def test_loads_quote_column_names_as_the_catalog_spells_them(dbType, quoted):
    """A reserved word can be a column, and a configured `rank` still finds
    Oracle's RANK, since the name is resolved before it is quoted.
    """
    database = _mockedDatabase(dbType)
    database.getAllColumnNames = MagicMock(return_value=['ID', 'RANK'] if dbType == DatabaseType.ORACLE else ['id', 'rank'])

    database.insert(table='scores', data=[(1, 2)], columns=['id', 'rank'])

    (statement,) = {call.args[0] for call in database.cursor.executemany.call_args_list} | {
        call.args[0] for call in database.cursor.execute.call_args_list}
    assert quoted in statement


def test_a_configured_column_the_table_lacks_is_a_configuration_error():
    from bauta.configuration import ConfigurationError

    database = _mockedDatabase(DatabaseType.MYSQL)

    with pytest.raises(ConfigurationError, match='people has no column nmae'):
        database.insert(table='people', data=[(1, 'a')], columns=['id', 'nmae'])


def test_columns_differing_only_in_case_must_be_named_exactly():
    from bauta.configuration import ConfigurationError

    database = _mockedDatabase(DatabaseType.POSTGRESQL)
    database.getAllColumnNames = MagicMock(return_value=['id', 'Name', 'NAME'])

    with pytest.raises(ConfigurationError, match='differ only in case'):
        database.insert(table='people', data=[(1, 'a')], columns=['id', 'name'])

    database.insert(table='people', data=[(1, 'a')], columns=['id', 'Name'])


def test_chunk_insert_splits_data_into_multiple_batches():
    """5 records with chunkSize=2 should batch as [0:2], [2:4], [4:6] (3 executemany
    calls, the last a partial batch) -- covers the chunking loop itself, not just
    that a single executemany call happens.
    """
    database = _mockedDatabase(DatabaseType.MYSQL)
    data = [(1,), (2,), (3,), (4,), (5,)]

    database.insert(table='people', data=data, chunkSize=2)

    batches = [callArgs[0][1] for callArgs in database.cursor.executemany.call_args_list]
    assert batches == [[(1,), (2,)], [(3,), (4,)], [(5,)]]
    assert database.connection.commit.call_count == 3


def test_chunk_insert_does_nothing_for_empty_data():
    database = _mockedDatabase(DatabaseType.MYSQL)

    database.insert(table='people', data=[], chunkSize=100)

    database.cursor.executemany.assert_not_called()


@pytest.mark.parametrize('dbType,expectedQuery', [
    (DatabaseType.MYSQL, 'TRUNCATE TABLE `people`'),
    (DatabaseType.POSTGRESQL, 'TRUNCATE TABLE "people"'),
    (DatabaseType.ORACLE, 'TRUNCATE TABLE "PEOPLE"'),
    (DatabaseType.MSSQL, 'TRUNCATE TABLE [people]'),
    (DatabaseType.MARIADB, 'TRUNCATE TABLE `people`'),
    (DatabaseType.SQLITE, 'DELETE FROM "people"'),  # SQLite has no TRUNCATE statement
    ])
def test_truncate_uses_the_dialects_truncate_query(dbType, expectedQuery):
    database = _mockedDatabase(dbType)

    database.truncate(table='people')

    assert database.cursor.execute.call_args[0][0] == expectedQuery
    assert database.connection.commit.call_count == 1


def test_context_manager_closes_on_normal_exit():
    database = _mockedDatabase(DatabaseType.MYSQL)

    with database:
        pass

    database.cursor.close.assert_called_once()
    database.connection.close.assert_called_once()


def test_context_manager_closes_even_on_exception():
    database = _mockedDatabase(DatabaseType.MYSQL)

    with pytest.raises(ValueError):
        with database:
            raise ValueError('boom')

    database.cursor.close.assert_called_once()
    database.connection.close.assert_called_once()


@pytest.mark.parametrize('rowCount,chunkSize,expectedBatchSizes', [
    (100, 100, [100]),
    (200, 100, [100, 100]),
    (250, 100, [100, 100, 50]),
    (1, 100, [1]),
    (0, 100, []),
    ])
def test_insert_batches_without_issuing_an_empty_statement(rowCount, chunkSize, expectedBatchSizes):
    """Regression check for the chunk walk running one slice past the end: when
    rowCount was an exact multiple of chunkSize the old `index > numberRecords`
    test admitted a final, always-empty executemany (100 rows at chunkSize 100
    issued two calls, the second with []), which some drivers reject. An empty
    `data` must issue no statement at all.
    """
    database = _mockedDatabase(DatabaseType.MYSQL)
    data = [(index, 'name') for index in range(rowCount)]

    database.insert(table='people', data=data, chunkSize=chunkSize)

    batchSizes = [len(call.args[1]) for call in database.cursor.executemany.call_args_list]

    assert batchSizes == expectedBatchSizes
    assert database.connection.commit.call_count == len(expectedBatchSizes)


def test_insert_covers_every_row_exactly_once_across_chunks():
    """The batches must reassemble into the original data, in order -- a wrong
    stride would still produce a plausible-looking batch count.
    """
    database = _mockedDatabase(DatabaseType.MYSQL)
    data = [(index, 'name') for index in range(250)]

    database.insert(table='people', data=data, chunkSize=100)

    submitted = [row for call in database.cursor.executemany.call_args_list for row in call.args[1]]

    assert submitted == data


def test_upsert_of_an_empty_result_set_issues_no_statement():
    database = _mockedDatabase(DatabaseType.MYSQL)

    database.upsert(table='people', data=[], chunkSize=100)

    assert database.cursor.executemany.call_count == 0
    assert database.connection.commit.call_count == 0


def _streamingDatabase(dbType=DatabaseType.MYSQL):
    database = _mockedDatabase(dbType)
    streamCursor = MagicMock()
    streamCursor.fetchmany.side_effect = [[(1,)], [(2,)], []]
    streamCursor.description = [('id',)]
    database.dialect = MagicMock(wraps=database.dialect)
    database.dialect.streamingCursor.return_value = streamCursor
    return database, streamCursor


def test_closing_a_stream_that_was_never_read_releases_its_rows():
    """A generator closed before it starts skips its `finally`, which left
    MySQL's connection refusing every later statement.
    """
    database, streamCursor = _streamingDatabase()

    columns, chunks = database.stream('SELECT id FROM t', chunkSize=1)
    chunks.close()

    assert columns == ['id']
    database.dialect.discardRemaining.assert_called_once_with(database.connection, streamCursor)
    streamCursor.close.assert_called_once()
    assert list(chunks) == []


def test_a_stream_read_to_the_end_closes_itself():
    database, streamCursor = _streamingDatabase()

    _, chunks = database.stream('SELECT id FROM t', chunkSize=1)

    assert list(chunks) == [[(1,)], [(2,)]]
    streamCursor.close.assert_called_once()
    assert database._streams == set()


def test_closing_the_database_closes_an_open_stream_first():
    database, streamCursor = _streamingDatabase()
    order = []
    streamCursor.close.side_effect = lambda: order.append('stream')
    database.cursor.close.side_effect = lambda: order.append('cursor')

    database.stream('SELECT id FROM t', chunkSize=1)
    database.close()

    assert order == ['stream', 'cursor']
    database.connection.close.assert_called_once()


def test_a_failure_to_close_does_not_replace_the_error_that_ended_the_block():
    database = _mockedDatabase(DatabaseType.MYSQL)
    database.cursor.close.side_effect = RuntimeError('Unread result found')

    with pytest.raises(ValueError, match='the policy does not cover email'):
        with database:
            raise ValueError('the policy does not cover email')

    database.connection.close.assert_called_once()


def test_a_query_that_fails_closes_its_stream():
    database, streamCursor = _streamingDatabase()
    streamCursor.execute.side_effect = RuntimeError('syntax')

    with pytest.raises(RuntimeError):
        database.stream('SELEC id FROM t', chunkSize=1)

    streamCursor.close.assert_called_once()
    assert database._streams == set()


# listTables --------------------------------------------------------------------

def _sqliteWithTables(tmp_path, name='tables.db'):
    import sqlite3

    from bauta.configuration import connectionConfig

    path = tmp_path / name
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)')
    connection.execute('CREATE TABLE orders (id INTEGER PRIMARY KEY, customerId INTEGER REFERENCES customers(id))')
    connection.execute('CREATE VIEW recentOrders AS SELECT * FROM orders')
    connection.commit()
    connection.close()

    return connectionConfig(type='sqlite', path=str(path))


def test_list_tables_returns_base_tables_sorted_without_views(tmp_path):
    from bauta.database import Database

    with Database(connectionSettings=_sqliteWithTables(tmp_path)) as database:
        assert database.listTables() == ['customers', 'orders']


def test_list_tables_leaves_out_sqlites_own_tables(tmp_path):
    import sqlite3

    from bauta.configuration import connectionConfig
    from bauta.database import Database

    path = tmp_path / 'sequence.db'
    connection = sqlite3.connect(path)
    # AUTOINCREMENT is what makes SQLite create its sqlite_sequence table.
    connection.execute('CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT)')
    connection.execute('INSERT INTO items DEFAULT VALUES')
    connection.commit()
    connection.close()

    with Database(connectionSettings=connectionConfig(type='sqlite', path=str(path))) as database:
        assert database.listTables() == ['items']


def test_listed_tables_can_be_used_to_read_their_own_columns(tmp_path):
    """The point of the shape: what listTables returns goes straight back into
    catalogColumns and getPrimaryColumnNames.
    """
    from bauta.database import Database

    with Database(connectionSettings=_sqliteWithTables(tmp_path)) as database:
        for table in database.listTables():
            assert database.catalogColumns(table)
            assert database.getPrimaryColumnNames(table) == ['id']


def test_a_listed_name_that_needs_quoting_still_reads_back_as_itself(tmp_path):
    """A reserved word, a name with a space and a name with a dot in it: each
    has to come back in a spelling that means that same table.
    """
    import sqlite3

    from bauta.configuration import connectionConfig
    from bauta.database import Database

    path = tmp_path / 'awkward.db'
    connection = sqlite3.connect(path)
    for name in ('"order"', '"two words"', '"dotted.name"'):
        connection.execute('CREATE TABLE {} (id INTEGER PRIMARY KEY, value TEXT)'.format(name))
    connection.commit()
    connection.close()

    with Database(connectionSettings=connectionConfig(type='sqlite', path=str(path))) as database:
        listed = database.listTables()
        assert listed == ['"dotted.name"', 'order', 'two words']
        for table in listed:
            assert database.catalogColumns(table) == ['id', 'value']


def test_list_tables_qualifies_names_only_when_a_schema_was_asked_for(tmp_path):
    from bauta.database import Database

    settings = _sqliteWithTables(tmp_path)
    other = _sqliteWithTables(tmp_path, name='other.db')

    with Database(connectionSettings=settings) as database:
        database.cursor.execute("ATTACH DATABASE '{}' AS extra".format(other.path))

        assert database.listTables() == ['customers', 'orders']
        assert database.listTables(schema='extra') == ['extra.customers', 'extra.orders']
        # And a qualified name still reads its own columns.
        assert database.catalogColumns('extra.orders') == ['id', 'customerId']


def test_foreign_keys_of_an_attached_schema_are_named_as_list_tables_names_its_tables(tmp_path):
    """Keys used to be read from the connection's own schema whatever was
    asked, so discover --schema and schema --schema lost every one.
    """
    from bauta.database import Database

    settings = _sqliteWithTables(tmp_path)
    other = _sqliteWithTables(tmp_path, name='other.db')

    with Database(connectionSettings=settings) as database:
        database.cursor.execute("ATTACH DATABASE '{}' AS extra".format(other.path))

        assert [(key.table, key.referencedTable) for key in database.getForeignKeys()] == [('orders', 'customers')]
        assert [(key.table, key.referencedTable) for key in database.getForeignKeys('extra')] == [('extra.orders', 'extra.customers')]
        assert [key.table for key in database.getForeignKeysFor(['extra.orders', 'orders'])] == ['orders', 'extra.orders']
        assert database.foreignKeysElsewhere([None]) == {'extra': 1}


def test_a_key_is_named_bare_only_in_the_current_schema_unless_that_schema_was_named():
    from bauta.database.dialects.base import _relativeName

    assert _relativeName('public', 'orders', 'public', None) == 'orders'
    assert _relativeName('app', 'orders', 'public', None) == 'app.orders'
    assert _relativeName('app', 'orders', 'public', 'app') == 'app.orders'
    assert _relativeName('public', 'customers', 'public', 'app') == 'customers'
    # Named explicitly, as listTables(schema='public') qualifies its tables.
    assert _relativeName('public', 'orders', 'public', 'public') == 'public.orders'


def test_sqlite_foreign_keys_and_list_tables_agree_on_which_tables_exist(tmp_path):
    """foreignKeys walks the tables listTables returns, so the two must not
    drift apart -- SQLite's own tables have no foreign keys to read.
    """
    from bauta.database import Database

    with Database(connectionSettings=_sqliteWithTables(tmp_path)) as database:
        keys = database.getForeignKeys()

        assert [key.table for key in keys] == ['orders']
        assert set(key.table for key in keys) <= set(database.listTables())


def test_processes_opening_a_new_sqlite_file_at_once_all_switch_it_to_wal(tmp_path):
    """The switch takes an exclusive lock without waiting for it, so all but
    one of several connections to a new file used to fail with "database is
    locked". A barrier opens them together, on a few new files.
    """
    import multiprocessing

    context = multiprocessing.get_context('spawn')
    with context.Manager() as manager:
        for round in range(5):
            path = str(tmp_path / 'new{}.db'.format(round))
            barrier = manager.Barrier(4)
            with context.Pool(4) as pool:
                modes = pool.starmap(_openAndReadJournalMode, [(path, barrier)] * 4)
            assert modes == ['wal'] * 4


def _openAndReadJournalMode(path, barrier):
    from bauta.configuration import connectionConfig
    from bauta.database import Database

    barrier.wait()
    with Database(connectionSettings=connectionConfig(type='sqlite', path=path), create=True) as database:
        return database.query('PRAGMA journal_mode')[0][0]


def test_an_in_memory_sqlite_database_opens_though_it_cannot_use_wal():
    from bauta.configuration import connectionConfig
    from bauta.database import Database

    with Database(connectionSettings=connectionConfig(type='sqlite', path=':memory:')) as database:
        assert database.query('PRAGMA journal_mode')[0][0] == 'memory'


@pytest.mark.parametrize('renamesDone, expected, target, stage', [(1, 'undid it', [(1,)], [(2,)]), (2, 'finished it', [(2,)], [(1,)])])
def test_a_swap_killed_between_renames_is_put_right_where_renames_commit_alone(tmp_path, monkeypatch, renamesDone, expected, target, stage):
    """Oracle commits each rename, so a job killed between them left the
    tables part-way: after the second, no target at all, and every later run
    failed looking for it. Checked here on SQLite, told its swap isn't atomic;
    test_integration_oracle.py checks Oracle itself.
    """
    from bauta.database import DIALECTS

    monkeypatch.setattr(DIALECTS[DatabaseType.SQLITE], 'ATOMIC_SWAP', False)
    with Database(connectionSettings=connectionConfig(type='sqlite', path=str(tmp_path / 't.db')), create=True) as database:
        for table, row in (('orders', 1), ('orders_stage', 2)):
            database.alter('CREATE TABLE {} (id INT PRIMARY KEY)'.format(table))
            database.insert(table=table, data=[(row,)])
        database.alter('ALTER TABLE orders_stage RENAME TO orders_tmp')
        if renamesDone == 2:
            database.alter('ALTER TABLE orders RENAME TO orders_stage')

        assert expected in database.recoverInterruptedSwap('orders', 'orders_stage')
        assert (database.query('SELECT id FROM orders'), database.query('SELECT id FROM orders_stage')) == (target, stage)
        assert not database.tableExists('orders_tmp')
        assert database.recoverInterruptedSwap('orders', 'orders_stage') is None

        database.alter('CREATE TABLE orders_tmp (id INT)')
        with pytest.raises(ConfigurationError, match='all exist'):
            database.recoverInterruptedSwap('orders', 'orders_stage')


def test_an_atomic_swap_never_looks_for_a_leftover(tmp_path):
    with Database(connectionSettings=connectionConfig(type='sqlite', path=str(tmp_path / 't.db')), create=True) as database:
        database.alter('CREATE TABLE orders_tmp (id INT)')

        assert database.recoverInterruptedSwap('orders', 'orders_stage') is None


@pytest.mark.parametrize('answer, message', [(False, 'says this session is not encrypted'), (None, 'could not say whether')])
def test_require_encryption_refuses_a_session_the_server_does_not_call_encrypted(monkeypatch, answer, message):
    """TLS is set in a connection's options, and nothing checked it was:
    PostgreSQL, Oracle and SQL Server without it copied production in the
    clear, saying so only in a dry run's log line. A server that can't say
    is refused too, since the setting promises the one answer.
    """
    from bauta.database import DIALECTS

    connection = MagicMock()
    monkeypatch.setattr(DIALECTS[DatabaseType.POSTGRESQL], 'connect', lambda settings: (connection, MagicMock()))
    monkeypatch.setattr(DIALECTS[DatabaseType.POSTGRESQL], 'isEncrypted', lambda cursor: answer)
    settings = connectionConfig(type='postgresql', user='u', password='p', database='d', host='h', requireEncryption=True)

    with pytest.raises(ConfigurationError, match=message):
        Database(connectionSettings=settings)
    connection.close.assert_called_once()

    monkeypatch.setattr(DIALECTS[DatabaseType.POSTGRESQL], 'isEncrypted', lambda cursor: True)
    Database(connectionSettings=settings).close()
    Database(connectionSettings=settings.model_copy(update={'requireEncryption': False, 'host': 'h2'})).close()


def test_require_encryption_is_a_server_connections_setting():
    with pytest.raises(Exception, match='requireEncryption'):
        connectionConfig(type='sqlite', path=':memory:', requireEncryption=True)


def test_a_read_only_connection_refuses_every_write_and_its_session_does_too(tmp_path):
    """Every write bauta makes -- a load, clear's DELETE, schema --apply's
    DDL, a swap -- goes through a method that refuses on a readOnly
    connection, and SQLite's session refuses a write made around them.
    """
    import sqlite3

    path = tmp_path / 'prod.db'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE orders (id INT PRIMARY KEY)')
        connection.execute('INSERT INTO orders VALUES (1)')
    connection.close()
    settings = connectionConfig(type='sqlite', path=str(path), readOnly=True)

    with Database(connectionSettings=settings) as database:
        for write in (lambda: database.insert('orders', [(2,)]), lambda: database.upsert('orders', [(2,)]), lambda: database.truncate('orders'),
                      lambda: database.alter('DROP TABLE orders'), lambda: database.execute('DELETE FROM orders'),
                      lambda: database.swap('orders', 'orders_stage')):
            with pytest.raises(ConfigurationError, match='is readOnly'):
                write()
        with pytest.raises(sqlite3.OperationalError, match='readonly|read-only|query_only'):
            database.cursor.execute('INSERT INTO orders VALUES (3)')

        assert database.query('SELECT id FROM orders') == [(1,)]


def test_a_watermark_named_twice_is_bound_twice(tmp_path):
    """`created > {{ watermark }} OR updated > {{ watermark }}` passed
    validation, and every run failed: two placeholders, one parameter.
    """
    with Database(connectionSettings=connectionConfig(type='sqlite', path=str(tmp_path / 't.db')), create=True) as database:
        database.alter('CREATE TABLE events (id INT, created INT, updated INT)')
        database.insert('events', [(1, 1, 1), (2, 1, 9), (3, 9, 1)])

        query, parameters = database.bindWatermark('SELECT id FROM events WHERE created > {{ watermark }} OR updated > {{watermark}} ORDER BY id', 5)
        _, chunks = database.stream(query, 10, parameters)

        assert parameters == (5, 5) and list(chunks) == [[(2,), (3,)]]
        assert database.bindWatermark('SELECT 1', 5) == ('SELECT 1', ())


def test_a_watermark_in_a_comment_or_a_literal_is_left_unbound(tmp_path):
    """Counting every {{ watermark }} bound one for each mention in a comment
    too, which SQLite, DuckDB and Oracle don't count as a placeholder: the
    query failed on a count of bindings.
    """
    with Database(connectionSettings=connectionConfig(type='sqlite', path=str(tmp_path / 't.db')), create=True) as database:
        database.alter('CREATE TABLE events (id INT)')
        database.insert('events', [(1,), (5,)])
        for text in ('SELECT id FROM events WHERE id > {{ watermark }} -- from {{ watermark }}',
                     "SELECT id FROM events /* {{ watermark }} */ WHERE id > {{ watermark }} AND 'a {{ watermark }}' <> ''"):
            query, parameters = database.bindWatermark(text, 2)
            _, chunks = database.stream(query, 10, parameters)
            assert parameters == (2,) and list(chunks) == [[(5,)]]

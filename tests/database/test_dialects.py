import datetime
import decimal
import uuid

import pytest

from bauta.configuration import DatabaseType
from bauta.database.dialects import ColumnCategory, DuckDBDialect, MariaDBDialect, MSSQLDialect, MySQLDialect, OracleDialect, PostgreSQLDialect, SQLiteDialect, \
    catalogName, quoteFoldedTable, quoteTableName, splitTableName, suffixedName

ALL_COLUMNS = ['id', 'name', 'amount']
PRIMARY_KEY_COLUMNS = ['id']
NON_PRIMARY_KEY_COLUMNS = ['name', 'amount']


@pytest.mark.parametrize('dialect', [MySQLDialect(), PostgreSQLDialect(), OracleDialect(), MSSQLDialect(), DuckDBDialect()])
@pytest.mark.parametrize('query', ['primaryKeyQuery', 'columnsQuery', 'tableExistsQuery'])
def test_catalog_queries_bind_the_schema_and_table_rather_than_interpolating_them(dialect, query):
    """A lookup that ignored the schema used to pick up a same-named table
    elsewhere on the server, and its key columns with it. Both parts are bound,
    and a NULL schema falls back to the connection's current one.
    """
    text = getattr(dialect, query)()

    assert text.count('{}') == 2
    assert 'COALESCE(' in text


@pytest.mark.parametrize('dialect', [MySQLDialect(), PostgreSQLDialect(), OracleDialect(), MSSQLDialect(), DuckDBDialect()])
def test_the_primary_key_query_ignores_unique_constraints(dialect):
    """Treating UNIQUE columns as key columns made an upsert match on (id,
    email): a changed email became an insert that violated the real key, and
    PostgreSQL refused the ON CONFLICT list outright.
    """
    assert 'UNIQUE' not in dialect.primaryKeyQuery().upper().replace('INDISUNIQUE', '')
    assert "'U'" not in dialect.primaryKeyQuery()


class _RecordingCursor:

    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, query, parameters=()):
        self.executed.append((query, parameters))

    def fetchall(self):
        return self.rows


@pytest.mark.parametrize('table,expected', [('people', (None, 'people')), ('sales.people', ('sales', 'people'))])
def test_the_primary_key_lookup_splits_a_qualified_table_name(table, expected):
    cursor = _RecordingCursor([('id',)])

    assert MySQLDialect().primaryKey(cursor, table) == ['id']
    assert cursor.executed[0][1] == expected
    assert '%s' in cursor.executed[0][0]


def test_mysql_upsert_of_a_key_only_table_does_not_use_insert_ignore():
    """INSERT IGNORE also turns truncation, NOT NULL and foreign-key errors into
    warnings, silently dropping or mangling rows.
    """
    query = MySQLDialect().upsertQuery('links', ['a', 'b'], ['a', 'b'], [])

    assert 'IGNORE' not in query
    assert query.endswith('ON DUPLICATE KEY UPDATE links.a=links.a')


@pytest.mark.parametrize('dialect', [PostgreSQLDialect(), OracleDialect(), SQLiteDialect(), DuckDBDialect()])
def test_a_rename_takes_the_new_name_unqualified(dialect):
    """`ALTER TABLE sales.orders RENAME TO sales.orders_tmp` is a syntax error;
    the renamed table stays in its schema anyway.
    """
    queries = ' '.join(dialect.swapQueries('sales.orders', 'sales.orders_stage', 'sales.orders_tmp'))

    assert 'RENAME TO sales.' not in queries
    assert 'ALTER TABLE sales.orders_stage RENAME TO orders_tmp' in queries
    assert 'ALTER TABLE sales.orders RENAME TO orders_stage' in queries
    assert 'ALTER TABLE sales.orders_tmp RENAME TO orders' in queries


def test_mssql_sp_rename_takes_the_new_name_unqualified():
    """sp_rename would otherwise create a table literally named `sales.orders_tmp`."""
    (query,) = MSSQLDialect().swapQueries('sales.orders', 'sales.orders_stage', 'sales.orders_tmp')

    assert query == ("EXEC sp_rename 'sales.orders_stage', 'orders_tmp'; EXEC sp_rename 'sales.orders', 'orders_stage'; "
                     "EXEC sp_rename 'sales.orders_tmp', 'orders';")


def test_oracle_catalog_queries_look_in_the_current_schema_not_every_schema():
    for query in (OracleDialect().primaryKeyQuery(), OracleDialect().columnsQuery(), OracleDialect().tableExistsQuery()):
        assert "SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')" in query
        assert 'table_name = {}' in query


def test_oracle_catalog_queries_bind_the_name_rather_than_upper_casing_it():
    """UPPER() in the query hid every table whose name is quoted, which on
    Oracle is the only way to name a lower-case one.
    """

    for query in (OracleDialect().primaryKeyQuery(), OracleDialect().columnsQuery(), OracleDialect().tableExistsQuery()):
        assert 'UPPER(' not in query


def test_postgresql_catalog_queries_bind_the_name_rather_than_lower_casing_it():
    for query in (PostgreSQLDialect().primaryKeyQuery(), PostgreSQLDialect().columnsQuery(), PostgreSQLDialect().tableExistsQuery()):
        assert 'lower(' not in query


def test_oracle_upsert_query_omits_when_matched_with_no_non_primary_columns():
    """An empty UPDATE SET is invalid Oracle syntax -- a table of only primary-key
    columns has nothing to update, so WHEN MATCHED must be dropped entirely.
    """
    query = OracleDialect().upsertQuery('ids_only', ['id'], ['id'], [])
    assert 'WHEN MATCHED' not in query
    assert 'WHEN NOT MATCHED THEN INSERT (id) VALUES (source.id)' in query


def test_oracle_upsert_from_stage_query_sources_the_stage_table_not_dual():
    query = OracleDialect().upsertFromStageQuery('people', 'people_stage', ALL_COLUMNS, PRIMARY_KEY_COLUMNS, NON_PRIMARY_KEY_COLUMNS)
    assert query.startswith('MERGE INTO people target USING people_stage source')
    assert 'FROM dual' not in query


def test_mssql_upsert_query_omits_when_matched_with_no_non_primary_columns():
    """Same reasoning as the equivalent Oracle test: an empty UPDATE SET is invalid
    T-SQL syntax too, so WHEN MATCHED must be dropped entirely.
    """
    query = MSSQLDialect().upsertQuery('ids_only', ['id'], ['id'], [])
    assert 'WHEN MATCHED' not in query
    assert 'WHEN NOT MATCHED THEN INSERT (id) VALUES (source.id)' in query


def test_mssql_upsert_from_stage_query_sources_the_stage_table_not_values():
    query = MSSQLDialect().upsertFromStageQuery('people', 'people_stage', ALL_COLUMNS, PRIMARY_KEY_COLUMNS, NON_PRIMARY_KEY_COLUMNS)
    assert query.startswith('MERGE INTO people AS target USING people_stage AS source')
    assert 'VALUES' not in query.split('ON')[0]


@pytest.mark.parametrize('table,expected', [('people', ('people', 'main')), ('other.people', ('people', 'other'))])
def test_sqlite_primary_key_lookup_binds_the_table_and_attached_database(table, expected):
    cursor = _RecordingCursor([('id',)])

    assert SQLiteDialect().primaryKey(cursor, table) == ['id']
    assert 'pragma_table_info(?, ?)' in cursor.executed[0][0]
    assert cursor.executed[0][1] == expected


def test_mysql_column_category_maps_known_type_names():
    assert MySQLDialect().columnCategory('INT') == ColumnCategory.NUMBER
    assert MySQLDialect().columnCategory('varchar') == ColumnCategory.TEXT  # case-insensitive
    assert MySQLDialect().columnCategory('DATETIME') == ColumnCategory.DATE
    assert MySQLDialect().columnCategory('BLOB') is None
    assert MySQLDialect().columnCategory(1234) is None  # non-string dataType is simply unrecognized, not an error


def test_mariadb_column_category_is_inherited_from_mysql():
    assert MariaDBDialect().columnCategory('INT') == ColumnCategory.NUMBER


def test_postgresql_column_category_maps_known_oids():
    assert PostgreSQLDialect().columnCategory(23) == ColumnCategory.NUMBER  # int4
    assert PostgreSQLDialect().columnCategory(25) == ColumnCategory.TEXT  # text
    assert PostgreSQLDialect().columnCategory(1114) == ColumnCategory.DATE  # timestamp
    assert PostgreSQLDialect().columnCategory(9999) is None


def test_oracle_column_category_matches_on_db_type_name():
    class _FakeDbType:
        def __init__(self, name):
            self.name = name

    assert OracleDialect().columnCategory(_FakeDbType('DB_TYPE_NUMBER')) == ColumnCategory.NUMBER
    assert OracleDialect().columnCategory(_FakeDbType('DB_TYPE_VARCHAR')) == ColumnCategory.TEXT
    assert OracleDialect().columnCategory(_FakeDbType('DB_TYPE_TIMESTAMP')) == ColumnCategory.DATE
    assert OracleDialect().columnCategory(_FakeDbType('DB_TYPE_BLOB')) is None
    assert OracleDialect().columnCategory('not a db type object') is None


def test_sqlite_swap_is_three_separate_statements_in_one_transaction():
    """sqlite3's cursor.execute() runs one statement at a time, and doesn't open a
    transaction before DDL -- without the BEGIN, each rename would commit alone.
    """
    queries = SQLiteDialect().swapQueries('people', 'people_stage', 'people_tmp')
    assert queries == [
        'BEGIN',
        'ALTER TABLE people_stage RENAME TO people_tmp',
        'ALTER TABLE people RENAME TO people_stage',
        'ALTER TABLE people_tmp RENAME TO people',
        ]


@pytest.mark.parametrize('dialect', [
    MySQLDialect(), MariaDBDialect(), PostgreSQLDialect(), SQLiteDialect(), OracleDialect(), MSSQLDialect(), DuckDBDialect(),
    ])
def test_upsert_of_a_key_only_table_is_valid_sql(dialect):
    """Every column is part of the primary key, so there is nothing to update on
    a conflict. An empty SET clause is a syntax error, and the INSERT-based
    dialects used to emit a dangling `DO UPDATE SET` / `ON DUPLICATE KEY UPDATE`
    and fail at the database (sqlite3: "incomplete input"). Oracle and MSSQL
    already dropped WHEN MATCHED from their MERGE for this case.

    Bridge tables and id-only lookup tables have exactly this shape. MySQL
    assigns the key to itself, the one conflict action it has that does nothing.
    """
    query = dialect.upsertQuery(table='t', allColumns=['id'], primaryKeyColumns=['id'], nonPrimaryKeyColumns=[])

    assert not query.rstrip().endswith(('SET', 'UPDATE'))
    assert 'DO UPDATE SET ' not in query
    assert 'ON DUPLICATE KEY UPDATE ' not in query or query.endswith('ON DUPLICATE KEY UPDATE t.id=t.id')


@pytest.mark.parametrize('dialect', [
    MySQLDialect(), MariaDBDialect(), PostgreSQLDialect(), SQLiteDialect(), OracleDialect(), MSSQLDialect(), DuckDBDialect(),
    ])
def test_upsert_from_stage_of_a_key_only_table_is_valid_sql(dialect):
    query = dialect.upsertFromStageQuery(targetTable='t', stageTable='s', allColumns=['id'], primaryKeyColumns=['id'], nonPrimaryKeyColumns=[])

    assert not query.rstrip().endswith(('SET', 'UPDATE'))
    assert 'DO UPDATE SET ' not in query
    assert 'ON DUPLICATE KEY UPDATE ' not in query or query.endswith('ON DUPLICATE KEY UPDATE t.id=t.id')


def test_copy_writes_each_row_through_psycopg():
    """psycopg's own encoders spell every value; ours only chooses COPY."""
    from unittest.mock import MagicMock

    from bauta.database.dialects.postgresql import _copyIn

    cursor = MagicMock()
    rows = [(1, 'a\tb', None), (2, '', b'\x00')]

    assert _copyIn(cursor, 'COPY t (a, b, c) FROM STDIN', rows) is True

    cursor.copy.assert_called_once_with('COPY t (a, b, c) FROM STDIN')
    written = cursor.copy.return_value.__enter__.return_value.write_row.call_args_list
    assert [call.args[0] for call in written] == rows


class _ExecutingCursor:

    def __init__(self):
        self.executed = []

    def execute(self, query, parameters=()):
        self.executed.append((query, parameters))


def test_mssql_bulk_insert_sends_a_thousand_rows_per_statement():
    cursor = _ExecutingCursor()
    rows = [(index, 'n{}'.format(index)) for index in range(2500)]

    assert MSSQLDialect().bulkInsert(cursor, 'people', ['id', 'name'], rows) is True

    assert [query.count('(%s, %s)') for query, _ in cursor.executed] == [1000, 1000, 500]
    assert cursor.executed[0][0].startswith('INSERT INTO people (id, name) VALUES (%s, %s), (%s, %s)')
    assert cursor.executed[2][1] == tuple(value for row in rows[2000:] for value in row)


def test_mssql_bulk_upsert_merges_many_rows_per_statement():
    cursor = _ExecutingCursor()

    assert MSSQLDialect().bulkUpsert(cursor, 'people', ['id', 'name'], ['id'], ['name'], [(1, 'a'), (2, 'b')]) is True

    ((query, parameters),) = cursor.executed
    assert query.startswith('MERGE INTO people AS target USING (VALUES (%s, %s), (%s, %s)) AS source (id, name) ON (target.id = source.id)')
    assert parameters == (1, 'a', 2, 'b')


def test_only_postgresql_mssql_and_duckdb_have_a_bulk_path():
    for dialect in (MySQLDialect(), MariaDBDialect(), OracleDialect(), SQLiteDialect()):
        assert dialect.bulkInsert(None, 't', ['id'], [(1,)]) is False
        assert dialect.bulkUpsert(None, 't', ['id'], ['id'], [], [(1,)]) is False


def _settings(**overrides):
    from bauta.configuration import connectionConfig

    fields = dict(type='postgresql', user='u', password='secret', database='d', host='h', port=5432)
    fields.update(overrides)
    if fields['type'] in ('sqlite', 'duckdb'):
        fields = {name: value for name, value in fields.items() if name in ('type', 'path', 'options')}
    elif fields['type'] == 'oracle':
        del fields['database']
    return connectionConfig(**fields)


def test_connect_arguments_add_the_options_to_the_fields():
    arguments = PostgreSQLDialect().connectArguments(_settings(options={'sslmode': 'verify-full', 'sslrootcert': '/ca.pem'}))

    assert arguments == {'user': 'u', 'password': 'secret', 'host': 'h', 'dbname': 'd', 'port': 5432,
                         'sslmode': 'verify-full', 'sslrootcert': '/ca.pem'}


def test_an_option_that_duplicates_a_field_is_refused():
    from bauta.configuration import ConfigurationError

    with pytest.raises(ConfigurationError, match='options host, password duplicate'):
        PostgreSQLDialect().connectArguments(_settings(options={'host': 'elsewhere', 'password': 'other', 'sslmode': 'require'}))


def test_each_dialect_maps_the_fields_to_its_drivers_own_argument_names():
    oracle = OracleDialect().connectArguments(_settings(type='oracle', serviceName='svc', options={'protocol': 'tcps'}))
    mssql = MSSQLDialect().connectArguments(_settings(type='mssql', port=None))
    sqlite = SQLiteDialect().connectArguments(_settings(type='sqlite', path='/tmp/x.db', options={'uri': True}))
    mysql = MySQLDialect().connectArguments(_settings(type='mysql', port=3306))
    postgresql = PostgreSQLDialect().connectArguments(_settings())

    assert oracle == {'user': 'u', 'password': 'secret', 'host': 'h', 'port': 5432, 'service_name': 'svc', 'sid': None, 'protocol': 'tcps'}
    assert mssql == {'server': 'h', 'user': 'u', 'password': 'secret', 'database': 'd'}
    assert sqlite == {'database': '/tmp/x.db', 'timeout': 30.0, 'uri': True}
    # psycopg calls it dbname, and mysql.connector refuses that name
    assert mysql == {'user': 'u', 'password': 'secret', 'host': 'h', 'database': 'd', 'port': 3306}
    assert postgresql == {'user': 'u', 'password': 'secret', 'host': 'h', 'dbname': 'd', 'port': 5432}


def test_sqlite_cannot_tell_whether_a_connection_is_encrypted():
    assert SQLiteDialect().isEncrypted(None) is None


@pytest.mark.parametrize('dialect,row,expected', [
    (MySQLDialect(), ('Ssl_cipher', 'TLS_AES_128_GCM_SHA256'), True),
    (MySQLDialect(), ('Ssl_cipher', ''), False),
    (PostgreSQLDialect(), (True,), True),
    (PostgreSQLDialect(), None, None),
    (OracleDialect(), ('tcps',), True),
    (OracleDialect(), ('tcp',), False),
    (MSSQLDialect(), ('TRUE',), True),
    (MSSQLDialect(), ('FALSE',), False),
    ])
def test_encryption_is_read_from_what_the_server_reports(dialect, row, expected):

    class _Cursor(_RecordingCursor):
        def fetchone(self):
            return row

    assert dialect.isEncrypted(_Cursor([])) is expected


@pytest.mark.parametrize('table,expected', [
    ('orders', (None, 'orders')),
    ('sales.orders', ('sales', 'orders')),
    ('"group"', (None, '"group"')),
    ('"sales"."group"', ('"sales"', '"group"')),
    ('dbo.[a.b]', ('dbo', '[a.b]')),
    ('`my db`.`group`', ('`my db`', '`group`')),
    ])
def test_a_table_name_splits_on_the_dot_that_separates_its_parts(table, expected):
    """A dot inside quotes belongs to the name, so `dbo.[a.b]` is one table."""
    assert splitTableName(table) == expected


@pytest.mark.parametrize('databaseType,name,expected', [
    (DatabaseType.POSTGRESQL, 'Orders', 'orders'),
    (DatabaseType.POSTGRESQL, '"Orders"', 'Orders'),
    (DatabaseType.ORACLE, 'orders', 'ORDERS'),
    (DatabaseType.ORACLE, '"group"', 'group'),
    (DatabaseType.MSSQL, '[group]', 'group'),
    (DatabaseType.MYSQL, '`group`', 'group'),
    (DatabaseType.MYSQL, 'Orders', 'Orders'),
    (DatabaseType.SQLITE, '"a""b"', 'a"b'),
    (DatabaseType.DUCKDB, 'Orders', 'Orders'),  # compares names ignoring case, and keeps them as written
    ])
def test_a_name_is_unquoted_and_folded_the_way_its_database_stores_it(databaseType, name, expected):
    """What a catalog lookup binds: quoting a name used to be enough to make
    every lookup of it report nothing.
    """
    assert catalogName(databaseType, name) == expected


@pytest.mark.parametrize('databaseType,table,expected', [
    (DatabaseType.POSTGRESQL, 'group', '"group"'),
    (DatabaseType.POSTGRESQL, '"group"', '"group"'),
    (DatabaseType.MSSQL, 'sales.group', '[sales].[group]'),
    (DatabaseType.ORACLE, '"group"', '"group"'),
    (DatabaseType.MYSQL, '`group`', '`group`'),
    ])
def test_a_table_name_is_quoted_once_however_it_was_written(databaseType, table, expected):
    assert quoteTableName(databaseType, table) == expected


@pytest.mark.parametrize('databaseType,table,expected', [
    (DatabaseType.POSTGRESQL, 'Orders', ('"Orders"', '"orders"')),
    (DatabaseType.ORACLE, 'orders', ('"orders"', '"ORDERS"')),
    (DatabaseType.ORACLE, '"group"', ('"group"', '"group"')),
    ])
def test_a_name_being_created_is_folded_and_a_name_read_from_a_catalog_is_not(databaseType, table, expected):
    """A catalog holds the spelling that answers; a name being created is
    folded so it answers to itself unquoted, as its columns are.
    """

    assert (quoteTableName(databaseType, table), quoteFoldedTable(databaseType, table)) == expected


@pytest.mark.parametrize('databaseType,table,expected', [
    (DatabaseType.POSTGRESQL, 'orders', 'orders_tmp'),
    (DatabaseType.MSSQL, '[group]', '[group_tmp]'),
    (DatabaseType.MSSQL, 'sales.[group]', 'sales.[group_tmp]'),
    (DatabaseType.ORACLE, '"group"', '"group_tmp"'),
    ])
def test_a_suffix_goes_inside_the_quotes_of_a_quoted_name(databaseType, table, expected):
    """`[group]_tmp` is not a name sp_rename can parse, so the swap failed."""
    assert suffixedName(databaseType, table, '_tmp') == expected


def test_mssql_swap_doubles_a_quote_in_a_table_name():
    """sp_rename takes both names as string literals, which a bare quote ended:
    a table named o'brien produced a statement SQL Server couldn't parse.
    """
    query, = MSSQLDialect().swapQueries(targetTable="[o'brien]", stageTable="[o'brien_stage]", tempTable="[o'brien_tmp]")

    assert query == ("EXEC sp_rename '[o''brien_stage]', 'o''brien_tmp'; EXEC sp_rename '[o''brien]', 'o''brien_stage'; "
                     "EXEC sp_rename '[o''brien_tmp]', 'o''brien';")


# COPY: which chunks it is trusted with -------------------------------------------

def _copyableValues():

    class Text(str):
        pass

    class Moment(datetime.datetime):
        pass

    return [
        None, True, 0, -42, 2 ** 70, decimal.Decimal('-1.2500'), decimal.Decimal('1E-10'), 1.5, float('nan'), float('-inf'),
        '', 'tab\there', Text('subclass'), datetime.date(2026, 1, 2), datetime.datetime(2026, 1, 2, 3, 4, 5, 123456),
        Moment(2026, 1, 2), datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc), datetime.time(3, 4, 5),
        uuid.UUID('12345678-1234-5678-1234-567812345678'), b'\x00\xff', bytearray(b'\x01'), memoryview(b'\x03'),
        ]


def test_a_chunk_of_scalar_values_goes_by_copy():
    from unittest.mock import MagicMock

    from bauta.database.dialects.postgresql import _copyIn

    cursor = MagicMock()

    assert _copyIn(cursor, 'COPY t FROM STDIN', [tuple(_copyableValues())]) is True


def test_a_value_copy_is_not_trusted_with_sends_the_chunk_the_other_way_and_sends_nothing():
    """A list may be an array or JSON, and a Jsonb or a duration has no text
    form COPY is trusted to agree on; the driver's adapters decide those.
    """
    from unittest.mock import MagicMock

    from bauta.database.dialects import PostgreSQLDialect
    from bauta.database.dialects.postgresql import _copyIn

    for value in ([1, 2], {'a': 1}, datetime.timedelta(hours=1), object()):
        cursor = MagicMock()
        rows = [(1, 'x'), (2, value)]
        assert _copyIn(cursor, 'COPY t FROM STDIN', rows) is False
        assert PostgreSQLDialect().bulkUpsert(cursor, 't', ['a', 'b'], ['a'], ['b'], rows) is False
        cursor.copy.assert_not_called()
        cursor.execute.assert_not_called()


def test_a_connection_whose_session_setup_fails_is_closed():
    """A failing SET search_path or ALTER SESSION -- a misspelled currentSchema,
    say -- left the connection it ran on open, and each retry opened another.
    """
    from unittest.mock import MagicMock

    from bauta.configuration import connectionConfig

    connection = MagicMock()
    connection.cursor.return_value.execute.side_effect = RuntimeError('schema "nope" does not exist')
    dialect = PostgreSQLDialect()
    dialect.openConnection = lambda settings: connection

    settings = connectionConfig(type='postgresql', host='h', user='u', password='p', database='d', currentSchema='nope')
    with pytest.raises(RuntimeError, match='does not exist'):
        dialect.connect(settings)

    connection.close.assert_called_once()


def test_duckdb_options_are_duckdbs_own_settings():
    from bauta.configuration import connectionConfig

    settings = connectionConfig(type='duckdb', path=':memory:', options={'threads': 2})

    assert DuckDBDialect().connectArguments(settings) == {'database': ':memory:', 'config': {'threads': 2}}


@pytest.mark.parametrize('dataType, expected', [('INTEGER', ColumnCategory.NUMBER), ('DECIMAL(10,2)', ColumnCategory.NUMBER),
                                                ('TIMESTAMP WITH TIME ZONE', ColumnCategory.DATE), ('VARCHAR', ColumnCategory.TEXT),
                                                ('BLOB', None)])
def test_duckdb_column_category_reads_its_type_text(dataType, expected):
    assert DuckDBDialect().columnCategory(dataType) == expected

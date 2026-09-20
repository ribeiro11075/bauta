"""Type mapping and DDL generation, without a server. The cross-database proof
is tests/test_integration_schema.py; these pin the individual decisions.
"""
import sqlite3

import pytest

from bauta.configuration import DatabaseConnectionConfig, DatabaseType
from bauta.database import Database
from bauta.databaseDialects import ColumnDefinition, ForeignKey
from bauta.schema import (PortableType, SchemaError, TableDefinition, clearOrder, clearTables, createStatements, orderParentsFirst,
                                    portableType, readTable, renderScript, renderType)
from bauta.subset import relatedTables

ORACLE = DatabaseType.ORACLE
POSTGRESQL = DatabaseType.POSTGRESQL
MYSQL = DatabaseType.MYSQL
MSSQL = DatabaseType.MSSQL
MARIADB = DatabaseType.MARIADB
SQLITE = DatabaseType.SQLITE


def column(dataType, length=None, precision=None, scale=None, nullable=True, name='c'):
    return ColumnDefinition(name=name, dataType=dataType, length=length, precision=precision, scale=scale, nullable=nullable)


@pytest.mark.parametrize('source,definition,kind', [
    (ORACLE, column('NUMBER', precision=None, scale=0), 'bigint'),
    (ORACLE, column('NUMBER', precision=5, scale=0), 'integer'),
    (ORACLE, column('NUMBER', precision=12, scale=2), 'decimal'),
    (ORACLE, column('NUMBER'), 'decimal'),
    (ORACLE, column('DATE'), 'timestamp'),
    (ORACLE, column('TIMESTAMP(6) WITH TIME ZONE'), 'timestampTz'),
    (ORACLE, column('CLOB'), 'text'),
    (POSTGRESQL, column('character varying', length=40), 'text'),
    (POSTGRESQL, column('timestamp with time zone'), 'timestampTz'),
    (POSTGRESQL, column('boolean'), 'boolean'),
    (POSTGRESQL, column('uuid'), 'uuid'),
    (POSTGRESQL, column('jsonb'), 'json'),
    (MYSQL, column('tinyint'), 'smallint'),
    (MYSQL, column('bit'), 'bigint'),
    (MYSQL, column('longtext', length=4294967295), 'text'),
    (MSSQL, column('bit'), 'boolean'),
    (MSSQL, column('uniqueidentifier'), 'uuid'),
    (MSSQL, column('money'), 'decimal'),
    (SQLITE, column('VARCHAR', length=40), 'text'),
    (SQLITE, column('BOOLEAN'), 'smallint'),
    (SQLITE, column('DATETIME'), 'timestamp'),
    (SQLITE, column('DECIMAL', precision=10, scale=2), 'decimal'),
    (SQLITE, column('BIGINT'), 'bigint'),
    ])
def test_source_types_map_to_portable_ones(source, definition, kind):
    assert portableType(source, definition).kind == kind


def test_unbounded_text_stays_unbounded():
    assert portableType(MYSQL, column('text', length=65535)).length is None
    assert portableType(MSSQL, column('nvarchar', length=-1)).length is None


def test_booleans_stored_as_integers_stay_integers():
    """PostgreSQL refuses an integer in a BOOLEAN column, so only sources whose
    drivers return real booleans produce one.
    """
    for source, name in ((SQLITE, 'BOOLEAN'), (MYSQL, 'boolean'), (MYSQL, 'bit')):
        portable = portableType(source, column(name))
        assert portable.kind != 'boolean'
        assert portable.note


def test_an_unknown_type_becomes_text_with_a_note():
    portable = portableType(POSTGRESQL, column('tsvector'))

    assert portable.kind == 'text'
    assert 'tsvector' in portable.note


@pytest.mark.parametrize('target,portable,expected', [
    (POSTGRESQL, PortableType('decimal', precision=12, scale=2), 'NUMERIC(12,2)'),
    (POSTGRESQL, PortableType('decimal'), 'NUMERIC'),
    (MYSQL, PortableType('decimal', precision=70, scale=40), 'DECIMAL(65,30)'),
    (MYSQL, PortableType('text'), 'LONGTEXT'),
    (MYSQL, PortableType('text', length=40), 'VARCHAR(40)'),
    (MSSQL, PortableType('text', length=5000), 'NVARCHAR(MAX)'),
    (MSSQL, PortableType('boolean'), 'BIT'),
    (ORACLE, PortableType('text', length=40), 'VARCHAR2(40 CHAR)'),
    (ORACLE, PortableType('text'), 'CLOB'),
    (ORACLE, PortableType('bigint'), 'NUMBER(19)'),
    (SQLITE, PortableType('float'), 'REAL'),
    ])
def test_portable_types_render_per_target(target, portable, expected):
    assert renderType(target, portable, isKey=False)[0] == expected


@pytest.mark.parametrize('target,expected', [(MYSQL, 'VARCHAR(255) COLLATE utf8mb4_0900_bin'), (MSSQL, 'NVARCHAR(255) COLLATE Latin1_General_BIN2'),
                                             (ORACLE, 'VARCHAR2(255 CHAR)'), (POSTGRESQL, 'TEXT'), (SQLITE, 'TEXT')])
def test_unbounded_text_in_a_key_is_bounded_where_the_target_requires_it(target, expected):
    rendered, note = renderType(target, PortableType('text'), isKey=True)

    assert rendered == expected
    assert (note is not None) == (not expected.startswith('TEXT'))


@pytest.mark.parametrize('target,expected', [
    (MYSQL, 'VARCHAR(10) COLLATE utf8mb4_0900_bin'),
    (MARIADB, 'VARCHAR(10) COLLATE utf8mb4_nopad_bin'),
    (MSSQL, 'NVARCHAR(10) COLLATE Latin1_General_BIN2'),
    (POSTGRESQL, 'VARCHAR(10)'),
    (ORACLE, 'VARCHAR2(10 CHAR)'),
    (SQLITE, 'VARCHAR(10)'),
    ])
def test_a_text_key_is_collated_to_compare_exactly_where_the_default_would_not(target, expected):
    """Their defaults compare `a` and `A` -- and `ss` and the German sharp s --
    as one value, so two keys the source keeps apart became one row in the copy,
    or a primary-key violation once a chunk held both.
    """

    assert renderType(target, PortableType('text', length=10), isKey=True)[0] == expected
    assert renderType(target, PortableType('text', length=10), isKey=False)[0] == expected.split(' COLLATE ')[0]


def test_lossy_renderings_say_so():
    assert renderType(ORACLE, PortableType('time'), isKey=False)[1]
    assert renderType(MYSQL, PortableType('timestampTz'), isKey=False)[1]


def test_a_time_zone_aware_timestamp_into_oracle_says_the_instant_can_move():
    """Oracle keeps the loading session's offset, so 12:00+02 came back as
    10:00-04 -- a different instant, and nothing said so.
    """
    rendered, note = renderType(ORACLE, PortableType('timestampTz'), isKey=False)

    assert rendered == 'TIMESTAMP WITH TIME ZONE'
    assert 'instant moves' in note


def test_a_time_into_oracle_is_wide_enough_for_a_day_long_one():
    """MySQL's driver returns a TIME as a timedelta, and its text runs to
    '-35 days, 1:00:01.999999'; VARCHAR2(16 CHAR) refused it.
    """
    rendered, note = renderType(ORACLE, PortableType('time'), isKey=False)

    assert rendered == 'VARCHAR2(32 CHAR)' and note
    assert len('-35 days, 1:00:01.999999') <= 32


@pytest.mark.parametrize('target,expected', [(MYSQL, 'DECIMAL(65,30)'), (MSSQL, 'DECIMAL(38,10)')])
def test_a_decimal_of_no_declared_size_says_what_it_was_given(target, expected):
    rendered, note = renderType(target, PortableType('decimal'), isKey=False)

    assert rendered == expected
    assert expected in note

    assert renderType(POSTGRESQL, PortableType('decimal'), isKey=False) == ('NUMERIC', None)


@pytest.mark.parametrize('target,expected', [(MYSQL, 'DECIMAL(65,30)'), (MSSQL, 'DECIMAL(38,30)'), (ORACLE, 'NUMBER(38,30)')])
def test_a_decimal_wider_than_the_target_says_what_it_was_clamped_to(target, expected):
    rendered, note = renderType(target, PortableType('decimal', precision=70, scale=30), isKey=False)

    assert rendered == expected
    assert 'clamped to {}'.format(expected) in note


@pytest.mark.parametrize('target,expected', [(MYSQL, 'INT'), (POSTGRESQL, 'INTEGER'), (ORACLE, 'NUMBER(10)'), (SQLITE, 'INTEGER')])
def test_sqlites_64_bit_integer_says_where_it_does_not_fit(target, expected):
    """Every SQLite INTEGER holds 64 bits, whatever its column says, so only
    SQLite itself takes them all back.
    """
    rendered, note = renderType(target, portableType(SQLITE, column('INTEGER')), isKey=False)

    assert rendered == expected
    assert (note is None) == (target == SQLITE)


@pytest.mark.parametrize('dataType,rendered,digits', [('bigint unsigned', 'BIGINT', '20 digits'), ('int unsigned', 'INTEGER', '10 digits')])
def test_an_unsigned_mysql_integer_says_its_values_will_not_load(dataType, rendered, digits):
    """MySQL reports `unsigned` only in column_type, which is why the catalog
    query reads that rather than data_type: an INT UNSIGNED reaches 4294967295
    and looked exactly like an INT.
    """
    renderedType, note = renderType(POSTGRESQL, portableType(MYSQL, column(dataType, precision=20, scale=0)), isKey=False)

    assert renderedType == rendered
    assert digits in note
    assert renderType(POSTGRESQL, portableType(MYSQL, column('bigint', precision=19, scale=0)), isKey=False)[1] is None


def table(name, columns, primaryKey=(), foreignKeys=()):
    return TableDefinition(name=name, columns=[column(dataType, name=columnName, nullable=nullable) for columnName, dataType, nullable in columns],
                           primaryKey=list(primaryKey), foreignKeys=list(foreignKeys))


CUSTOMERS = table('customers', [('id', 'integer', False), ('email', 'text', True)], primaryKey=['id'])
ORDERS = table('orders', [('id', 'integer', False), ('customer_id', 'integer', True)], primaryKey=['id'],
               foreignKeys=[ForeignKey('orders', ('customer_id',), 'customers', ('id',), 'orders_customer_id_fkey')])


def test_statements_create_parents_first_with_keys_and_constraints():
    statements = createStatements(POSTGRESQL, MSSQL, [ORDERS, CUSTOMERS])

    assert [statement.table for statement in statements] == ['customers', 'orders']
    assert '[id] INT NOT NULL' in statements[0].sql
    assert '[email] NVARCHAR(MAX)' in statements[0].sql
    assert 'PRIMARY KEY ([id])' in statements[0].sql
    assert 'CONSTRAINT orders_customer_id_fkey FOREIGN KEY ([customer_id]) REFERENCES [customers] ([id])' in statements[1].sql


@pytest.mark.parametrize('target,expected', [
    (DatabaseType.ORACLE, '"RANK" NUMBER(10)'), (POSTGRESQL, '"rank" INTEGER'), (DatabaseType.MYSQL, '`Rank` INT'), (MSSQL, '[Rank] INT'),
    ])
def test_column_names_are_quoted_as_the_target_would_store_them_unquoted(target, expected):
    """So a reserved word works, and the column still answers to its unquoted name."""
    definition = table('scores', [('Rank', 'integer', True)], primaryKey=[])

    assert expected in createStatements(POSTGRESQL, target, [definition])[0].sql


def test_a_foreign_key_to_a_table_not_being_created_is_left_out_and_noted():
    [statement] = createStatements(POSTGRESQL, POSTGRESQL, [ORDERS])

    assert 'FOREIGN KEY' not in statement.sql
    assert any('customers is not being created' in note for note in statement.notes)


def test_foreign_keys_can_be_left_out():
    statements = createStatements(POSTGRESQL, POSTGRESQL, [CUSTOMERS, ORDERS], includeForeignKeys=False)

    assert all('FOREIGN KEY' not in statement.sql for statement in statements)


def test_stage_tables_have_the_key_but_no_foreign_keys():
    """They cannot carry them: a swap of the parent redirects the key to the
    emptied old table, and every row is then refused.
    """
    statements = createStatements(POSTGRESQL, POSTGRESQL, [CUSTOMERS, ORDERS], stageSuffix='_stage')

    assert [statement.table for statement in statements] == ['customers', 'customers_stage', 'orders', 'orders_stage']
    assert 'PRIMARY KEY ("id")' in statements[3].sql
    assert 'CONSTRAINT orders_customer_id_fkey FOREIGN KEY ("customer_id") REFERENCES "customers" ("id")' in statements[2].sql
    assert 'FOREIGN KEY' not in statements[3].sql


def test_a_primary_key_column_is_never_nullable():
    definition = table('t', [('id', 'integer', True)], primaryKey=['id'])

    assert '"id" INTEGER NOT NULL' in createStatements(POSTGRESQL, POSTGRESQL, [definition])[0].sql


def test_two_tables_with_the_same_constraint_name_get_different_ones():
    """Constraint names are per table on PostgreSQL and SQLite, but unique
    across the schema on the other four, where the second CREATE TABLE would
    fail and leave half a schema behind.
    """
    items = table('items', [('id', 'integer', False), ('customer_id', 'integer', True)], primaryKey=['id'],
                  foreignKeys=[ForeignKey('items', ('customer_id',), 'customers', ('id',), 'fk_parent')])
    orders = ORDERS._replace(foreignKeys=[ForeignKey('orders', ('customer_id',), 'customers', ('id',), 'fk_parent')])

    statements = createStatements(POSTGRESQL, MYSQL, [CUSTOMERS, orders, items])
    names = [statement.sql.split('CONSTRAINT ')[1].split(' ')[0] for statement in statements if 'CONSTRAINT' in statement.sql]

    assert sorted(names) == ['fk_parent', 'fk_parent_2']


def test_long_constraint_names_that_share_a_prefix_stay_different():
    """Cut to the length limit, two names that differ past it become one."""
    shared = 'fk_' + 'a' * 70
    definition = ORDERS._replace(foreignKeys=[ForeignKey('orders', ('customer_id',), 'customers', ('id',), shared + '_one'),
                                              ForeignKey('orders', ('id',), 'customers', ('id',), shared + '_two')])

    sql = createStatements(POSTGRESQL, ORACLE, [CUSTOMERS, definition])[1].sql
    names = [part.split(' ')[0] for part in sql.split('CONSTRAINT ')[1:]]

    assert len(set(names)) == 2
    assert all(len(name) <= 63 for name in names)


def test_a_key_to_a_column_the_primary_key_does_not_cover_brings_a_unique_constraint():
    """Every dialect needs a unique constraint behind a foreign key, and
    `subset --root products` makes exactly this shape.
    """
    products = table('products', [('id', 'integer', False), ('sku', 'text', False)], primaryKey=['id'])
    aliases = table('sku_aliases', [('alias', 'text', False), ('sku', 'text', False)], primaryKey=['alias'],
                    foreignKeys=[ForeignKey('sku_aliases', ('sku',), 'products', ('sku',), 'fk_sku')])

    statements = createStatements(POSTGRESQL, POSTGRESQL, [aliases, products], stageSuffix='_stage')

    assert [statement.table for statement in statements] == ['products', 'products_stage', 'sku_aliases', 'sku_aliases_stage']
    assert 'UNIQUE ("sku")' in statements[0].sql
    assert all('UNIQUE' not in statement.sql for statement in statements[1:])
    assert 'UNIQUE' not in createStatements(POSTGRESQL, POSTGRESQL, [products])[0].sql


def test_a_key_to_the_primary_key_needs_no_unique_constraint():
    assert 'UNIQUE' not in createStatements(POSTGRESQL, POSTGRESQL, [CUSTOMERS, ORDERS])[0].sql


def test_constraint_names_are_made_safe_for_every_dialect():
    foreignKey = ForeignKey('orders', ('customer_id',), 'customers', ('id',), '1 weird-name' + 'x' * 80)
    definition = ORDERS._replace(foreignKeys=[foreignKey])

    sql = createStatements(POSTGRESQL, ORACLE, [CUSTOMERS, definition])[1].sql
    name = sql.split('CONSTRAINT ')[1].split(' ')[0]

    assert name.startswith('fk_1_weird_name')
    assert len(name) == 63


def test_the_script_puts_notes_above_their_table():
    script = renderScript(createStatements(POSTGRESQL, ORACLE, [table('t', [('at', 'time', True)])]), ['heading'])

    assert script.startswith('-- heading\n\n-- at: Oracle has no TIME type')
    assert script.rstrip().endswith(');')


def test_cycles_are_reported_but_self_references_are_not():
    selfReference = [ForeignKey('employees', ('manager_id',), 'employees', ('id',), 'fk')]
    assert orderParentsFirst(['employees'], selfReference) == ['employees']

    cycle = [ForeignKey('a', ('b_id',), 'b', ('id',), 'fk1'), ForeignKey('b', ('a_id',), 'a', ('id',), 'fk2')]
    with pytest.raises(SchemaError, match='cycle among: a, b'):
        orderParentsFirst(['a', 'b'], cycle)


def test_the_cycle_message_fits_every_command_that_orders_tables():
    """`synthesize` and `clear` raise it too, over tables that already exist,
    and neither has the --no-foreign-keys the message used to offer.
    """
    cycle = [ForeignKey('a', ('b_id',), 'b', ('id',), 'fk1'), ForeignKey('b', ('a_id',), 'a', ('id',), 'fk2')]

    with pytest.raises(SchemaError) as raised:
        clearOrder(['a', 'b'], cycle)

    assert '--' not in str(raised.value)


def test_clear_order_is_children_first_and_case_insensitive():
    foreignKeys = [ForeignKey('ORDERS', ('CUSTOMER_ID',), 'CUSTOMERS', ('ID',), 'fk'),
                   ForeignKey('ITEMS', ('ORDER_ID',), 'ORDERS', ('ID',), 'fk2')]

    assert clearOrder(['customers', 'items', 'orders'], foreignKeys) == ['items', 'orders', 'customers']


def test_related_tables_follow_references_both_ways():
    foreignKeys = [ForeignKey('orders', ('customer_id',), 'customers', ('id',), 'fk1'),
                   ForeignKey('orders', ('region_id',), 'regions', ('id',), 'fk2'),
                   ForeignKey('invoices', ('order_id',), 'orders', ('id',), 'fk3')]

    assert relatedTables(foreignKeys, ['customers']) == ['customers', 'invoices', 'orders', 'regions']
    assert relatedTables(foreignKeys, ['ORDERS'], followChildren=False) == ['customers', 'orders', 'regions']


@pytest.fixture
def sqliteDatabase(tmp_path):
    connection = sqlite3.connect(str(tmp_path / 'schema.db'))
    connection.executescript('''
        CREATE TABLE customers (id INTEGER PRIMARY KEY, email VARCHAR(120) NOT NULL, balance DECIMAL(12, 2), active BOOLEAN, note);
        CREATE TABLE orders (id INT, line INT, customer_id INT REFERENCES customers(id), PRIMARY KEY (line, id));
        ''')
    connection.close()

    with Database(connectionSettings=DatabaseConnectionConfig(type=SQLITE, database=str(tmp_path / 'schema.db'))) as database:
        yield database


def test_sqlite_columns_are_read_from_their_declared_types(sqliteDatabase):
    columns = {definition.name: definition for definition in sqliteDatabase.getColumnDefinitions('customers')}

    assert columns['email'] == ColumnDefinition('email', 'VARCHAR', 120, None, None, False)
    assert columns['balance'] == ColumnDefinition('balance', 'DECIMAL', None, 12, 2, True)
    assert columns['id'].nullable is False
    assert columns['note'].dataType == ''


def test_the_defined_primary_key_keeps_its_declared_order(sqliteDatabase):
    assert sqliteDatabase.getPrimaryColumnNames('orders') == ['line', 'id']


def test_table_existence_is_case_insensitive_on_sqlite(sqliteDatabase):
    assert sqliteDatabase.tableExists('CUSTOMERS')
    assert not sqliteDatabase.tableExists('suppliers')


def test_read_table_carries_its_foreign_keys(sqliteDatabase):
    definition = readTable(sqliteDatabase, 'orders', sqliteDatabase.getForeignKeys())

    assert definition.primaryKey == ['line', 'id']
    assert [foreignKey.referencedTable for foreignKey in definition.foreignKeys] == ['customers']


def test_read_table_names_the_table_as_it_was_asked_for(sqliteDatabase):
    """Taking the name from a matching foreign key instead spelled a table in
    a key the way the catalog holds it -- upper case on Oracle -- and one
    without any the way it was asked for, in the same invocation.
    """
    keyed = readTable(sqliteDatabase, 'ORDERS', sqliteDatabase.getForeignKeys())
    unkeyed = readTable(sqliteDatabase, 'customers', sqliteDatabase.getForeignKeys())

    assert (keyed.name, unkeyed.name) == ('ORDERS', 'customers')


def test_a_table_named_for_a_reserved_word_is_created_quoted():
    """`CREATE TABLE group (...)` is a syntax error on every dialect, and
    `--apply` failed part-way through a set after creating the tables before it.
    """
    group = table('group', [('id', 'integer', False)], primaryKey=['id'])
    lines = table('lines', [('id', 'integer', False), ('group_id', 'integer', True)], primaryKey=['id'],
                  foreignKeys=[ForeignKey('lines', ('group_id',), 'group', ('id',), 'fk_lines')])

    statements = createStatements(POSTGRESQL, POSTGRESQL, [group, lines])

    assert statements[0].sql.startswith('CREATE TABLE "group" (')
    assert 'REFERENCES "group" ("id")' in statements[1].sql


def test_a_name_the_target_would_cut_short_is_noted():
    """PostgreSQL keeps 63 bytes and says nothing, so two tables alike up to
    there became one, and the second job loaded over the first.
    """
    long = table('a' * 64, [('id', 'integer', False)], primaryKey=['id'])

    (statement,) = createStatements(MYSQL, POSTGRESQL, [long])

    assert statement.notes == ['name: {} is 64 bytes long, and postgresql keeps only 63, '
                               'so it names whatever other table shares its first 63'.format('a' * 64)]
    assert createStatements(MYSQL, DatabaseType.SQLITE, [long])[0].notes == []


def test_a_table_a_person_named_in_quotes_keeps_that_spelling():
    """Quoting is the only way to name a lower-case table on Oracle, so folding
    it would create a different table than the one asked for.
    """
    quoted = table('"group"', [('id', 'integer', False)], primaryKey=['id'])

    assert createStatements(POSTGRESQL, ORACLE, [quoted])[0].sql.startswith('CREATE TABLE "group" (')


def test_a_foreign_key_references_the_parent_by_the_name_it_is_created_under():
    """The catalog's spelling of the key's tables need not be the spelling the
    tables are created under; MySQL would not find the other one.
    """
    orders = ORDERS._replace(foreignKeys=[ForeignKey('ORDERS', ('customer_id',), 'CUSTOMERS', ('id',), 'fk')])

    assert 'REFERENCES `customers` (`id`)' in createStatements(POSTGRESQL, MYSQL, [CUSTOMERS, orders])[1].sql


def test_read_table_rejects_a_missing_table(sqliteDatabase):
    with pytest.raises(SchemaError, match='not found'):
        readTable(sqliteDatabase, 'suppliers', [])


def test_generated_sqlite_ddl_round_trips(sqliteDatabase, tmp_path):
    definitions = [readTable(sqliteDatabase, name, sqliteDatabase.getForeignKeys()) for name in ('orders', 'customers')]

    with Database(connectionSettings=DatabaseConnectionConfig(type=SQLITE, database=str(tmp_path / 'copy.db'))) as copy:
        for statement in createStatements(SQLITE, SQLITE, definitions):
            copy.alter(statement.sql)

        assert copy.getPrimaryColumnNames('orders') == ['line', 'id']
        assert copy.getForeignKeys() == sqliteDatabase.getForeignKeys()
        assert [definition.name for definition in copy.getColumnDefinitions('customers')] == ['id', 'email', 'balance', 'active', 'note']


def test_clear_tables_empties_children_first(sqliteDatabase):
    sqliteDatabase.alter('PRAGMA foreign_keys = ON')
    sqliteDatabase.insert(table='customers', data=[(1, 'a@b.c', 1, 1, None)])
    sqliteDatabase.insert(table='orders', data=[(1, 1, 1), (2, 1, 1)])

    assert clearTables(sqliteDatabase, ['customers', 'orders']) == [('orders', 2), ('customers', 1)]

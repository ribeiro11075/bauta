"""A disposable copy of a realistic, awkward schema on all six databases, for
exploratory testing -- by hand, or by agents looking for ways to break bauta.

    python tests/sandbox.py create NAME DIR    # build it, write DIR/configuration/database.yaml
    python tests/sandbox.py drop NAME DIR      # remove it again

Each sandbox has its own namespace on every server (a database on MySQL,
MariaDB and SQL Server, a schema on PostgreSQL and Oracle), so several can run
at once. database.yaml gets two aliases per database: `<db>` holds the schema
and its rows, `<db>_copy` is an empty namespace to copy into. SQLite's are
files in DIR.

The schema has the shapes that break data tools: composite keys, a table that
references itself, two tables that reference each other, two paths to the same
parent, a key to a UNIQUE column that isn't the primary key, a table without a
primary key, a chain of 18 tables, reserved-word names, values at the edges of
each type, and rows that tempt a partial load -- recent orders from customers
who haven't changed in years. See TABLES for the list.
"""
import datetime
import decimal
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bauta.configuration import DatabaseConnectionConfig, DatabaseType  # noqa: E402
from bauta.database import Database  # noqa: E402
from bauta.databaseDialects import quoteIdentifier  # noqa: E402
from servers import SERVERS  # noqa: E402

SEED = 20260919
CHAIN = 18

# Per dialect: int, bigint, decimal (with {p},{s}), float, varchar ({n}), char ({n}), long text, date, timestamp, boolean, binary.
TYPES = {
    DatabaseType.SQLITE: ('INTEGER', 'BIGINT', 'DECIMAL({p},{s})', 'REAL', 'VARCHAR({n})', 'CHAR({n})', 'TEXT', 'DATE', 'DATETIME', 'BOOLEAN',
                          'BLOB'),
    DatabaseType.MYSQL: ('INT', 'BIGINT', 'DECIMAL({p},{s})', 'DOUBLE', 'VARCHAR({n})', 'CHAR({n})', 'LONGTEXT', 'DATE', 'DATETIME(6)',
                         'BOOLEAN', 'BLOB'),
    DatabaseType.MARIADB: ('INT', 'BIGINT', 'DECIMAL({p},{s})', 'DOUBLE', 'VARCHAR({n})', 'CHAR({n})', 'LONGTEXT', 'DATE', 'DATETIME(6)',
                           'BOOLEAN', 'BLOB'),
    DatabaseType.POSTGRESQL: ('INTEGER', 'BIGINT', 'NUMERIC({p},{s})', 'DOUBLE PRECISION', 'VARCHAR({n})', 'CHAR({n})', 'TEXT', 'DATE',
                              'TIMESTAMP(6)', 'BOOLEAN', 'BYTEA'),
    DatabaseType.ORACLE: ('NUMBER(10)', 'NUMBER(19)', 'NUMBER({p},{s})', 'BINARY_DOUBLE', 'VARCHAR2({n} CHAR)', 'CHAR({n})', 'CLOB', 'DATE',
                          'TIMESTAMP(6)', 'NUMBER(1)', 'BLOB'),
    DatabaseType.MSSQL: ('INT', 'BIGINT', 'DECIMAL({p},{s})', 'FLOAT', 'NVARCHAR({n})', 'NCHAR({n})', 'NVARCHAR(MAX)', 'DATE', 'DATETIME2(6)',
                         'BIT', 'VARBINARY(MAX)'),
    }

TABLES = """
regions         composite primary key (country, code)
customers       self-reference (referred_by), composite key to regions, UNIQUE email, updated_at half years old
products        UNIQUE sku, referenced by sku_aliases
orders          customer_id NOT NULL, composite shipping region; many recent orders of long-unchanged customers
order_items     composite primary key that includes a foreign key
payments        a second path to customers (payments -> orders -> customers, and payments -> customers)
departments     cycle with employees (departments.head_id <-> employees.dept_id)
employees       self-reference (manager_id), and the cycle above
sku_aliases     foreign key to products.sku, a UNIQUE column that isn't the primary key
audit_log       no primary key, duplicate rows
events          100 rows share one updated_at, for watermark boundaries; seq is an increasing integer
chain_01..18    each references the one before; deeper than subset follows
type_zoo        one row per edge value of each type
"group"         reserved-word table and columns ("order" references orders, "select")
"""


def _types(databaseType):
    names = ('int', 'bigint', 'decimal', 'float', 'varchar', 'char', 'text', 'date', 'timestamp', 'bool', 'binary')
    return dict(zip(names, TYPES[databaseType]))


def schemaStatements(databaseType):
    """CREATE TABLE statements, parents first; the cycle's second key comes last."""

    t = _types(databaseType)
    q = lambda name: quoteIdentifier(databaseType, name)  # noqa: E731
    dec = lambda p, s: t['decimal'].format(p=p, s=s)  # noqa: E731
    vc = lambda n: t['varchar'].format(n=n)  # noqa: E731
    ch = lambda n: t['char'].format(n=n)  # noqa: E731
    sqlite = databaseType == DatabaseType.SQLITE

    statements = [
        'CREATE TABLE regions (country {} NOT NULL, code {} NOT NULL, name {}, PRIMARY KEY (country, code))'.format(ch(2), vc(10), vc(80)),
        'CREATE TABLE customers (id {int} NOT NULL PRIMARY KEY, email {v120} NOT NULL, name {v200}, region_country {c2}, region_code {v10}, '
        'referred_by {int}, created_at {ts} NOT NULL, updated_at {ts} NOT NULL, balance {d12}, active {bool}, notes {text}, '
        'CONSTRAINT uq_customers_email UNIQUE (email), '
        'CONSTRAINT fk_customers_region FOREIGN KEY (region_country, region_code) REFERENCES regions (country, code), '
        'CONSTRAINT fk_customers_referrer FOREIGN KEY (referred_by) REFERENCES customers (id))'.format(
            int=t['int'], v120=vc(120), v200=vc(200), c2=ch(2), v10=vc(10), ts=t['timestamp'], d12=dec(12, 2), bool=t['bool'], text=t['text']),
        'CREATE TABLE products (id {int} NOT NULL PRIMARY KEY, sku {v20} NOT NULL, name {v200}, price {d10}, updated_at {ts} NOT NULL, '
        'CONSTRAINT uq_products_sku UNIQUE (sku))'.format(int=t['int'], v20=vc(20), v200=vc(200), d10=dec(10, 2), ts=t['timestamp']),
        'CREATE TABLE orders (id {int} NOT NULL PRIMARY KEY, customer_id {int} NOT NULL, ship_country {c2}, ship_code {v10}, status {v20}, '
        'ordered_at {ts} NOT NULL, updated_at {ts} NOT NULL, total {d12}, '
        'CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (id), '
        'CONSTRAINT fk_orders_region FOREIGN KEY (ship_country, ship_code) REFERENCES regions (country, code))'.format(
            int=t['int'], c2=ch(2), v10=vc(10), v20=vc(20), ts=t['timestamp'], d12=dec(12, 2)),
        'CREATE TABLE order_items (order_id {int} NOT NULL, line_no {int} NOT NULL, product_id {int} NOT NULL, qty {int} NOT NULL, '
        'PRIMARY KEY (order_id, line_no), '
        'CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES orders (id), '
        'CONSTRAINT fk_items_product FOREIGN KEY (product_id) REFERENCES products (id))'.format(int=t['int']),
        'CREATE TABLE payments (id {int} NOT NULL PRIMARY KEY, order_id {int} NOT NULL, customer_id {int} NOT NULL, amount {d12}, '
        'paid_at {ts} NOT NULL, '
        'CONSTRAINT fk_payments_order FOREIGN KEY (order_id) REFERENCES orders (id), '
        'CONSTRAINT fk_payments_customer FOREIGN KEY (customer_id) REFERENCES customers (id))'.format(
            int=t['int'], d12=dec(12, 2), ts=t['timestamp']),
        'CREATE TABLE departments (id {int} NOT NULL PRIMARY KEY, name {v80}, head_id {int}{sqliteKey})'.format(
            int=t['int'], v80=vc(80),
            sqliteKey=', CONSTRAINT fk_departments_head FOREIGN KEY (head_id) REFERENCES employees (id)' if sqlite else ''),
        'CREATE TABLE employees (id {int} NOT NULL PRIMARY KEY, name {v200}, manager_id {int}, dept_id {int}, hired_on {date}, '
        'CONSTRAINT fk_employees_manager FOREIGN KEY (manager_id) REFERENCES employees (id), '
        'CONSTRAINT fk_employees_dept FOREIGN KEY (dept_id) REFERENCES departments (id))'.format(int=t['int'], v200=vc(200), date=t['date']),
        'CREATE TABLE sku_aliases (alias {v40} NOT NULL PRIMARY KEY, sku {v20} NOT NULL, '
        'CONSTRAINT fk_aliases_sku FOREIGN KEY (sku) REFERENCES products (sku))'.format(v40=vc(40), v20=vc(20)),
        'CREATE TABLE audit_log (event_id {int}, actor {v80}, payload {text}, happened_at {ts})'.format(
            int=t['int'], v80=vc(80), text=t['text'], ts=t['timestamp']),
        'CREATE TABLE events (id {int} NOT NULL PRIMARY KEY, seq {bigint} NOT NULL, kind {v20}, updated_at {ts} NOT NULL)'.format(
            int=t['int'], bigint=t['bigint'], v20=vc(20), ts=t['timestamp']),
        'CREATE TABLE type_zoo (id {int} NOT NULL PRIMARY KEY, big {bigint}, exact {d38}, approx {float}, word {v200}, padded {c10}, '
        'body {text}, day {date}, moment {ts}, flag {bool}, blob_value {binary})'.format(
            int=t['int'], bigint=t['bigint'], d38=dec(38, 10), float=t['float'], v200=vc(200), c10=ch(10), text=t['text'],
            date=t['date'], ts=t['timestamp'], bool=t['bool'], binary=t['binary']),
        'CREATE TABLE {group} (id {int} NOT NULL PRIMARY KEY, {order} {int}, {select} {v40}, '
        'CONSTRAINT fk_group_order FOREIGN KEY ({order}) REFERENCES orders (id))'.format(
            group=q('group'), order=q('order'), select=q('select'), int=t['int'], v40=vc(40)),
        ]

    for n in range(1, CHAIN + 1):
        previous = ', prev_id {int}, CONSTRAINT fk_chain_{n:02d} FOREIGN KEY (prev_id) REFERENCES chain_{p:02d} (id)'.format(
            int=t['int'], n=n, p=n - 1) if n > 1 else ''
        statements.append('CREATE TABLE chain_{:02d} (id {} NOT NULL PRIMARY KEY, label {}{})'.format(n, t['int'], vc(40), previous))

    if not sqlite:
        statements.append('ALTER TABLE departments ADD CONSTRAINT fk_departments_head FOREIGN KEY (head_id) REFERENCES employees (id)')

    return statements


def rows(databaseType):
    """Table -> rows, in load order. Deterministic."""

    rng = random.Random(SEED)
    sqlite = databaseType == DatabaseType.SQLITE

    def ts(value):
        return value.isoformat(sep=' ') if sqlite and value is not None else value

    def day(value):
        return value.isoformat() if sqlite and value is not None else value

    old, recent = datetime.datetime(2020, 3, 1, 9, 0, 0), datetime.datetime(2026, 9, 1, 0, 0, 0)
    names = ['Ana', 'José', 'Zoë', 'Łukasz', '王伟', 'Дмитрий', "O'Brien", 'Émilie 🚀', 'Mary-Jane', 'Nguyễn Văn A']
    regions = [('pt', 'lx', 'Lisboa'), ('pt', 'po', 'Porto'), ('es', 'md', 'Madrid'), ('es', 'bc', 'Barcelona'), ('us', 'ny', 'New York'),
               ('us', 'ca', 'California')]
    data = {'regions': regions}

    customers = []
    for i in range(1, 201):
        region = None if i % 7 == 0 else regions[i % len(regions)][:2]
        referrer = rng.choice([None, rng.randint(1, i - 1)]) if i > 10 else None
        # Customers 1-100 last changed in 2020; 101-200 recently, twenty of them at one instant.
        updated = old + datetime.timedelta(days=i) if i <= 100 else (recent if i % 10 == 0 else recent + datetime.timedelta(minutes=i, microseconds=i))
        balance = [decimal.Decimal('0.00'), decimal.Decimal('-12.50'), decimal.Decimal('9999999999.99'), None][i % 4] if i % 5 == 0 else \
            decimal.Decimal(rng.randint(0, 1000000)) / 100
        notes = [None, '', 'x' * 5000, 'line one\nline two\ttabbed', "quote ' and \" and \\ backslash"][i % 5]
        customers.append((i, 'c{}@example.test'.format(i), '{} {}'.format(names[i % len(names)], i), region[0] if region else None,
                          region[1] if region else None, referrer, ts(old - datetime.timedelta(days=400 - i)), ts(updated), balance, i % 3 != 0, notes))
    data['customers'] = customers

    data['products'] = [(i, 'SKU-{:04d}'.format(i), 'Product {}'.format(i), decimal.Decimal(rng.randint(100, 99999)) / 100,
                         ts(old if i <= 25 else recent + datetime.timedelta(hours=i))) for i in range(1, 51)]

    orders = []
    for i in range(1, 601):
        # Most recent orders go to customers 1-100, whose own rows are old.
        customer = rng.randint(1, 100) if i > 300 else rng.randint(1, 200)
        ship = None if i % 11 == 0 else regions[i % len(regions)][:2]
        placed = (old if i <= 300 else recent) + datetime.timedelta(hours=i)
        orders.append((i, customer, ship[0] if ship else None, ship[1] if ship else None, ['new', 'paid', 'shipped', 'cancelled'][i % 4],
                       ts(placed), ts(placed + datetime.timedelta(minutes=5)), decimal.Decimal(rng.randint(100, 500000)) / 100))
    data['orders'] = orders

    data['order_items'] = [(order[0], line, rng.randint(1, 50), rng.randint(1, 5)) for order in orders for line in range(1, rng.randint(1, 4) + 1)]

    payments = []
    for order in orders[::2]:
        # One in ten is paid by someone other than the orderer: the two paths to customers disagree.
        payer = order[1] if order[0] % 10 else rng.randint(1, 200)
        payments.append((len(payments) + 1, order[0], payer, order[7], order[6]))
    data['payments'] = payments

    data['departments'] = [(i, 'Department {}'.format(i), None) for i in range(1, 6)]
    data['employees'] = [(i, 'Employee {}'.format(i), None if i <= 5 else rng.randint(1, i - 1), (i % 5) + 1,
                          day(datetime.date(2015, 1, 1) + datetime.timedelta(days=i * 30))) for i in range(1, 41)]
    data['sku_aliases'] = [('ALIAS-{}'.format(i), 'SKU-{:04d}'.format(rng.randint(1, 50))) for i in range(1, 31)]

    log = [(i % 50, 'actor{}'.format(i % 7), '{{"n": {}}}'.format(i), ts(recent + datetime.timedelta(seconds=i // 3))) for i in range(300)]
    data['audit_log'] = log + log[:20]

    instant = recent + datetime.timedelta(days=1)
    data['events'] = [(i, 1000 + i, ['created', 'updated', 'deleted'][i % 3],
                       ts(instant if 100 < i <= 200 else instant + datetime.timedelta(seconds=i - 100 if i > 200 else i - 200)))
                      for i in range(1, 301)]

    zoo = [
        (1, 9223372036854775807, decimal.Decimal('9999999999999999999999999999.9999999999'), 1e308, 'emoji 🚀👩‍👩‍👧 and 中文', 'pad',
         'tab\tnewline\ncarriage\rnull-ish', datetime.date(1000, 1, 1), datetime.datetime(2026, 9, 19, 23, 59, 59, 999999), True, b'\x00\x01\xff'),
        (2, -9223372036854775808, decimal.Decimal('-0.0000000001'), -0.0, '', '', '', datetime.date(9999, 12, 31),
         datetime.datetime(1970, 1, 1, 0, 0, 0), False, b''),
        (3, 0, decimal.Decimal('0'), 1e-300, ' leading and trailing ', 'x' * 10, 'a' * 100000, datetime.date(2000, 2, 29),
         datetime.datetime(2000, 2, 29, 12, 0, 0, 1), None, None),
        (4, None, None, None, None, None, None, None, None, None, None),
        (5, 1, decimal.Decimal('123.4500000000'), 3.141592653589793, "O'Reilly; DROP TABLE type_zoo; --", 'ÀÉÎ', 'ü' * 5000,
         datetime.date(1970, 1, 1), datetime.datetime(2038, 1, 19, 3, 14, 8), True, bytes(range(256))),
        ]
    if sqlite:
        zoo = [row[:7] + (day(row[7]), ts(row[8])) + row[9:] for row in zoo]
    if databaseType == DatabaseType.MSSQL:
        # pymssql sends b'' as '', which SQL Server won't convert to VARBINARY:
        # a known bauta bug, found building this. NULL stands in for it here.
        zoo = [row[:10] + (None if row[10] == b'' else row[10],) for row in zoo]
    if databaseType == DatabaseType.ORACLE:
        # oracledb binds a float as NUMBER, whose range BINARY_DOUBLE's exceeds:
        # 1e308 and 1e-300 fail to load. A known bauta bug; in-range values here.
        zoo = [row[:3] + ({1e308: 1e125, 1e-300: 1e-130}.get(row[3], row[3]),) + row[4:] for row in zoo]
    data['type_zoo'] = zoo

    data['group'] = [(i, i * 3, ['select', 'from', 'where'][i % 3]) for i in range(1, 21)]
    for n in range(1, CHAIN + 1):
        data['chain_{:02d}'.format(n)] = [(i, 'link {}.{}'.format(n, i)) + ((i if i < 5 else None,) if n > 1 else ()) for i in range(1, 6)]

    return data


def _namespace(name, server):
    return 'sb_{}'.format(name) if server != 'oracle' else 'SB_{}'.format(name.upper())


def _admin(server):
    driver, settings = SERVERS[server]
    database = Database(connectionSettings=settings)
    if server == 'mssql':
        database.connection.autocommit(True)
    return database


def _create(server, namespace):
    with _admin(server) as database:
        statement = {
            'mysql': 'CREATE DATABASE {0} CHARACTER SET utf8mb4 COLLATE utf8mb4_bin',
            'mariadb': 'CREATE DATABASE {0} CHARACTER SET utf8mb4 COLLATE utf8mb4_bin',
            'postgresql': 'CREATE SCHEMA {0}',
            'oracle': 'CREATE USER {0} IDENTIFIED BY "Pw{0}" QUOTA UNLIMITED ON USERS',
            'mssql': 'CREATE DATABASE {0}',
            }[server]
        database.alter(statement.format(namespace))


def _drop(server, namespace, quiet=False):
    with _admin(server) as database:
        statements = {
            'mysql': ['DROP DATABASE IF EXISTS {0}'],
            'mariadb': ['DROP DATABASE IF EXISTS {0}'],
            'postgresql': ['DROP SCHEMA IF EXISTS {0} CASCADE'],
            'oracle': ['DROP USER {0} CASCADE'],
            'mssql': ["IF DB_ID('{0}') IS NOT NULL ALTER DATABASE {0} SET SINGLE_USER WITH ROLLBACK IMMEDIATE", 'DROP DATABASE IF EXISTS {0}'],
            }[server]
        for statement in statements:
            try:
                database.alter(statement.format(namespace))
            except Exception as error:
                if not quiet:
                    print('  {}: {}'.format(server, error))


def _settings(server, namespace):
    _, settings = SERVERS[server]
    if server in ('mysql', 'mariadb', 'mssql'):
        return settings.model_copy(update={'database': namespace})
    return settings.model_copy(update={'currentSchema': namespace})


def _yaml(alias, settings):
    lines = ['{}:'.format(alias), '  type: {}'.format(settings.type.value), '  database: {}'.format(settings.database)]
    if settings.type != DatabaseType.SQLITE:
        lines += ['  host: {}'.format(settings.host), '  port: {}'.format(settings.port), '  user: {}'.format(settings.user),
                  "  password: '{}'".format(settings.password.get_secret_value())]
    for field in ('serviceName', 'currentSchema'):
        if getattr(settings, field, None):
            lines.append('  {}: {}'.format(field, getattr(settings, field)))
    return '\n'.join(lines)


def _populate(settings):
    with Database(connectionSettings=settings) as database:
        for statement in schemaStatements(settings.type):
            database.alter(statement)
        data = rows(settings.type)
        for table in ('regions', 'customers', 'products', 'orders', 'order_items', 'payments', 'departments', 'employees', 'sku_aliases',
                      'audit_log', 'events', 'type_zoo', 'group') + tuple('chain_{:02d}'.format(n) for n in range(1, CHAIN + 1)):
            name = quoteIdentifier(settings.type, 'group') if table == 'group' else table
            database.insert(table=name, data=data[table], chunkSize=500)
        for department in range(1, 6):
            database.alter('UPDATE departments SET head_id = {0} WHERE id = {0}'.format(department + 5))


def create(name, directory):
    directory = Path(directory).resolve()
    (directory / 'configuration').mkdir(parents=True, exist_ok=True)
    aliases = []

    for suffix in ('', '_copy'):
        path = directory / 'sqlite{}.db'.format(suffix)
        for leftover in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm')):
            leftover.unlink(missing_ok=True)
        sqlite = DatabaseConnectionConfig(type=DatabaseType.SQLITE, database=str(path))
        if not suffix:
            _populate(sqlite)
        aliases.append(_yaml('sqlite' + suffix, sqlite))

    for server in sorted(SERVERS):
        for suffix in ('', '_copy'):
            namespace = _namespace(name + suffix, server)
            _drop(server, namespace, quiet=True)
            _create(server, namespace)
            settings = _settings(server, namespace)
            if not suffix:
                _populate(settings)
            aliases.append(_yaml(server + suffix, settings))
        print('  {}: ready'.format(server))

    (directory / 'configuration' / 'database.yaml').write_text('\n\n'.join(aliases) + '\n')
    print('sandbox {} ready: {}'.format(name, directory / 'configuration' / 'database.yaml'))


def drop(name, directory):
    for server in sorted(SERVERS):
        for suffix in ('', '_copy'):
            _drop(server, _namespace(name + suffix, server))
    for suffix in ('', '_copy'):
        Path(directory, 'sqlite{}.db'.format(suffix)).unlink(missing_ok=True)
    print('sandbox {} dropped'.format(name))


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] not in ('create', 'drop'):
        sys.exit(__doc__)
    {'create': create, 'drop': drop}[sys.argv[1]](sys.argv[2], sys.argv[3])

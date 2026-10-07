"""Which tables a query reads rows from, as `coverage` counts them."""
import pytest

from bauta.review.queryTables import tablesRead


@pytest.mark.parametrize('query, tables', [
    ('select id from orders -- users table no longer copied', {'ORDERS'}),
    ("select id from orders where kind = 'users'", {'ORDERS'}),
    ('select * from orders /* from users */ where id > {{ watermark }}', {'ORDERS'}),
    ('select o.id from orders o where o.uid in (select id from users)', {'ORDERS'}),
    ('select * from orders where exists (select 1 from users u where u.id = orders.uid)', {'ORDERS'}),
    ('select (select max(x) from users) as m, id from orders', {'ORDERS'}),
    ('select * from orders o join customers c on c.id = o.cid left join "Line Items" li on li.oid = o.id', {'ORDERS', 'CUSTOMERS', 'LINE ITEMS'}),
    ('select * from app.orders, app.customers c where 1 = 1', {'ORDERS', 'CUSTOMERS'}),
    ('select * from [dbo].[Order Details] od', {'ORDER DETAILS'}),
    ('select * from `shop`.`orders`', {'ORDERS'}),
    ('with recent as (select * from orders) select * from recent r join users u on u.id = r.uid', {'ORDERS', 'USERS'}),
    ('select * from (select * from orders) x', {'ORDERS'}),
    ('select a from t1 union all select a from t2', {'T1', 'T2'}),
    ('(select a from t1) union (select a from t2)', {'T1', 'T2'}),
    ('select * from a cross join lateral (select * from b where b.x = a.x) l', {'A', 'B'}),
    ('select * from generate_series(1, 10) g', set()),
    ('select $$ from users $$ as text from orders', {'ORDERS'}),
    # As `bauta subset` writes them: the parent's selection filters, and is copied by a job of its own.
    ('WITH s1 AS MATERIALIZED (SELECT * FROM "customers" c1 WHERE (id < 100)), '
     's2 AS (SELECT * FROM "orders" o2 WHERE EXISTS (SELECT 1 FROM s1 s3 WHERE s3."id" = o2."cid")) SELECT * FROM s2', {'ORDERS'}),
    ])
def test_the_tables_a_query_reads_rows_from(query, tables):
    assert tablesRead(query) == tables


def test_a_query_whose_parentheses_do_not_balance_is_not_read():
    assert tablesRead('select * from (orders') is None
    assert tablesRead('select * from orders)') is None


def test_tables_inside_a_parenthesized_join_are_read():
    assert tablesRead('select * from orders o join (users u join roles r on r.id = u.rid) on u.id = o.uid') == {'ORDERS', 'USERS', 'ROLES'}
    assert tablesRead('select * from (a cross join b)') == {'A', 'B'}

# Copy a subset


```
bauta subset --connection prod --target staging \
    --root customers --where "created_at >= '2026-01-01'" --mask --output subset/jobs.yaml
```

`subset` generates one data job per table so that the copy is **referentially complete**: every foreign key in a copied row points at a row that was also copied. It reads the foreign keys from the source database's catalog, including composite keys, and follows them in both directions:

- **Down** (skip this with `--no-children`): rows that reference the selected rows. A customer's orders, and those orders' line items.
- **Up** (always): rows that anything selected references. The products those line items point at, and whatever those products point at in turn.

Each job's `sourceQuery` is plain SQL: a `WITH` clause defining each table's selection once, joined by `EXISTS`. It runs unchanged on all seven databases (MySQL from 8.0, MariaDB from 10.2). On PostgreSQL, SQLite and DuckDB the selections are marked `MATERIALIZED`, so each is computed once. Jobs load parents before children, so the target can keep its foreign keys enabled. `--mask` adds a proposed policy for each table, as `discover` does.

**Cycles** can't be followed in SQL that works on every database. This includes a table that references itself, like `employees.manager_id`. `subset` reports the cycle and stops. Break it with `--ignore-foreign-key employees.manager_id`.

An ignored key is a key the subset stops following, so the rows it copies may point at rows it didn't. A nullable column doesn't help by itself — the value is still there, still pointing at nothing. Either mask the column to `'null'`, which needs it to be nullable, or leave that foreign key out of the target. `verify-references` counts what is left pointing at nothing either way. Naming any one column of a composite key ignores the whole key.

**Depth is limited.** A subset may follow a chain of up to 16 tables (`customers` → `orders` → `order_items` → ... is a chain of three). MySQL refuses deeper ones, and SQL Server takes seconds to plan them and fails past about 24, so `subset` refuses them up front rather than generating queries that fail. For a deeper schema, root the subset lower down, use `--no-children`, or split it into subsets rooted at different tables.

**Each table is read at a different moment**, by its own job. Rows written to the source between two of those reads can reference rows that weren't copied, and the target's foreign keys will then reject them. Subset from a replica or a snapshot that isn't being written to, or make `--where` exclude recent rows (`created_at < '2026-09-01'`) so late writes fall outside the subset.

The target's tables must already exist. `subset` generates jobs; it doesn't create tables. See the next section for creating them.


## Creating and refreshing the copy

### `schema`: creating the target's tables

```
bauta schema --connection prod --target staging --table customers --related --apply
```

`schema` reads the source's tables and creates matching tables in the target, **in the target's own dialect**: an Oracle `NUMBER(12,2)` becomes `NUMERIC(12,2)` on PostgreSQL, and `NVARCHAR(MAX)` on SQL Server becomes `CLOB` on Oracle.

- **What it copies:** columns, nullability, the primary key, foreign keys between the tables being created, and the `UNIQUE` constraints those keys need — a key may reference a unique column that isn't the primary key, and every dialect refuses one with nothing unique behind it. Not indexes, defaults, check constraints, triggers or permissions. A non-production copy rarely needs them, and translating them between databases is where schema tools go wrong.
- **Which tables:** `--table` names them, or `--all-tables` takes every table in the database (`--schema NAME` for another schema's). `--related` adds every table a subset rooted there would copy, which is what the headers of generated subset jobs suggest. `--no-children` narrows that to the tables `--table` references. Each is created under the name you asked for, not the case its catalog happens to hold (Oracle's is upper case), so the jobs that follow find every one of them.
- **Constraint names:** a foreign key keeps its source name where that fits the target's length limit and no other key in the run has taken it; otherwise it gets a numbered suffix (`fk_parent_2`). PostgreSQL and SQLite name constraints per table, while MySQL, MariaDB, Oracle and SQL Server need them unique across the schema.
- **Without `--apply`,** it prints the SQL, or writes it to `--output`, for you to review or hand to a DBA. Each lossy choice is a comment above its table.
- **With `--apply`,** it creates the tables in dependency order and **skips any that already exist**. It never alters or drops anything, so it's safe to re-run.
- **`--stage-suffix _stage`** also creates `<table>_stage` tables for `swap` jobs, with the same columns and key but **no foreign keys**, and none of the unique constraints those keys need: a key follows the table it was declared on, so once the parent is swapped it would check the emptied old table and refuse every row. The consequence is that a swapped table's keys alternate — see [how a swap works](../concepts/how-it-works.md#how-a-swap-works). When `--target` is the source database itself, as for [masking in place](mask-a-table.md#masking-in-place), only the stage tables are created.
- **`--no-foreign-keys`** leaves foreign keys out. Use it when tables reference each other in a cycle; add those keys yourself once both tables exist.

A few conversions change what a column can hold, and the generated SQL notes each one:

| Source | Target | Becomes |
| --- | --- | --- |
| Oracle `DATE` | anything else | a timestamp, since Oracle's `DATE` includes a time of day |
| MySQL `TIME` | anything | it is a duration, not a time of day — `-838:59:59` is a legal value — and it is written as `[-]HH:MM:SS[.ffffff]` text, which every database parses back. A `TIME` column elsewhere holds only a time of day, so a value outside one is refused as it loads rather than stored as something else |
| any `TIME` | Oracle | `VARCHAR2(32 CHAR)`, since Oracle has no time-of-day type; wide enough for the day-long values MySQL's `TIME` allows |
| a time-zone-aware timestamp | Oracle | `TIMESTAMP WITH TIME ZONE`, which keeps the offset of the session that loads the row, not the source's, so the instant moves unless that session is UTC |
| SQLite `INTEGER` | anything else | that target's `INT`, which is narrower: SQLite stores an integer in up to 8 bytes whatever the column is called |
| MySQL or MariaDB unsigned integers | anything | the next signed type up, which holds the whole range: `SMALLINT UNSIGNED` an `INTEGER`, `INT UNSIGNED` a `BIGINT`. No database has a signed integer for all of `BIGINT UNSIGNED` or DuckDB's `UBIGINT`, so those become `DECIMAL(20,0)` |
| DuckDB `HUGEINT` | anything | a decimal of 39 digits, which DuckDB, Oracle and SQL Server clamp to 38; a value past that is refused as it loads |
| DuckDB `LIST`, `STRUCT`, `MAP` | anything else | JSON, loaded as JSON text; into PostgreSQL, `JSONB` |
| any decimal | sqlite | `TEXT`, which keeps every digit. SQLite has no exact decimal type, and a column declared `DECIMAL(p,s)` holds a float: it would keep about 15 digits and round the rest away as the row is written |
| a decimal declaring no precision | mysql, mariadb, mssql | `DECIMAL(65,30)` or `DECIMAL(38,10)`, which round anything longer |
| a decimal wider than the target allows | mysql, mariadb, oracle, mssql | the widest that target has, so whole digits or decimal places are lost |
| a time-zone-aware timestamp | MySQL, MariaDB | `DATETIME(6)`, and the offset is lost |
| unbounded text in a key | MySQL, SQL Server, Oracle | 255 characters, since those can't index unbounded text |
| a boolean stored as an integer (SQLite, MySQL, Oracle `NUMBER(1)`) | anything | a small integer, since PostgreSQL won't load an integer into `BOOLEAN` |
| a type it doesn't recognize | anything | text |

Every combination of the seven databases is tested, twice: tables are created on the target and a copy then loads into them, once for ordinary columns, and once for each source's own UUID, time of day, JSON, binary, time-zone-aware timestamp and unsigned integer, which come back as the same values.

### `verify-references`: checking the copy's references

```
bauta run && bauta verify-references
```

`verify-references` counts, for each foreign key on a table the jobs load, the rows whose key matches no row of the table it references. It's the check that proves a copy intact, where [`audit`](prove-the-copy-is-safe.md#reviewing-policies-audit) can only predict from the configuration.

- **Which keys:** those the target declares, and those of the sources copied into it, matched to the copy by table name as `audit` matches them. Each database's keys are read from the schemas the jobs use: a target's from the schema of each `targetTableFinal`, `app` for `app.orders`; a source's from the schema of each `sourceQuery` that reads a table whole, `SELECT * FROM app.orders`, and from the connection's own for any other query. A declared key doesn't rule orphans out: MySQL loads can turn checks off, and SQL Server, Oracle and PostgreSQL constraints can be disabled or never validated. A key only a source declares is matched to the target's own spelling of each column.
- **Which tables:** each active job's `targetTableFinal`, with every key it declares, and the tables those reference. `--job` narrows it, and names inactive jobs too.
- **What it costs:** one `NOT EXISTS` query per key, which scans the child table once and looks each key up in the parent's primary key. The source is read for its catalog only, never its rows.
- **What it reports:** a count per key, never a value, since some keys are copied as they are. A key whose columns include a NULL points at nothing and isn't counted, as databases don't enforce it. A key that can't be checked, because the target lacks its table or a column, or refused the query, is reported with the reason.
- **Nothing to check:** a target with no key to count gets a line saying so, rather than passing as a check that ran. Where no database involved declares a key anywhere -- the application enforces them, or the tables have none -- it is `NONE`, and the run still succeeds. Where one does declare keys, but in a schema the jobs' tables aren't in, it is `ERROR`, naming the schemas: set `currentSchema` on the connection, or qualify the jobs' tables with their schema.
- **Exit status:** 1 if any key has orphaned rows or couldn't be checked, or keys were declared only where the jobs don't look, so it can follow `run` in CI. `--format json` and `--output FILE` work as for `audit`.

```
OK       staging: orders (customer_id) -> customers (id): 0 orphaned row(s)
ORPHANS  staging: order_items (product_id) -> products (id) [not declared]: 12 orphaned row(s)
Checked 2 foreign key(s): 1 with orphaned rows, 0 not checked
```

### `clear`: emptying the copy before a refresh

A subset's jobs upsert, so rows from an earlier subset stay unless the copy is emptied first:

```
bauta clear --config subset --dry-run     # which tables, in what order
bauta clear --config subset --yes
bauta run --config subset --force
```

- **What it empties:** `clear` deletes every row from each active job's `targetTableFinal`, child tables before their parents, so the target's foreign keys don't block it. `--job` narrows it to particular jobs.
- **All or nothing:** each database is emptied in one transaction. If one table can't be emptied, for example because a table outside the set still references its rows, nothing is deleted.
- **`DELETE`, not `TRUNCATE`:** PostgreSQL, SQL Server and Oracle refuse to truncate a table that a foreign key references.
- **Nothing happens without `--yes`.** Without it, `clear` exits with status 2.
- **Incremental jobs are refused.** Their stored watermark would survive, so the next run would load only new rows into the empty table.
- **Run with `--force` afterwards**, so a `refresh` window can't leave a cleared table empty.

Between `clear` and the end of the run, the copy is empty or partly loaded. For a copy people use while it refreshes, load into stage tables and `swap` instead.

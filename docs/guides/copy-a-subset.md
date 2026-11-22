# Copy a subset


```
bauta subset --connection prod --target staging \
    --root customers --where "created_at >= '2026-01-01'" --mask --output subset/jobs.yaml
```

`subset` generates one data job per table so that the copy is **referentially complete**: every foreign key in a copied row points at a row that was also copied. It reads the foreign keys from the source database's catalog, including composite keys, and follows them in both directions:

- **Down** (skip this with `--no-children`): rows that reference the selected rows. A customer's orders, and those orders' line items.
- **Up** (always): rows that anything selected references. The products those line items point at, and whatever those products point at in turn.

Each job's `sourceQuery` is plain SQL that runs unchanged on all seven databases (MySQL from 8.0.19, MariaDB from 10.2): a `WITH` clause defining each table's selection once (materialized on PostgreSQL, SQLite and DuckDB), joined by `EXISTS`. Jobs load parents before children, so the target can keep its foreign keys enabled. `--mask` adds a proposed policy for each table, as `discover` does, and is on without asking where `--target` has [`requireMasking`](../reference/connections.md#requiring-masking); without it, `subset` warns that the jobs copy every column as it is.

**Cycles** can't be followed in SQL that works on every database. This includes a table that references itself, like `employees.manager_id`. `subset` reports the cycle and stops. Break it with `--ignore-foreign-key employees.manager_id`.

An ignored key isn't followed, so copied rows may point at rows that weren't copied. Mask the column to `'null'` (it must be nullable), or leave that foreign key out of the target; `verify-references` counts what still points at nothing. Naming one column of a composite key ignores the whole key.

**Depth is limited** to a chain of 16 tables, since MySQL and SQL Server fail on deeper queries. For a deeper schema, root the subset lower down, use `--no-children`, or split it into several subsets.

**Each table is read at a different moment**, by its own job, so rows written to the source in between can reference rows that weren't copied. Subset from a replica or snapshot, or make `--where` exclude recent rows (`created_at < '2026-09-01'`).

The target's tables must already exist; `schema`, below, creates them.


## Creating and refreshing the copy

### `schema`: creating the target's tables

```
bauta schema --connection prod --target staging --table customers --related --apply
```

`schema` reads the source's tables and creates matching tables in the target, **in the target's own dialect**: an Oracle `NUMBER(12,2)` becomes `NUMERIC(12,2)` on PostgreSQL, and `NVARCHAR(MAX)` on SQL Server becomes `CLOB` on Oracle.

- **What it copies:** columns, nullability, the primary key, foreign keys between the tables being created and the `UNIQUE` constraints they need, and plain indexes over columns, so queries against the copy use production's indexes. Left out, with a note: expression, partial and unindexable-column indexes, and every index on a DuckDB target. Not defaults, check constraints, triggers or permissions: a copy rarely needs them, and translating them between databases is where schema tools go wrong.
- **Which tables:** `--table` names them, or `--all-tables` takes every table (`--schema NAME` for another schema's). `--related` adds every table a subset rooted there would copy, and `--no-children` narrows that to what `--table` references. Each is created under the name you asked for, so the jobs that follow find it.
- **Names and types:** between two of the same database, a column keeps its spelling and a PostgreSQL array its type. To another kind, a column is folded as that database folds an unquoted name, and an array becomes text, with a comment saying so. A foreign key keeps its source name unless it is too long or already taken, then gets a numbered suffix.
- **Without `--apply`,** it prints the SQL, or writes it to `--output`, for you to review or hand to a DBA. Each lossy choice is a comment above its table.
- **With `--apply`,** it creates the tables in dependency order and **skips any that already exist**. It never alters or drops anything, so it's safe to re-run.
- **`--stage-suffix _stage`** also creates `<table>_stage` tables for `swap` jobs, with the same columns and key but **no foreign keys** (a key follows its table, so after the parent swaps it would check the emptied old one); see [how a swap works](../concepts/how-it-works.md#how-a-swap-works). When `--target` is the source itself, as for [masking in place](mask-a-table.md#masking-in-place), only the stage tables are created.
- **`--no-foreign-keys`** leaves foreign keys out. Use it when tables reference each other in a cycle; add those keys yourself once both tables exist.

A few conversions change what a column can hold, and the generated SQL notes each one:

| Source | Target | Becomes |
| --- | --- | --- |
| Oracle `DATE` | anything else | a timestamp, since Oracle's `DATE` includes a time of day |
| MySQL `TIME` | anything | `[-]HH:MM:SS[.ffffff]` text: MySQL's `TIME` is a duration (`-838:59:59` is legal), and a value outside a day is refused by a `TIME` column elsewhere |
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

Every pair of the seven databases is tested: tables are created on the target and a copy loads into them, ordinary columns and each source's own special types alike, which come back as the same values.

### `verify-references`: checking the copy's references

```
bauta run && bauta verify-references
```

`verify-references` counts, for each foreign key on a table the jobs load, the rows whose key matches no row of the table it references. It's the check that proves a copy intact, where [`audit`](prove-the-copy-is-safe.md#reviewing-policies-audit) can only predict from the configuration.

- **Which keys:** those the target declares, and those of the sources copied into it, read from the schemas the jobs use and matched by table name. (A declared key doesn't rule orphans out: constraints can be disabled or never validated.)
- **Which tables:** each active job's `targetTableFinal`, with every key it declares, and the tables those reference. `--job` narrows it, and names inactive jobs too.
- **What it costs:** one `NOT EXISTS` query per key, which scans the child table once and looks each key up in the parent's primary key. The source is read for its catalog only, never its rows.
- **What it reports:** a count per key, never a value. A key with a NULL column isn't counted, as databases don't enforce one. A key that can't be checked (a missing table or column, a refused query) is reported with the reason.
- **Nothing to check:** where no database declares a key anywhere, it says `NONE`, and succeeds. Where keys exist only in schemas the jobs don't use, it is `ERROR`, naming them: set `currentSchema`, or qualify the jobs' tables.
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
- **Nothing happens without `--yes`** (exit status 2).
- **Incremental jobs are refused**: their watermark would survive, and the next run load only new rows.
- **Never while those jobs run**: `clear` takes their run lock, and with a run under way empties nothing and exits 1.
- **Run with `--force` afterwards**, so a `refresh` window can't leave a cleared table empty.

Between `clear` and the end of the run, the copy is empty or partly loaded. For a copy people use while it refreshes, load into stage tables and `swap` instead.

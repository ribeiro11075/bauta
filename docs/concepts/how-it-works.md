# How it works, and why

The behaviour behind the fields in [configuration.md](../reference/configuration.md), and the consequences worth knowing before you rely on it.

- [How a data job moves rows](#how-a-data-job-moves-rows)
- [How a swap works](#how-a-swap-works)
- [Incremental loads](#incremental-loads)
- [refresh and predecessors](#refresh-and-predecessors)
- [Single runs, not a daemon](#single-runs-not-a-daemon)
- [Workers](#workers)
- [Partitions](#partitions)
- [Retries](#retries)
- [Structured logs](#structured-logs)
- [Masking](#masking)
- [Moving values between drivers](#moving-values-between-drivers)
- [Files as a target](#files-as-a-target)
- [Iceberg tables](#iceberg-tables)


## How a data job moves rows

Extracts are **streamed**. A data job pulls `chunkSize` rows, transforms them, writes them, and only then pulls the next chunk — it never holds the whole result set. Peak memory is about `chunkSize` × row width whether the source has ten thousand rows or ten billion.

Streaming is per-driver, because `fetchmany()` bounds nothing if the driver has already pulled every row off the socket:

| Dialect | Cursor |
| --- | --- |
| mysql, mariadb | unbuffered |
| postgresql | server-side (named) |
| oracle | `arraysize` tuned to the chunk |
| mssql, sqlite, duckdb | plain — all three already stream |

Loads are written a chunk at a time too, each chunk in its own transaction. The MySQL, MariaDB and Oracle drivers already send a chunk in a few round trips; psycopg, pymssql and DuckDB send one statement per row, so those three get a bulk path:

- **PostgreSQL uses `COPY`**, about 100 times faster on 50,000 rows. An upsert copies into a temporary table and merges it with one `INSERT ... ON CONFLICT`. A chunk holding a value `COPY` can't spell safely (an array, a JSON object, an interval) goes row by row instead.
- **SQL Server uses multi-row statements** of up to a thousand rows: about 6 times faster for inserts and 28 for upserts.
- **DuckDB loads each chunk as an Arrow table**, with one `INSERT ... SELECT`, or `INSERT ... ON CONFLICT` for an upsert: 50,000 rows took 13 seconds row by row, and a tenth of a second this way. A column Arrow can't give one type -- SQLite hands back numbers and text together -- goes as text, which DuckDB casts to the column's type; only a column holding something else besides, such as bytes beside text, sends its chunk row by row.

One statement can't update a row twice, so for all three, rows repeating a key within a chunk are first reduced to the last of them — what applying them in turn would leave.

**A `sourceQuery` that repeats a key** is a question the two upsert paths answer differently, so it is worth not writing one. A stage-less `upsert` keeps the last row of each key, as above. An `upsert` with a `targetTableStage` loads every row into the stage first, and the stage carries the target's primary key, so the database refuses the repeat there — loudly, and before anything reaches the target. Deduplicate in the query instead.

**An upsert matches rows by primary key alone.** A row with a new primary key whose other unique key — an email, say — a different row already holds is refused, and the load fails without retrying, on every database. MySQL and MariaDB need telling: their `ON DUPLICATE KEY UPDATE` fires on any unique key, and used to update the row that held the email with the new row's values, dropping the new row without an error. A guard ahead of the update now fails the statement there instead. Two rows sharing a unique value usually means a masked value collided, or the target's unique keys differ from the source's.

Where the [native masker](../guides/make-it-faster.md#the-native-masker) is installed, the three stages overlap rather than taking turns: masking moves to a worker thread while the reader and writer keep the database connections, which they must — `mysqlclient` and PyMySQL forbid a connection being used by a thread other than its own, and SQLite enforces the same. Drivers release the GIL while they wait on a socket and the native masker releases it for a whole chunk, so the waiting and the masking genuinely overlap. A job then holds about four chunks rather than one: one being read, two masked or being masked ahead of the writer, and one being written. Pure-Python masking is slow enough to swamp any wait worth hiding, so it stays sequential; `BAUTA_PIPELINE` overrides either default.

One chunk is still masked at a time, and chunks are written in the order they were read. The native masker takes the whole chunk in one call and masks its columns at once, so the GIL changes hands once a chunk rather than once a column; with [`maskingThreads`](../guides/make-it-faster.md#masking-threads) above 1, the columns and their distinct values are spread over several threads, which changes how fast the chunk is masked, not the result. A stage-less upsert writes straight into the live target, where one statement can't update the same row twice, so a key repeating across chunks has to arrive as it was read.

**One consequence to know before sizing a job:** extract and load interleave, so a source that fails part-way leaves the rows it already yielded written.

- **Invisible** for `swap`, and for `upsert` with a `targetTableStage` — both write to the stage table, and `targetTableFinal` is only touched in the last step.
- **Visible** for a stage-less `upsert`, which writes partial results straight into the live target. Use a stage table for anything large.

Masking is a stage of this same pipeline (transform, then mask, then load), so a masked job streams like any other. See [masking](#masking).


## How a swap works

`swap` exchanges `targetTableStage` and `targetTableFinal` by renaming them, through a temporary `<target>_tmp` name. A rename never moves a table between schemas, so the two must share one; validation checks. Names may be schema-qualified (`sales.orders`).

| Dialect | Atomic |
| --- | --- |
| mysql, mariadb | yes: one `RENAME TABLE` statement |
| postgresql, mssql, sqlite, duckdb | yes: the renames run in one transaction |
| oracle | **no**: Oracle commits each DDL statement on its own |

On Oracle, a rename that fails — another session holding the table, which is `ORA-00054` — undoes the renames that already went through, so both tables end where they started and the next run swaps normally. The job still fails, and says which statement failed. A job stopped for exceeding `timeoutSeconds` is sent SIGTERM, which waits until the renames are done. A process killed outright between two of them -- out of memory, or the SIGKILL a job gets when it doesn't stop within five seconds -- can't undo anything, and leaves a table under the temporary name: after the second rename, no table under the target's name at all. **The next run of the job puts it right before loading**, and logs what it did: with the target missing, it renames the temporary table to the target, finishing the swap, since that table holds a load that completed; with the stage missing, it renames it back to the stage, undoing it. All three present is no state a swap leaves, so the job fails, naming the temporary table to look at and remove.

**Views.** PostgreSQL ties a view to the table itself, not to its name, so after the renames a view over the target would read what is now the stage table. The swap takes care of it: each view built directly on the target is recreated from its own definition in the same transaction, so it reads the new target, and keeps its grants and the views built on it. SQLite has the opposite habit — it rewrites the views that *name* a renamed table, to follow it — so the swap renames with `legacy_alter_table` on, which leaves them naming the target. The remaining dialects resolve views by name and need nothing.

**DuckDB can't swap a table in a foreign key, or one with an index.** It refuses to rename a table with an index or one another references, and renaming one that references another leaves the other naming it by its old name, after which it can't be dropped. A swap job whose target or stage is either fails before renaming anything, saying to use `upsert`. A primary key is no obstacle.

**What isn't rebound on PostgreSQL:** materialized views, which keep reading the old table until recreated.

**What isn't rebound anywhere:** foreign keys in other tables that reference the target. Every database ties them to the table, not its name, so they move with the old table to the stage's name and stop checking the new target. The next run then can't empty the stage: PostgreSQL, SQL Server, Oracle, MySQL and MariaDB refuse to truncate a referenced table, and SQLite refuses to delete rows still referenced. Recreate those keys in `postTargetAdhocQueries`, or use `upsert` with a stage table instead of `swap`. `audit --connect` [reports](../guides/prove-the-copy-is-safe.md#reviewing-policies-audit) a swap job whose target such a key references.

**A `postTargetAdhocQuery` that fails after the swap** fails the job, but the swap has already happened: the target holds the new rows. The failure says so, and reports the rows loaded rather than none, so a copy that was in fact rebuilt doesn't read as a job that moved nothing.

**The swapped table keeps its primary key and unique keys.** Before the stage is loaded, the job gives it whichever of the target's it lacks -- a stage made with `CREATE TABLE ... AS SELECT`, say, or the unique constraints [`--stage-suffix`](../guides/copy-a-subset.md#schema-creating-the-targets-tables) leaves out -- so the live table has them after every swap, not every other one, and a source repeating a key fails the load before anything reaches the target. A stage that already has them, as every stage after the first swap does, is left as it is.

- **Each is added unnamed**, so the database names it as it would any constraint, and no name can collide with one the target already has. The two tables then trade constraint names at every swap, as they trade table names.
- **PostgreSQL, MySQL, MariaDB, Oracle and SQL Server** add them to the empty stage with `ALTER TABLE`; SQL Server first makes a key column NOT NULL, restating its type and collation, since it refuses a key over a column that allows NULL. **SQLite and DuckDB**, which can't add one that way, create the empty stage again from its own definition with the keys added, and SQLite its indexes after it.
- **Not copied:** a unique index on an expression, a partial or filtered one, or one over a prefix of a column, which a column list can't recreate; and a DuckDB `CREATE UNIQUE INDEX`, since DuckDB won't swap a table with an index at all.

**The swapped table keeps its grants and plain indexes too.** The two tables trade names every run, so whatever only the target had was there after every other swap: a role granted `SELECT` on the copy could read it after one run and not after the next, and queries had their indexes every other run. Before loading, the stage is also given the target's own grants -- `GRANT ... ON` the table, not grants on its schema or database, which cover the stage already -- and its plain, non-unique indexes over columns, named as the target's with `_s`. PostgreSQL's B-tree indexes, MySQL's and MariaDB's, Oracle's normal ones and SQL Server's non-clustered ones are copied; an index on an expression, a partial or filtered one, or another kind (GIN, columnstore) is not, and SQLite's are not, since SQLite names indexes across the database. A stage with indexes loads more slowly than one without, as every second run's already did. A grant or index that can't be given -- no privilege to grant it, say -- is a warning, and the swap goes on.
- **A key that can't be added** -- no privilege to alter the stage, say -- is a warning, and the swap goes ahead as it would have.

**The swapped table's foreign keys alternate.** A stage is given none of them, for the reason `--stage-suffix` gives it none: a key follows the table it was declared on, so a key on the stage of a parent that is swapped too would check the emptied old table. After a swap the live table is the former stage, without them, and after the next the original is back with them; between the two, the copy enforces none, and no load fails to tell you. `audit --connect` [warns](../guides/prove-the-copy-is-safe.md#reviewing-policies-audit) about a swap job whose table declares foreign keys. Recreate them in `postTargetAdhocQueries`, or use `upsert` with a stage table for any table whose references matter.


## Incremental loads

By default a job re-extracts its whole source every run. Set `watermarkColumn` and it extracts only rows past where the last **successful** run got to:

```yaml
loadOrders:
  sourceQuery: >
    select id, customerId, amount, updatedAt from orders
    where updatedAt > {{ watermark }} - interval 5 minute
  watermarkColumn: updatedAt
  watermarkInitial: 1970-01-01 00:00:00    # unquoted: YAML reads it as a timestamp
  insertStrategy: upsert
  # ...
```

- **`{{ watermark }}`** is a bound parameter, not text substitution. It can go anywhere a value can, including a join or subquery, and is rewritten to each dialect's own placeholder, so one query works on all seven.
- **`watermarkColumn`** is read from the *raw* rows, before transforms run. A transform may reformat the column, and the next run's predicate needs a value the source can still compare against.
- **`watermarkInitial` is bound as whatever YAML made of it.** Written without quotes, `1970-01-01 00:00:00` is a timestamp, which is what a timestamp column and the lookback arithmetic below both want. In quotes it is text, and PostgreSQL, Oracle and DuckDB refuse to subtract an interval from text — on the first run, every run, so the job never advances. Quote it only where the column really is text.
- **`insertStrategy: upsert`** is required, or `append` into a files connection. `swap` or `overwrite` would replace the target with only the rows that changed, deleting everything else. An append keeps no key, so the overlap below is loaded twice there; see [appends and duplicate rows](#appends-and-duplicate-rows).

### Why the lookback window

The `- interval 5 minute` above closes the hole that silently loses data in incremental pipelines:

> A transaction that **starts before** your run and **commits after** it, carrying a timestamp from before your run, was never visible to your query — but the watermark has already moved past it. The row is skipped for good.

Re-reading a small overlap every run closes it, and costs nothing *because* `upsert` makes reloading an existing row a no-op. That's why the two go together.

The library never does arithmetic on a watermark, so write the lookback in your own dialect:

| Dialect | Lookback |
| --- | --- |
| mysql, mariadb | `{{ watermark }} - interval 5 minute` |
| postgresql | `{{ watermark }} - interval '5 minutes'` |
| oracle | `{{ watermark }} - numtodsinterval(5, 'minute')` |
| mssql | `dateadd(minute, -5, {{ watermark }})` |
| sqlite | `datetime({{ watermark }}, '-5 minutes')` |
| duckdb | `{{ watermark }} - interval '5 minutes'` |
| numeric id | `{{ watermark }} - 5` |

Take watermarks from the **database's** clock, not the ETL host's, or clock skew becomes data loss.

### Crash safety

On success, each step commits before the next:

```
load committed  →  record watermark  →  record run  →  report completion
```

So every point a job can die at falls *backwards*, into re-reading rows already loaded — harmless under `upsert`. Nothing is recorded for a failed job: an advanced watermark there would skip rows permanently, the one unrecoverable direction. A run that finds no new rows leaves the stored watermark where it is.

### Deletes

**Watermarks cannot see hard deletes.** A deleted row has no `updatedAt` to pass the watermark; it just stops appearing, and the target keeps it. Soft deletes — a flag whose change bumps `updatedAt` — work fine.

**`bauta run --full-refresh` removes them**, scheduled as often as a deleted row may outlive its source, weekly say. It runs each incremental job over its whole source and replaces the target, rather than upserting into it:

- Its query is bound to `watermarkInitial`, the value a first run binds, so it returns every row without being rewritten. What it reads up to is recorded as the watermark, and the next incremental run carries on from there.
- A database target is loaded into its `targetTableStage` and [swapped](#how-a-swap-works) in, so readers see the old copy until the new one is whole. That needs `targetTableStage` on the job, and the stage table created (`bauta schema --stage-suffix`); a job without one is refused, with every other such job, before anything runs. [`bauta audit`](../guides/prove-the-copy-is-safe.md#reviewing-policies-audit) warns of each such job, so `audit --strict` in CI finds one before the scheduled refresh does. An Iceberg target takes one overwrite commit. Incremental jobs writing files are refused: their appended parts and an overwrite's snapshot directories are different layouts to a reader.
- Refresh windows are ignored, as with `--force`. Jobs that aren't incremental run as they always do.
- Since the target is replaced whole, a masking key changed since the last run is no reason to stop: a full refresh is also how an incremental job's copy moves to a new key without `bauta clear`.

It costs a full extract per refresh, and room for two copies of each table while it swaps. Change-data-capture, which this library doesn't do, is the alternative that costs neither.

A reasonable split: `swap` for small tables; watermarked `upsert` for large append-and-update tables, with `--full-refresh` scheduled where deletes matter.

### Tables that reference each other

A watermarked parent holds only the rows changed since `watermarkInitial`. A new order for a customer who hasn't changed since then references a customer the parent job never selects. With the foreign key declared in the target, the child job fails on every run, because a failed job's watermark doesn't advance. Without it, the orders load with references that point at nothing. Copy the parent whole, or have its query also select what new child rows reference:

```sql
select * from customers c
where c.updatedAt > {{ watermark }}
   or exists (select 1 from orders o where o.customerId = c.id and o.updatedAt > {{ watermark }})
```

`audit --connect` [warns about a parent copied in part](../guides/prove-the-copy-is-safe.md#reviewing-policies-audit) whose child isn't limited to match, and [`verify-references`](../guides/copy-a-subset.md#verify-references-checking-the-copys-references) counts the rows a copy already holds that point at nothing.

### Where watermarks are kept

In run state: `jobs.yaml`'s `memory`, a file or a table (see [run state](../guides/run-on-a-schedule.md#run-state)); from Python, a `MemoryBackend`. `FileMemory` writes each update to a temporary file and renames it into place, so a process killed mid-write leaves the previous version rather than a file nothing can parse. It assumes a filesystem that persists between runs and is shared by every worker. Where that's false — a container without a volume, anything scaled across machines, serverless — use `DatabaseMemory`. `FileMemory` there doesn't fail loudly: it silently forgets every watermark and re-extracts from `watermarkInitial`. See [library.md](../reference/python-api.md#memory-backends).


## `refresh` and predecessors

**`refresh` decides whether a job is in a cycle at all. `predecessors` only orders jobs within a cycle.** So a predecessor sitting inside its own refresh window is not waited for.

A job with `refresh: 5` whose predecessor has `refresh: 60` runs alone for 11 cycles in 12, and waits for its predecessor on the 12th. That's deliberate — otherwise `refresh: 5` would silently behave as `refresh: 60` — and it's what lets an hourly dimension load and a 5-minute fact load coexist.

The trade-off is freshness, not correctness: between windows the dependent reads output up to an hour old. That's fine for a durable table, and wrong if the predecessor produces something transient the dependent consumes. Give both the same `refresh` in that case.

It's wrong too when the dependent's table references the predecessor's: in the cycles the predecessor sits out, new orders load before the customers they reference. `audit --connect` [warns about this](../guides/prove-the-copy-is-safe.md#reviewing-policies-audit), and about a referencing job that doesn't wait at all.

`bauta jobs` shows which jobs are due and which are throttled.


## Single runs, not a daemon

`bauta run` makes one pass and exits, because it should compose with whatever already schedules work — cron, a systemd timer, a Kubernetes CronJob, an Airflow task — rather than compete with it. Those give you alerting, backfill and calendar-aware schedules that `refresh` can't express; `refresh` is a throttle, not a schedule. [Run from Airflow or Dagster](../guides/run-from-an-orchestrator.md) has working examples.

`refresh` still works across separate invocations, since it's checked against the durable memory backend. Running every 5 minutes with `refresh: 60` correctly skips 11 runs in 12:

```cron
*/5 * * * *  cd /srv/etl && bauta run
```

`--forever` keeps the process resident, for freshness below cron's one-minute floor or where there's no scheduler.

**Stopping.** On `SIGINT` or `SIGTERM`, a run starts no new jobs, lets the running ones finish, reports the rest as skipped, and exits with status 130. Killing jobs mid-load instead would leave a streaming cursor or a half-loaded table for the database to clean up. A container's grace period has to cover the longest job for this to finish; give long jobs a `timeoutSeconds` shorter than that grace period, so a hung one can't hold the shutdown. Between `--forever` cycles, a signal ends the pause within a second, however long `cycleSleepSeconds` is.

**Overlapping runs.** `run` holds a lock (`memory.yaml.run.lock`, beside the run state file; see [run state](../guides/run-on-a-schedule.md#run-state) for run state in a table) for as long as it runs. A second invocation sharing that memory file exits with status 1 instead of running the same jobs at the same time, which a cron interval shorter than a slow run would otherwise cause. The operating system releases the lock if the process dies.

A skipped job exits non-zero just as a failed one does: it didn't run, so its data isn't there.


## Workers

Each job runs in a process of its own, as soon as its predecessors have completed, one of the `workers` slots is free, and each connection it uses is below its [`maxConcurrentJobs`](../reference/connections.md) -- always 1 for DuckDB, which one process at a time may open. A job held back by a connection doesn't hold back the jobs behind it that use others. Starting a process costs a few milliseconds, forked as below, and it lets each job be ended on its own:

- **A job that dies** — killed for memory, crashed in a driver — fails, and only that job. Its dependents are skipped, and the run still ends; it doesn't wait for an outcome that will never come.
- **A job past its `timeoutSeconds`** is sent `SIGTERM`, then `SIGKILL` five seconds later if it hasn't exited. It fails with a `Timeout` error and its dependents are skipped. Its database connections close with it, so each server rolls back whatever the job hadn't committed; what it had committed stays, as for any failure part-way (see [how a data job moves rows](#how-a-data-job-moves-rows)). The timeout covers the whole job, retries included.

Within its process, a job masks on one thread, or on several with [`maskingThreads`](../guides/make-it-faster.md#masking-threads); `auto` divides half the cores between the jobs running when each starts.

Processes are forked from a **forkserver**: a process started once per run, before any job, that has imported bauta and nothing else, so a job starts in milliseconds rather than importing bauta, pydantic and the configuration models over again -- about 200 ms and as much CPU a job, which made 60 small jobs take 13 seconds on one worker rather than 1.5. It is not plain `fork`, which copies whatever locks the parent's threads hold and can deadlock a child: the forkserver is single-threaded when it forks. Each job is given the run's environment as it is when the job starts, not as it was when the forkserver started, so a program changing `BAUTA_*` variables between runs is heard. Where there is no forkserver, or `BAUTA_START_METHOD=spawn` says so, each job is started afresh instead. Either way a job imports the program that started the run again, as `__mp_main__`, so a program embedding the library needs an `if __name__ == '__main__':` guard; see [library.md](../reference/python-api.md#running-jobs).

**Logs from jobs** are sent back to the main process and written by its handlers, so they follow `--log`, `--log-format` and `--quiet` like everything else.

**Ctrl-C** reaches every process in the terminal's group; jobs ignore it and leave the decision to the main process, as described under stopping above.

**A run killed outright** — `kill -9`, an out-of-memory kill, a scheduler that doesn't wait — takes its jobs with it. Each job watches a pipe the run holds the other end of and never writes to, so it reads end-of-file the moment the run dies, and ends there: no unwinding, no further rows, and each server rolls back what it hadn't committed. It matters because the run lock is held by that process and dies with it, so the next `bauta run` can start immediately; a job left loading would have written over it.


## Partitions

A job reads its `sourceQuery` as one stream, on one connection, so a table of a few billion rows takes as long as one connection takes to read, mask and write it. `partitions` divides the job into slices that run at once:

```yaml
copyEvents:
  sourceQuery: select id, accountId, payload, createdAt from events
  insertStrategy: swap
  targetTableStage: events_stage
  partitions:
    column: id
    count: 8
  # ...
```

**How the rows are divided.** The job first asks for the column's smallest and largest value over the whole query, divides that range into `count` ranges, and reads each through the query wrapped in a derived table:

```sql
SELECT * FROM (select id, accountId, payload, createdAt from events) bauta_partition WHERE "id" >= 250000 AND "id" < 375000
```

Ranges rather than a modulo: a range is read from an index on the column, so each slice reads only its own rows, where `MOD(id, 8) = 3` would have every slice scan the whole table — and has no one spelling, since Oracle has no `%` and SQL Server no `MOD`. The first slice has no lower bound and takes the rows where the column is null as well; the last has no upper bound. So every row is read exactly once, a row added beyond the bounds after they were read included. Slices are only as even as the values: a key with a large gap gives the slices around it fewer rows. There are fewer slices where the column has fewer integers between its bounds than `count`, and one, the query as it is, where the query returns no rows.

The column must be among those the query returns. **A number** is sliced into equal ranges between its smallest and largest value, as above. **Any other column -- a UUID key, text, a date --** is sliced where the database deals its values into `count` even shares, in its own order: `NTILE` over the column, and the last value of each share is where a slice ends. That costs one ordered pass over the column first, which an index on it serves, and gives slices of even size however the values are spread, each still a range an index can read; SQL Server's `uniqueidentifier`, which orders by its last six bytes first, is sliced in that order, since the bounds come from the database. Those bounds are data, so they are bound into each slice's query as parameters, never written into it or logged; on PostgreSQL, MySQL, MariaDB and SQL Server, a query that binds no watermark has its literal `%` doubled for them, as the drivers then require. `partitions: auto` slices a target's UUID primary key the same way.

**What it costs.** The bounds query: next to nothing where the column is indexed and the query reads one table, and a whole extra pass where the database has to evaluate a join or an aggregate to answer it. And the derived table: SQL Server refuses one holding an `ORDER BY` (without `TOP`) or a `WITH` clause, so write a partitioned job's query without them there.

**Each slice is a reader, masker and writer of its own**, on threads of the job's process, with a source and a target connection of its own: a connection serves one thread, and SQLite and the MySQL drivers refuse any other. Every slice writes into the one table the job loads: its `targetTableStage` for `swap` and for `upsert` with a stage, and the live table for a stage-less `upsert`. Files and Iceberg targets refuse `partitions`, since what they publish is what one writer wrote.

**All or nothing.** The job succeeds only once every slice has loaded. The first slice to fail stops the others before their next write, and its error is the job's: nothing is swapped in or upserted from the stage, no watermark or run is recorded, and a [retry](#retries) starts every slice over, as it starts any job over. A stage-less upsert keeps what its slices wrote before the failure, as it would unpartitioned. An incremental job's watermark is the highest any slice read.

**Masks are the same.** Every mask is derived from its value alone, so a partitioned copy is byte for byte the copy one stream makes. `shuffle`, which shuffles values within a chunk, is the exception, since a slice's chunks hold other rows; each slice numbers its chunks apart from every other's, so no two chunks of a job are shuffled alike.

**No longer one snapshot.** Each slice reads in a transaction of its own, so the copy is not one consistent view of the source, and a row whose partition column changes during the run can be read twice or not at all. Partition on a column that doesn't change, such as the primary key. Several connections upserting into one table at once can deadlock on SQL Server and MySQL; a deadlock fails the attempt as any database error does, and the job is retried, but a stage table avoids it.

**Workers, connections and threads.** A partitioned job is one job: one `workers` slot, one process, one `timeoutSeconds` for all its slices. It holds a place in each connection's [`maxConcurrentJobs`](../reference/connections.md) for every slice, though, since each slice opens a connection there; a job with more slices than a connection allows is refused by `validate`, and so is any partitioned job on DuckDB, which allows one. With `maskingThreads: 1` each slice masks on its own thread; above 1, the slices share the job's masking threads, except that where `maskingThreads: auto` gave the job no more threads than it has slices, each slice masks on its own thread instead ([partitions: auto](../guides/make-it-faster.md#partitions-auto)). Masking in Python holds Python's interpreter lock, so slices overlap their reading and writing but not their masking; with the [native masker](../guides/make-it-faster.md#the-native-masker) they overlap that too. See [partitions](../guides/make-it-faster.md#partitions) for what it gains.


## Retries

A data job with `retries: 3` gets up to four attempts. The delay starts at `retryDelaySeconds` and doubles, so a database that's down isn't hit at a fixed interval while it recovers, up to five minutes between attempts.

Retrying a whole job is safe because both strategies converge on a re-run: `swap` restages and re-swaps, and `upsert` reapplies existing rows as a no-op.

**What isn't retried:** configuration errors (including a target without a primary key), transform errors, unresolvable transformer references and masking errors. All of them come from this package and fail the same way every time; retrying would only delay the failure and bury the message under repeats. Everything a database driver raises *is* retried — transient and permanent database errors can't be told apart reliably across seven drivers, and a needless retry costs far less than losing a load to one dropped connection.

Masked data jobs retry like any other data job. The watermark is read again on each attempt, so a `DatabaseMemory` that fails once is retried too.


## Structured logs

`--log-format json` writes one object per line, for a log collector (for history and alerts, see [operations.md](../guides/run-on-a-schedule.md)):

```json
{"timestamp": "2026-09-16 01:00:12.514", "level": "INFO", "logger": "bauta", "message": "Completed loadOrders (4200 row(s))",
 "file": "pipeline.py", "line": 397, "job": "loadOrders", "status": "completed", "rowCount": 4200, "attempts": 1}
```

The fields are the point. Completions, failures and skips carry `job` and `status`; completions add `rowCount` and `attempts`, failures `error` and `durationSeconds`, and each cycle's summary its totals. Just before its completion, each job logs `stages`: the seconds it was busy [reading, masking, writing and waiting](../guides/watch-what-ran.md#where-the-time-went). A collector can alert on `status="failed"` or chart rows per job without parsing messages. Logs go to stderr; `--log FILE` adds a file. `--quiet` leaves only errors on stderr, one line each without a traceback, and a job's failure once rather than again for its last attempt: under cron, that is a mail when a run fails and none when it succeeds, while `--log` still records everything.


## Masking

Masking is a stage of a data job, between transform and load, rather than a separate kind of job. That one decision does most of the work:

- **Unmasked rows never reach the target**, not even its stage table. Masking happens in the ETL process's memory, a few chunks at a time.
- **It streams.** Memory stays bounded by `chunkSize`, however large the table.
- **It gets retries, watermarks, `--dry-run` and structured logs**, because data jobs already have them.
- **Masking in place is a `swap`.** Rows load into a stage table, which is then swapped with the original, so a failed run leaves the original untouched.

Every mask is derived from `HMAC(key, domain, value)`, keyed on the value itself rather than on the row's position. The same value therefore masks the same way in every table and on every run, which keeps joins working and makes runs reproducible. Keys are masked with a keyed permutation (a Feistel network), which can't produce collisions.

A policy must list **every column the query returns**, or the job fails before writing anything. A new production column should stop the job, not flow into a non-production copy unmasked.

[masking.md](../guides/mask-a-table.md) has the strategies, the key, the manifest, `audit`, `discover`, `subset`, `schema`, `synthesize` and `clear`.


## Moving values between drivers

Copying between different databases means one driver's values have to be accepted by another. Two connection settings make that work, and both apply to every job:

- **Oracle.** CLOB and BLOB columns are fetched as plain text and bytes rather than as LOB handles, which no other driver can load. The session's date formats are set to ISO 8601, so text such as `'2026-01-02 03:04:05'` loads into a `DATE` or `TIMESTAMP` column; that includes SQLite's dates and a `watermarkInitial` compared against a date column. This changes Oracle's implicit conversions between dates and text in both directions, so a `sourceQuery` that relied on the default `DD-MON-RR` format, or that calls `TO_CHAR` on a date without a format, now sees ISO text. Dates that arrive as datetime objects are unaffected.
- **JSON columns.** PostgreSQL's `json` and `jsonb` are read as their text, as MySQL, SQLite and DuckDB return JSON, rather than parsed into Python: parsed, the string `"123"` was loaded back as the number `123`, a bare number failed the job, and a number with a fraction passed through a float, so `12345678901234567890.123` arrived as `12345678901234567000`. The text crosses exactly, into any target; JSON Lines writes it as the JSON it is. A JSON column -- PostgreSQL's, MySQL's or DuckDB's -- masked with a policy other than `keep`, `json` or `null` has the value decoded before masking and the mask encoded as JSON after: `email` masks the address in `"ana@corp.example"` as it would in a text column, and returns a JSON string. MariaDB's JSON is text to its driver, and stays text. `discover` and `audit` parse JSON, to look for personal data inside.
- **DuckDB.** Each session's time zone is UTC. DuckDB's default is the machine's own, which it converts through when a time-zone-aware value meets a column without one, so the same job stored different times on machines in different zones.
- **Lists, dictionaries, UUIDs and times.** A PostgreSQL array, an Oracle JSON document or DuckDB's LIST, STRUCT and MAP arrive as lists and dictionaries, which SQLite's, MySQL's, Oracle's and SQL Server's drivers can't bind; they are written as JSON text, which is what `schema` maps them to. PostgreSQL writes a list as JSON into a `json` or `jsonb` column and as an array elsewhere. A UUID and a time of day are written as text for the drivers that refuse them.
- **SQLite.** `Decimal` values, which other drivers return for `NUMERIC` columns, are written as their exact text — and kept that way only by a column SQLite gives text affinity, which is what `schema` creates for a decimal. A column declared `DECIMAL(38,10)` has *numeric* affinity, and SQLite converts the text to an integer or a float as it stores it: `123456789012345678.1234567890` comes back as `123456789012345680`. Dates, timestamps and UUIDs are stored as ISO text, replacing Python's built-in converters, which are deprecated since 3.12.

`tests/integration/test_integration_schema.py` copies the same rows between every pair of the seven databases to keep this true.


## How names are written

A table name lives in two places, and they want opposite things. A statement needs it quoted, or a table called `group` is a syntax error. A catalog lookup — the primary key an upsert matches on, the columns a load fills, whether the table is there at all — binds it as a *value*, and a catalog holds names bare, so quoting one hides the table completely.

So a name given to bauta is read before it is used. It is split on the dot that separates schema from table, ignoring dots inside quotes; each part is then unquoted, or, if it was written plainly, folded the way that database folds an unquoted name — upper case on Oracle, lower case on PostgreSQL, unchanged elsewhere. That spelling is what a lookup binds. To build a statement, it is quoted again in that database's own style. A name no database would accept unquoted, such as one with a space, is taken as it is written, since it has no unquoted spelling to fold.

Two things follow. Writing `orders` means whatever the database means by `orders`, on all seven. Writing `"Orders"` means that exact table, and is the only way to name one whose case the database would otherwise fold.

A name longer than the target keeps is refused rather than used. Every database but SQLite and DuckDB, which have none, cuts one to its limit — 63 bytes on PostgreSQL, 64 characters on MySQL and MariaDB, 128 on Oracle and SQL Server — and none of them says so, so two names alike up to the limit are one table: two jobs would load over each other, and the second swap would rename over the first's rows.

The same reading builds the temporary name a swap renames through, so the suffix goes inside the quotes — `[group_tmp]`, never `[group]_tmp`, which SQL Server's parser refuses. `bauta schema` quotes the tables it creates as it already quoted their columns, and `subset` and `discover` quote the names they write into the jobs and queries they generate.


## Files as a target

A `files` connection is a directory, on this machine or in S3, Google Cloud Storage or Azure Blob Storage, that jobs write tables of files into -- Parquet, CSV or JSON Lines -- for a lake that Athena, Snowflake or Databricks reads, or a handoff. The pipeline is the same as for a database -- streamed, transformed, masked, a chunk at a time -- and only the load differs.

### What a run leaves where

```
<root>/
  _bauta_staging/<run>/                    a run's parts while it writes, and _PUBLISHING while an append moves them; gone when it ends
  crm/customer_events/                     append: every run's parts, side by side
    part-20260926T120000Z-a1b2c3-00001.parquet
    part-20260926T130000Z-d4e5f6-00001.parquet
  crm/customers/                           overwrite: one directory per complete snapshot
    snapshot=20260926T120000Z-a1b2c3/
      _SUCCESS                             what the run wrote: rows, files, column types
      part-20260926T120000Z-a1b2c3-00001.parquet
      part-20260926T120000Z-a1b2c3-00002.parquet
  exports/customers.parquet                singleFile: one file, replaced whole
```

A part's name ends in its format's extension: `.parquet`, `.csv` or `.ndjson`, and `.gz` after a text format's when it is gzipped, which is how Athena and Spark tell.

A run's name begins with when it started, in UTC, so names sort by time, and ends in six random characters so two runs in one second differ.

**Nothing is visible until the job succeeds.** Parts are written under `_bauta_staging`, outside every table's directory; an engine discovering the whole tree -- Spark, Hive, pyarrow -- skips it too, as it skips any name beginning with `_` or `.`, which is why a table's own path may not begin with either. `_SUCCESS` is skipped the same way, so it is never read as data. Once every row is written they are moved into place, and a failed job's staging is removed. A failure leaves the table as it was, and a retry starts over under a new run name. A process killed outright leaves its staging directory behind, out of sight; delete it when no run is going.

Locally, moving is a rename, so a reader opens a part whole or not at all. **An `append` moves its parts in one at a time**, so one failing *while* moving them has published some. It takes those back before it reports the failure. A process killed among the moves can't, so before its first move it lists them in `_PUBLISHING` in its staging directory, and the next run of the table removes whatever that list names, and the staging with it, before it writes, logging how many parts it took back. That run starts from the same watermark and appends the same rows again, once. A table's run touches only its own table's leftovers.

**In a cloud** the same holds, by other means. An object store has no rename: a part is moved by copying it within the bucket and deleting the staged one, and an object appears only whole, once its copy completes. pyarrow does that itself on S3 and GCS; on Azure without a hierarchical namespace it can't move at all, so bauta copies and deletes, and on an account with one (ADLS Gen2) a move is a rename, as on disk. The copy bounds a part's size where it is one request: 5 GiB on S3, and 256 MiB on Azure with pyarrow before 19, which bounds `fileSize` and a `singleFile` table; GCS, and Azure from pyarrow 19, copy any size.

Staging matters more in a cloud than on disk: an upload pyarrow is made to stop is completed rather than abandoned, in all three -- it has no way to abort one -- so a part written in its final place would appear, cut short, whenever its job failed. Written in staging, the cut-short part is deleted with it.

Object stores have no directories, and nothing bauta does creates the empty objects some tools write to stand for them, which readers may list as files -- with one exception: pyarrow, deleting the last object under a prefix, may leave one in its place. Only staging is emptied that way, so the one it leaves is `<root>/_bauta_staging/`, outside every table. For the same reason, bauta finds a table's snapshots from the files under it rather than asking for directories.

**Parts and row groups.** A job holds rows until they reach `rowGroupSize` in memory, then writes them as one row group; a part is closed when it reaches `fileSize` on disk and the next begun. Memory stays at about one row group plus the pipeline's chunks, however large the table. Row groups are what engines skip by, using the minimum and maximum each records for every column, so a job copying in a useful order -- by date, say -- lets a reader's filter skip most of the table.

### Reading an overwrite job's table

Each run of an `overwrite` job publishes a complete snapshot of the table beside the previous ones, writes its `_SUCCESS` last, and then removes all but the newest `keepSnapshots`, along with any older snapshot a crashed run left without a `_SUCCESS`. The table is never half-replaced; the price is that a reader must choose a snapshot. Pointed at the table's directory as a whole, a reader sees every snapshot kept, each row once per snapshot.

- **Athena, Spark, Databricks, Trino:** declare `snapshot` as a partition column (it is a Hive-style `name=value` directory) and read `where snapshot = (select max(snapshot) ...)`, or keep a view that does.
- **Snowflake:** an external table over the table's directory with `snapshot` derived from the file path, filtered the same way; or load the newest snapshot's path with `COPY INTO`.
- **Anything reading one file:** `singleFile: true`, where the table is small enough to be one file. It is replaced by a rename, or on S3 a copy that replaces the object as it completes, so a reader sees the old file or the new one.

`keepSnapshots: 1` removes the previous snapshot as soon as the new one is complete, and a query still reading it then fails; the default of 2 leaves it for the next run to remove.

Atomic replacement of a table read in place, and upserts, are what table formats such as Iceberg and Delta Lake are for; plain Parquet has neither.

### Appends and duplicate rows

An `append` job adds new parts each run and never rewrites old ones, so nothing stops a row from arriving twice:

- the [lookback window](#why-the-lookback-window) of an incremental job re-reads its overlap every run, on purpose;
- a job that dies between publishing and recording its watermark is run again from the old watermark. One that fails or is killed *while* publishing is not a cause: its parts are [taken back](#what-a-run-leaves-where).

Against a database, `upsert` makes both harmless. In an append-only table, readers take the latest version of each key, by the watermark column:

```sql
select * from (
  select *, row_number() over (partition by id order by updatedAt desc) as newest
  from customer_events
) where newest = 1
```

Hard deletes are as invisible as in any incremental load.

### Column types

A Parquet file has one schema, and a table's parts should share it, so each column's type is fixed before the first row group is written and never changes:

- **declared** in `targetColumnTypes`, or else
- **settled by the first chunk that holds a value in the column**, from the Python values the driver returned, after transforms and masking:

| Values | Written as |
| --- | --- |
| `int` | `int64`, or `decimal(38,0)` where one is past 64 bits |
| `float`, or `float` with `int` | `float64` |
| `Decimal`, or `Decimal` with `int` | `decimal(p,s)` as the driver reports it (PostgreSQL, Oracle, DuckDB), else `decimal(38,s)`, `s` the larger of 10 and the widest scale in that chunk |
| `str`, or text with numbers (SQLite) | `string` |
| `bytes` | `binary` |
| `bool` | `bool` |
| `date` | `date` |
| `datetime` without a time zone | `timestamp`, the wall-clock value as the source held it |
| `datetime` with a time zone | `timestamptz`, converted to UTC |
| `time` | `time` |
| a JSON document or list, a UUID, an interval | `string`: JSON text, the UUID's text, `HH:MM:SS[.ffffff]` |

A value that doesn't fit its column's type fails the job, naming the column and the type to declare, never the value. That includes a float in an integer column -- which pyarrow on its own would write as its truncation -- a decimal with more places than its scale, and a time with a time zone in a column without one. A declared `decimal` takes floats, by their shortest text (`0.1` is `0.1`), and a declared `string` takes numbers and dates as their text.

A column with no value in the rows before the first row group is written has nothing to be settled by. It is written as `string`, with a warning naming it; a later value there that isn't text fails the job, rather than becoming its text unseen. Declare such a column's type if it holds anything else.

`targetColumnTypes` names columns as the file does -- after `targetColumns` -- matched ignoring case, and naming one the table doesn't have is an error.

### Formats

`format` is a connection's: every table it holds is written one way. Parquet keeps each column's type; CSV and JSON Lines are text, so every type is spelled one way in both, and a reader told the column types reads it back exact:

| Type | CSV | JSON Lines |
| --- | --- | --- |
| null | an empty, unquoted field | `null` |
| empty text | `""`, quoted, so it isn't read as null | `""` |
| decimal | its digits, to the column's scale: `12.30` | a string, `"12.30"`: a JSON number is a float to most readers, and money would lose cents |
| float | `1.5`, `nan`, `inf` | `1.5`; JSON has no NaN or infinity, so `"NaN"`, `"Infinity"`, `"-Infinity"`, as BigQuery and Snowflake read them |
| timestamp | `2026-01-02 03:04:05.000006` | the same, as a string |
| timestamptz | `2026-01-02 03:04:05.000006Z`, in UTC | the same, as a string |
| date, time | `2026-01-02`, `03:04:05.000000` | the same, as strings |
| bool | `true`, `false` | `true`, `false` |
| binary | base64 | base64, as a string |
| a JSON document or list | its JSON text | nested, as itself |

A timestamp has a space where ISO 8601 has a `T`: it is what Hive, Athena and Spark read as a timestamp in text, and Snowflake and BigQuery read either.

CSV quotes every text value, doubles a quote within one, and writes a header in every part, so each part reads on its own. JSON Lines writes every column in every line, in the table's order.

A JSON document is nested only in a column that has held one -- a dictionary or list from the driver, such as PostgreSQL's `jsonb` -- and only where its text is a JSON object or array; anything else there, and every other text column, is written as a string.


## Iceberg tables

An `iceberg` connection writes Iceberg tables through a catalog, with pyiceberg: no JVM and no Spark. The rows go through the same pipeline and the same [column types](#column-types) as a files connection; only the commit differs.

### One commit a run

`append` and `overwrite` write the run's Parquet parts straight into the table's data directory, a row group at a time, with the writer a files connection uses, and register them all in one commit at the end -- `overwrite` deleting the table's rows in that same commit. A reader sees the run whole or not at all, and never the table empty between the delete and the add. A table that isn't there is created by that commit, so a failed first run leaves no table either.

Nothing needs staging, as files do: a data file no commit names is never read. A run that fails deletes the parts it wrote on its way out. A process killed outright leaves them: never read, but taking room until a tool that removes a table's orphan files -- Spark's `remove_orphan_files`, a catalog's maintenance -- deletes them.

`upsert` is the exception. pyiceberg merges a table held in memory, so a run merges -- and commits -- a row group at a time, keeping the last row of a key that repeats within one. A run that fails part-way leaves the row groups it merged, as a stage-less upsert into a database does; the next run, from the same watermark, merges them again, harmlessly.

### The table's schema

A table that exists decides how each column is written: its own types, spelled as it spells them, whatever the query's values would settle. A declared `targetColumnTypes` it disagrees with, a column it requires that the query doesn't return, and a column the query returns that it lacks are each refused before a row is written -- the last unless the connection says `evolveSchema`, which adds it.

A table bauta creates takes the columns' settled types, with the integers Iceberg has no type for written as the nearest it has: `int8` and `int16` as `int`, `uint32` as `long`, `uint64` as `decimal(20,0)`. Its identifier fields are `targetKey`, and a key column can't be null. It deletes its oldest metadata files as it commits (`write.metadata.delete-after-commit.enabled`), which pyiceberg otherwise keeps forever.

### Old snapshots

After each run the table keeps its newest `keepSnapshots` snapshots and expires the rest -- and bauta deletes the data files only the expired ones referenced, which pyiceberg leaves behind. Without that, a row deleted or overwritten would stay readable in them: someone's data a request asked to remove, or a value masked under a key since rotated. A query still reading an expired snapshot fails, so `keepSnapshots` is also how long a slow reader has.

### Catalogs and storage

Glue, REST and SQL catalogs are loaded with pyiceberg's own; the connection's cloud settings reach them under pyiceberg's names, and `properties` pass anything else through. Files are written with pyarrow's filesystems, as a files connection's are, for every cloud: pyiceberg would otherwise reach for fsspec's for Azure, which needs another package, and hands pyarrow an Azure path it can't read -- `bauta.lake.iceberg.FileIO` corrects that one spelling. An `az://` warehouse is given to pyiceberg as the `abfss://` URL it stands for.

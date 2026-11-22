# How it works, and why

The behaviour behind the fields in [the configuration reference](../reference/configuration.md), and the consequences worth knowing before you rely on it.

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
- [Files as a source](#files-as-a-source)
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
- **DuckDB loads each chunk as an Arrow table** with one `INSERT ... SELECT` (or `... ON CONFLICT` for an upsert): 50,000 rows in a tenth of a second, against 13 seconds row by row. A column Arrow can't give one type, such as SQLite's mix of numbers and text, goes as text for DuckDB to cast; only a column mixing in something else, such as bytes, sends its chunk row by row.

One statement can't update a row twice, so for all three, rows repeating a key within a chunk are first reduced to the last of them — what applying them in turn would leave.

**Don't write a `sourceQuery` that repeats a key**: the two upsert paths answer it differently. A stage-less `upsert` keeps the last row of each key, as above; one with a `targetTableStage` loads the stage first, whose primary key refuses the repeat, loudly and before anything reaches the target. Deduplicate in the query.

**An upsert matches rows by primary key alone.** A new row whose other unique key (an email, say) another row already holds is refused, and the load fails without retrying, on every database. On MySQL and MariaDB, whose `ON DUPLICATE KEY UPDATE` fires on any unique key and would silently overwrite the other row, a guard fails the statement instead. Two rows sharing a unique value usually means a masked value collided, or the target's unique keys differ from the source's.

Where the [native masker](../guides/make-it-faster.md#the-native-masker) is installed, the three stages overlap: masking moves to a worker thread while the reader and writer keep the connections (some drivers, and SQLite, forbid sharing one across threads). Drivers release the GIL while waiting on a socket, and the native masker releases it for a whole chunk, so waiting and masking genuinely overlap. A job then holds about four chunks: one being read, two masked ahead of the writer, one being written. Pure-Python masking is too slow for the overlap to help, so it stays sequential; `BAUTA_PIPELINE` overrides either default.

Chunks are still masked one at a time and written in the order they were read, so a key repeating across chunks reaches a stage-less upsert as it was read. The native masker masks a chunk's columns in one call; with [`maskingThreads`](../guides/make-it-faster.md#masking-threads) above 1 it spreads them over threads, which changes the speed, never the result.

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

On Oracle, a rename that fails (another session holding the table, `ORA-00054`) undoes the renames already done, so both tables end where they started; the job fails, naming the statement. A job stopped for exceeding `timeoutSeconds` gets SIGTERM, which waits for the renames to finish. A process killed outright between two renames (out of memory, or the SIGKILL that follows five seconds later) leaves a table under the temporary name. **The next run puts it right before loading**, and logs what it did: with the target missing, it finishes the swap, since the temporary table holds a completed load; with the stage missing, it undoes it. All three tables present is no state a swap leaves, so the job fails, naming the temporary table to remove.

**Views.** PostgreSQL ties a view to the table, not its name, so the swap recreates each view built directly on the target, in the same transaction, keeping its grants and the views built on it. SQLite rewrites views to follow a renamed table, so the swap renames with `legacy_alter_table` on, leaving them naming the target. The other dialects resolve views by name and need nothing.

**DuckDB can't swap a table with an index or in a foreign key**, either side of it: it refuses some of those renames and mishandles the rest. Such a swap job fails before renaming anything, saying to use `upsert`. A primary key is no obstacle.

**What isn't rebound on PostgreSQL:** materialized views, which keep reading the old table until recreated.

**What isn't rebound anywhere:** foreign keys in other tables that reference the target. They follow the old table to the stage's name, stop checking the new target, and stop the next run emptying the stage, which every database refuses while it is referenced. Recreate them in `postTargetAdhocQueries`, or use `upsert` with a stage table. `audit --connect` [reports](../guides/prove-the-copy-is-safe.md#reviewing-policies-audit) such a swap job.

**A `postTargetAdhocQuery` that fails after the swap** fails the job, but the target already holds the new rows; the failure says so and reports the rows loaded.

**The swapped table keeps its primary key and unique keys.** Before loading, the stage is given whichever of the target's it lacks (a stage made with `CREATE TABLE ... AS SELECT`, say, or the unique constraints [`--stage-suffix`](../guides/copy-a-subset.md#schema-creating-the-targets-tables) leaves out), so the live table has them after every swap, not every other one, and a repeated key fails the load before reaching the target.

- **Each is added unnamed**, so the database names it as it would any constraint, and no name can collide with one the target already has. The two tables then trade constraint names at every swap, as they trade table names.
- **PostgreSQL, MySQL, MariaDB, Oracle and SQL Server** add them with `ALTER TABLE` (SQL Server first making key columns NOT NULL). **SQLite and DuckDB**, which can't, recreate the empty stage with the keys added.
- **Not copied:** a unique index on an expression, a partial or filtered one, or one over a prefix of a column, which a column list can't recreate; and a DuckDB `CREATE UNIQUE INDEX`, since DuckDB won't swap a table with an index at all.

**The swapped table keeps its grants and plain indexes too**, or they would be there only every other run. Before loading, the stage is given the target's table grants (schema and database grants cover it already) and its plain, non-unique column indexes, named as the target's with `_s`: B-tree on PostgreSQL, MySQL's and MariaDB's, Oracle's normal ones and SQL Server's non-clustered ones. Expression, partial, GIN or columnstore indexes are not copied, nor SQLite's, whose names are database-wide. A grant or index that can't be given is a warning, and the swap goes on.
- **A key that can't be added** -- no privilege to alter the stage, say -- is a warning, and the swap goes ahead as it would have.

**The swapped table's foreign keys alternate.** A stage is given none (a key follows the table it was declared on, so one on the stage of a swapped parent would check the emptied old table), so every other run the copy enforces none, silently. `audit --connect` [warns](../guides/prove-the-copy-is-safe.md#reviewing-policies-audit) about such a swap job. Recreate them in `postTargetAdhocQueries`, or use `upsert` with a stage table where references matter.


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
- A database target is loaded into its `targetTableStage` and [swapped](#how-a-swap-works) in, so readers see the old copy until the new one is whole. The job needs a `targetTableStage` and the table created (`bauta schema --stage-suffix`); without one, the refresh is refused before anything runs, and [`bauta audit`](../guides/prove-the-copy-is-safe.md#reviewing-policies-audit) warns of it beforehand. An Iceberg target takes one overwrite commit. Incremental jobs writing files are refused: appended parts and snapshot directories are different layouts to a reader.
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

In run state: `jobs.yaml`'s `memory`, a file or a table (see [run state](../guides/run-on-a-schedule.md#run-state)); from Python, a `MemoryBackend`. A file is replaced through a temporary one, so a process killed mid-write leaves the previous version. It needs a filesystem that persists between runs and that every worker shares; where it doesn't (a container without a volume, several machines, serverless), keep run state in a table. A lost file fails nothing: every job silently re-extracts from `watermarkInitial`. See [memory backends](../reference/python-api.md#memory-backends).


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

`--forever` keeps the process resident, for freshness below cron's one-minute floor or where there's no scheduler. History, the manifest and notifications are written as each cycle ends; a cycle with nothing due writes none, and is logged only at `--log-level debug`.

**Stopping.** On `SIGINT` or `SIGTERM`, a run starts no new jobs, lets the running ones finish, reports the rest as skipped, and exits with status 130, rather than leave a half-loaded table. A container's grace period must cover the longest job; give long jobs a `timeoutSeconds` shorter than it, so a hung one can't hold the shutdown. Between `--forever` cycles, a signal ends the pause within a second.

**Overlapping runs.** `run` holds a lock beside its run state (`memory.yaml.run.lock`; see [run state](../guides/run-on-a-schedule.md#run-state)) while it runs, so a second run sharing that state exits with status 1 rather than run the same jobs at once, as a cron interval shorter than a slow run would cause. The operating system releases the lock if the process dies.

A skipped job exits non-zero just as a failed one does: it didn't run, so its data isn't there.


## Workers

Each job runs in a process of its own once its predecessors have completed, a `workers` slot is free, and each connection it uses is below its [`maxConcurrentJobs`](../reference/connections.md) (always 1 for DuckDB). A job held back by a connection doesn't hold back jobs using others. A process of its own lets each job be ended on its own:

- **A job that dies** — killed for memory, crashed in a driver — fails, and only that job. Its dependents are skipped, and the run still ends; it doesn't wait for an outcome that will never come.
- **A job past its `timeoutSeconds`** (retries included) is sent `SIGTERM`, then `SIGKILL` five seconds later. It fails with a `Timeout` error and its dependents are skipped. Each server rolls back what it hadn't committed; what it had committed stays, as for any failure part-way (see [how a data job moves rows](#how-a-data-job-moves-rows)).

Within its process, a job masks on one thread, or on several with [`maskingThreads`](../guides/make-it-faster.md#masking-threads); `auto` divides half the cores between the jobs running when each starts.

Processes are forked from a **forkserver**, started once per run with bauta already imported, so a job starts in milliseconds instead of re-importing it (about 200 ms a job: 60 small jobs took 13 seconds rather than 1.5). Unlike plain `fork`, it is single-threaded when it forks, so a child can't inherit a held lock. Each job gets the run's environment as it is when the job starts. Where there is no forkserver, or `BAUTA_START_METHOD=spawn`, each job starts afresh. Either way a job re-imports the program that started the run, as `__mp_main__`, so a program embedding the library needs an `if __name__ == '__main__':` guard; see [running jobs](../reference/python-api.md#running-jobs).

**Logs from jobs** are sent back to the main process and written by its handlers, so they follow `--log`, `--log-format` and `--quiet` like everything else.

**Ctrl-C** reaches every process in the terminal's group; jobs ignore it and leave the decision to the main process, as described under stopping above.

**A run killed outright** (`kill -9`, an out-of-memory kill) takes its jobs with it: each watches a pipe from the run and ends the moment it closes, writing no further rows, while each server rolls back what it hadn't committed. That matters because the run lock dies with the run, so the next `bauta run` can start at once, and an orphaned job would write over it.


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

Ranges, not a modulo, so each slice reads only its own rows from an index on the column, where `MOD(id, 8) = 3` would have every slice scan the table. The first slice has no lower bound and takes the nulls too; the last has no upper bound; so every row is read exactly once, including rows added after the bounds were read. Slices are only as even as the values: a large gap in the key leaves the slices around it light. A column with fewer integers between its bounds than `count` gets fewer slices, and a query returning no rows gets one.

The column must be among those the query returns. **A number** is sliced into equal ranges, as above. **Any other column** (a UUID key, text, a date) is sliced where the database's own order deals its values into `count` even shares (`NTILE`), at the cost of one ordered pass over the column first, which an index serves. These bounds are data, so they are bound as parameters, never written into the query or logged. `partitions: auto` slices a UUID primary key the same way.

**What it costs.** The bounds query: next to nothing where the column is indexed and the query reads one table, and a whole extra pass where the database has to evaluate a join or an aggregate to answer it. And the derived table: SQL Server refuses one holding an `ORDER BY` (without `TOP`) or a `WITH` clause, so write a partitioned job's query without them there.

**Each slice is a reader, masker and writer of its own**, on threads of the job's process, with its own source and target connections. Every slice writes into the one table the job loads: the stage, or the live table for a stage-less `upsert`. Files and Iceberg targets refuse `partitions`, since what they publish is what one writer wrote.

**All or nothing.** The job succeeds only once every slice has loaded. The first slice to fail stops the others before their next write, and its error is the job's: nothing is swapped in or upserted from the stage, no watermark or run is recorded, and a [retry](#retries) starts every slice over, as it starts any job over. A stage-less upsert keeps what its slices wrote before the failure, as it would unpartitioned. An incremental job's watermark is the highest any slice read.

**Masks are the same.** Every mask is derived from its value alone, so a partitioned copy is byte for byte the copy one stream makes. `shuffle`, which shuffles values within a chunk, is the exception, since a slice's chunks hold other rows; each slice numbers its chunks apart from every other's, so no two chunks of a job are shuffled alike.

**No longer one snapshot.** Each slice reads in its own transaction, so the copy is not one consistent view, and a row whose partition column changes mid-run can be read twice or not at all: partition on a column that doesn't change, such as the primary key. Concurrent upserts into one table can deadlock on SQL Server and MySQL; the attempt is retried, but a stage table avoids it.

**Workers, connections and threads.** A partitioned job is one job: one `workers` slot, one process, one `timeoutSeconds`. But each slice holds a place in each connection's [`maxConcurrentJobs`](../reference/connections.md), so `validate` refuses a job with more slices than a connection allows, and any partitioned job on DuckDB. With `maskingThreads: 1` each slice masks on its own thread; above 1 the slices share the job's threads, unless `auto` gave it no more threads than slices ([partitions: auto](../guides/make-it-faster.md#partitions-auto)). Masking in Python holds the interpreter lock, so slices overlap only their reading and writing; with the [native masker](../guides/make-it-faster.md#the-native-masker) they overlap masking too. See [partitions](../guides/make-it-faster.md#partitions) for what it gains.


## Retries

A data job with `retries: 3` gets up to four attempts. The delay starts at `retryDelaySeconds` and doubles, so a database that's down isn't hit at a fixed interval while it recovers, up to five minutes between attempts.

Retrying a whole job is safe because both strategies converge on a re-run: `swap` restages and re-swaps, and `upsert` reapplies existing rows as a no-op.

**What isn't retried:** configuration, transform and masking errors (a target without a primary key among them). They come from this package and fail the same way every time, so a retry would only bury the message. Everything a driver raises *is* retried: transient and permanent database errors can't be told apart reliably across seven drivers, and a needless retry costs less than a load lost to one dropped connection.

Masked data jobs retry like any other data job. The watermark is read again on each attempt, so a `DatabaseMemory` that fails once is retried too.


## Structured logs

`--log-format json` writes one object per line, for a log collector (for history and alerts, see [run on a schedule](../guides/run-on-a-schedule.md)):

```json
{"timestamp": "2026-09-16 01:00:12.514", "level": "INFO", "logger": "bauta", "message": "Completed loadOrders (4200 row(s))",
 "file": "pipeline.py", "line": 397, "job": "loadOrders", "status": "completed", "rowCount": 4200, "attempts": 1}
```

Completions, failures and skips carry `job` and `status`; completions add `rowCount` and `attempts`, failures `error` and `durationSeconds`, and each cycle's summary its totals. Each job also logs `stages`, the seconds it spent [reading, masking, writing and waiting](../guides/watch-what-ran.md#where-the-time-went). So a collector can alert on `status="failed"` without parsing messages. Logs go to stderr, and `--log FILE` adds a file. `--quiet` leaves only errors on stderr, one line each, a job's failure once: under cron, a mail when a run fails and none when it succeeds, while `--log` still records everything.


## Masking

Masking is a stage of a data job, between transform and load, rather than a separate kind of job. That one decision does most of the work:

- **Unmasked rows never reach the target**, not even its stage table. Masking happens in the ETL process's memory, a few chunks at a time.
- **It streams.** Memory stays bounded by `chunkSize`, however large the table.
- **It gets retries, watermarks, `--dry-run` and structured logs**, because data jobs already have them.
- **Masking in place is a `swap`.** Rows load into a stage table, which is then swapped with the original, so a failed run leaves the original untouched.

Every mask is derived from `HMAC(key, domain, value)`, keyed on the value itself rather than on the row's position. The same value therefore masks the same way in every table and on every run, which keeps joins working and makes runs reproducible. Keys are masked with a keyed permutation (a Feistel network), which can't produce collisions.

A policy must list **every column the query returns**, or the job fails before writing anything. A new production column should stop the job, not flow into a non-production copy unmasked.

The [guides](../guides/mask-a-table.md) take masking, the key, the manifest and the commands around them in turn.


## Moving values between drivers

Copying between different databases means one driver's values have to be accepted by another. Two connection settings make that work, and both apply to every job:

- **Oracle.** CLOB and BLOB columns are fetched as text and bytes, not as LOB handles no other driver can load. The session's date formats are ISO 8601, so text such as SQLite's dates or a `watermarkInitial` loads into a `DATE` or `TIMESTAMP`. That changes Oracle's implicit conversions both ways: a `sourceQuery` relying on the default `DD-MON-RR`, or on `TO_CHAR` without a format, now sees ISO text.
- **JSON columns.** PostgreSQL's `json` and `jsonb` are read as text, as the other databases return JSON, so a value crosses exactly: parsed, `"123"` came back as `123` and `12345678901234567890.123` lost digits through a float. A JSON column masked with anything but `keep`, `json` or `null` is decoded before masking and encoded after, so `email` masks the address in `"ana@corp.example"` and returns a JSON string. MariaDB's JSON is text to its driver, and stays text.
- **DuckDB.** Each session's time zone is UTC, not the machine's, so the same job stores the same times wherever it runs.
- **Lists, dictionaries, UUIDs and times.** A PostgreSQL array, an Oracle JSON document or DuckDB's LIST, STRUCT and MAP arrive as lists and dictionaries, which SQLite's, MySQL's, Oracle's and SQL Server's drivers can't bind; they are written as JSON text, which is what `schema` maps them to. PostgreSQL writes a list as JSON into a `json` or `jsonb` column and as an array elsewhere. A UUID and a time of day are written as text for the drivers that refuse them.
- **SQLite.** A `Decimal` is written as its exact text, which stays exact only in a column of text affinity, as `schema` creates for a decimal: a `DECIMAL(38,10)` column has *numeric* affinity, and rounds `123456789012345678.1234567890` to `123456789012345680`. Dates, timestamps and UUIDs are stored as ISO text.

`tests/integration/test_integration_schema.py` copies the same rows between every pair of the seven databases to keep this true.


## How names are written

A statement needs a table name quoted, or a table called `group` is a syntax error; a catalog lookup binds it as a bare *value*, which quoting would hide. So a name is read once: split into schema and table (ignoring dots inside quotes), each part unquoted, or, written plainly, folded as the database folds an unquoted name (upper case on Oracle, lower on PostgreSQL). Lookups bind that spelling; statements quote it again in the database's style.

So `orders` means whatever the database means by `orders`, on all seven, and `"Orders"` means that exact table, the only way to name one whose case the database would fold.

A name longer than the target keeps is refused. Databases silently cut one to their limit (63 bytes on PostgreSQL, 64 characters on MySQL and MariaDB, 128 on Oracle and SQL Server), so two names alike up to it would be one table. `schema`, `subset` and `discover` quote the names they write the same way.


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

**Nothing is visible until the job succeeds.** Parts are written under `_bauta_staging`, outside every table, and moved into place once every row is written. Engines skip names beginning with `_` or `.` (so a table's path may not begin with either), which keeps both staging and `_SUCCESS` from being read as data. A failure leaves the table as it was and removes its staging; a retry starts over under a new run name. A process killed outright leaves its staging behind, out of sight; delete it when no run is going.

Locally, a move is a rename, so a reader sees a part whole or not at all. **An `append` moves its parts one at a time**; one failing partway takes back what it moved. One killed partway can't, so it lists its moves in `_PUBLISHING` first, and the table's next run removes what that names before it writes, then appends the same rows again from the same watermark.

**In a cloud** the same holds by other means. An object store has no rename, so a part is moved by copying it within the bucket and deleting the staged one; an object appears only whole. (On Azure with a hierarchical namespace, ADLS Gen2, a move is a rename.) A copy is one request, which bounds a part's size: 5 GiB on S3, and 256 MiB on Azure with pyarrow before 19, which limits `fileSize` and a `singleFile` table.

Staging matters more in a cloud: pyarrow can't abort an upload, only complete it, so a part written in its final place would appear cut short whenever its job failed. In staging, it is deleted with the rest.

**Parts and row groups.** A job holds rows until they reach `rowGroupSize` in memory, then writes them as one row group; a part is closed when it reaches `fileSize` on disk and the next begun. Memory stays at about one row group plus the pipeline's chunks, however large the table. Row groups are what engines skip by, using the minimum and maximum each records for every column, so a job copying in a useful order -- by date, say -- lets a reader's filter skip most of the table.

### Reading an overwrite job's table

Each run of an `overwrite` job publishes a complete snapshot beside the previous ones, writes its `_SUCCESS` last, then removes all but the newest `keepSnapshots` (and any snapshot a crashed run left without `_SUCCESS`). The table is never half-replaced, but a reader must choose a snapshot: pointed at the whole directory, it sees each row once per snapshot kept.

- **Athena, Spark, Databricks, Trino:** declare `snapshot` as a partition column (it is a Hive-style `name=value` directory) and read `where snapshot = (select max(snapshot) ...)`, or keep a view that does.
- **Snowflake:** an external table over the table's directory with `snapshot` derived from the file path, filtered the same way; or load the newest snapshot's path with `COPY INTO`.
- **Anything reading one file:** `singleFile: true`, for a table small enough to be one file, replaced so a reader sees the old file or the new one.

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

A value that doesn't fit its column's type (a float in an integer column, a decimal with more places than its scale, a zoned time in an unzoned column) fails the job, naming the column and the type to declare, never the value. A declared `decimal` takes floats by their shortest text (`0.1` is `0.1`), and a declared `string` takes numbers and dates as text.

A column holding no value before the first row group is written is written as `string`, with a warning; a later value there that isn't text fails the job. Declare its type if it holds anything else.

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

A JSON document is nested only in a column that has held one (such as PostgreSQL's `jsonb`), and only where it is an object or array; everything else is written as a string.


## Files as a source

A job can name a `files` connection as its `sourceConnection`, and read its tables with SQL. Past the query, nothing differs from reading a database: rows are streamed a chunk at a time, transformed, and masked, every column must be covered, and watermarks and partitions work as they do elsewhere.

```yaml
copyOrders:
  sourceConnection: lake
  sourceQuery: SELECT id, email, total FROM "sales/orders" WHERE id > {{ watermark }}
  watermarkColumn: id
  watermarkInitial: 0
  targetConnection: staging
  targetTableFinal: orders
  insertStrategy: upsert
  masking:
    key: ${MASKING_KEY}
    columns: {id: keep, email: email, total: keep}
```

The query is DuckDB's SQL, answered by an in-memory DuckDB over the connection's files, read through pyarrow with the connection's own credentials. A table is named by its path under `root`, quoted where it holds anything but letters, digits and underscores, and is one of:

- **A directory of files,** every file under it unless a part of its path begins with `_` or `.`, which is how Spark and Hive mark their own. Directories named `column=value` become columns.
- **What an overwrite job left:** its newest complete snapshot alone, since each holds the whole table.
- **A single file,** by its own name, or as `singleFile` names it: `orders` for `orders.parquet`.

All of a connection's tables are read in its `format`, with its `delimiter` for CSV; gzipped text is told by a `.gz` name. Every CSV column is read as text, so a zip code keeps its leading zeros: `CAST` in the query for a number or date, a watermark column included, since `'10'` sorts before `'9'` as text. An empty field is NULL, a quoted empty one empty text. A table's columns are those of all its files: a column a file lacks reads as NULL, and files disagreeing on a column's type are refused.

The query reaches nothing but the tables under `root`: DuckDB's access to other files is turned off before it runs, so `read_csv('/etc/passwd')` is refused, and so is writing a file. A dry run asks a connection jobs only read to be listable, not writable, and `readOnly: true` on it refuses any job writing there.

Files have no catalog to read, so `discover --update` and `verify-references` pass over these jobs and `coverage` refuses the connection: name the columns in the masking policy yourself, and `audit --connect` will check each against what the query returns.


## Iceberg tables

An `iceberg` connection writes Iceberg tables through a catalog, with pyiceberg: no JVM and no Spark. The rows go through the same pipeline and the same [column types](#column-types) as a files connection; only the commit differs.

### One commit a run

`append` and `overwrite` write the run's Parquet parts straight into the table's data directory, a row group at a time, with the writer a files connection uses, and register them all in one commit at the end -- `overwrite` deleting the table's rows in that same commit. A reader sees the run whole or not at all, and never the table empty between the delete and the add. A table that isn't there is created by that commit, so a failed first run leaves no table either.

Nothing needs staging: a data file no commit names is never read. A failed run deletes its parts; one killed outright leaves them, unread, until an orphan-file cleanup (Spark's `remove_orphan_files`, a catalog's maintenance) removes them.

`upsert` is the exception. pyiceberg merges a table held in memory, so a run merges -- and commits -- a row group at a time, keeping the last row of a key that repeats within one. A run that fails part-way leaves the row groups it merged, as a stage-less upsert into a database does; the next run, from the same watermark, merges them again, harmlessly.

### The table's schema

An existing table decides how each column is written, in its own types. A declared `targetColumnTypes` it disagrees with, a required column the query doesn't return, and a returned column it lacks are each refused before a row is written; the last is added instead under `evolveSchema`.

A table bauta creates takes the columns' settled types, with the integers Iceberg has no type for written as the nearest it has: `int8` and `int16` as `int`, `uint32` as `long`, `uint64` as `decimal(20,0)`. Its identifier fields are `targetKey`, and a key column can't be null. It deletes its oldest metadata files as it commits (`write.metadata.delete-after-commit.enabled`), which pyiceberg otherwise keeps forever.

### Old snapshots

After each run the table keeps its newest `keepSnapshots` snapshots and expires the rest, and bauta deletes the data files only those referenced, which pyiceberg leaves behind. Otherwise a deleted or overwritten row would stay readable in them: data someone asked to have removed, or a value masked under a rotated key. A query still reading an expired snapshot fails, so `keepSnapshots` is also how long a slow reader has.

### Catalogs and storage

Glue, REST and SQL catalogs are loaded with pyiceberg's own; the connection's cloud settings reach them under pyiceberg's names, and `properties` pass anything else through. Files are written with pyarrow's filesystems, as a files connection's are, in every cloud; an `az://` warehouse is given to pyiceberg as the `abfss://` URL it stands for.

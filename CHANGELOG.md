# Changelog

What changed in each release of `bauta` and `bauta-rs`, which are always released together at the same version. **Breaking** lists what can stop an existing setup from working after an upgrade; read it before upgrading.

Masks never change between releases unless an entry here says so: the same value, key and domain give the same mask in every version so far.


## 0.1.9 — 2026-09-26

DuckDB joins the six databases as a source or target, and connections replace databases as the unit of configuration: `connections.yaml`, `sourceConnection` and `targetConnection`, and settings checked per connection type. Copies between databases load lists, JSON, UUIDs, times and unsigned integers that failed before. **Read Breaking before upgrading: every configuration needs editing.** No mask changes.

### Breaking
- **`database.yaml` is now `connections.yaml`**, and a job's `sourceDatabase` and `targetDatabase` are now `sourceConnection` and `targetConnection`, in `defaults:` too. The old names are not read: `validate` refuses them as unknown settings. Rename the file, then search and replace the two settings.
- **The flags follow:** `--databases` is `--connections`, and `--memory-database`, `--history-database` and `--manifest-database` are `--memory-connection`, `--history-connection` and `--manifest-connection`. A `memory`, `history` or `manifest` table names its alias with `connection:` rather than `database:`.
- **Each connection type takes only its own settings.** SQLite and DuckDB name their file with `path` rather than `database`, and Oracle, which is reached by `serviceName` or `sid`, no longer takes the `database` it never used. A setting of another type -- a `serviceName` on a PostgreSQL connection, a `host` on a SQLite one -- is refused naming the types it belongs to; it used to be accepted and ignored. From Python, `DatabaseConnectionConfig` is replaced by one class per type (`PostgreSQLConnection`, `SQLiteConnection`, ...), `ConnectionConfig` is their union, and `connectionConfig(type=..., ...)` builds one from settings as the file would give them.
- **`discover`, `subset`, `schema`, `synthesize` and `coverage` take `--connection ALIAS`** where they took `--database ALIAS`. From Python, `Configuration.validateDatabaseConfiguration` is `validateConnectionConfiguration`, and `validateJobGraph` takes `connections=` rather than `databases=`.

### Added
- **DuckDB** (`type: duckdb`, `pip install "bauta[duckdb]"`), as source or target of any job, and for `schema`, `subset`, `discover`, `coverage`, `audit`, `clear` and `verify-references`. `database` is a file path or `:memory:`, `currentSchema` is supported, and `options` are DuckDB's own settings. A chunk loads as an Arrow table in one statement: 50,000 rows row by row took 13 seconds, and take a tenth of a second. It runs in every suite that runs across all the databases, by default, since it needs no server. See [DuckDB](docs/configuration.md#duckdb).
- **`maxConcurrentJobs` on a connection** caps how many jobs use it at once, however many `workers` the run has; a job held back waits while jobs on other connections start. DuckDB is always 1: it lets one process at a time open a file, and every job is a process of its own. Two aliases for one DuckDB file are refused, and a job finding the file held by something outside the run waits up to a minute, then fails saying why. Run state can't be kept in DuckDB, since the run holds it open throughout; history and manifests can.
- DuckDB can't swap a table in a foreign key or one with an index -- it refuses to rename either, and renaming a referencing one corrupts its catalog -- so such a swap fails before renaming anything, saying to use `upsert`. Each DuckDB session runs in UTC, since DuckDB otherwise converts through the machine's own zone when a time-zone-aware value meets a column without one. `clear` empties DuckDB tables one transaction per table, since DuckDB checks a foreign key against what is committed.
- `discover` warns, when masking DuckDB in place, that a table in a foreign key can't be swapped.

### Fixed
- **Lists and dictionaries load into SQLite, MySQL, MariaDB, Oracle and SQL Server as JSON text**, which is what `schema` maps them to. A PostgreSQL array or JSON document, and DuckDB's LIST, STRUCT and MAP, failed the copy at its first chunk: none of those drivers can bind one. PostgreSQL writes a list into a `json` or `jsonb` column as JSON rather than as an array, which that column refused.
- **A UUID loads into MySQL and MariaDB, and a time of day into Oracle,** as text; their drivers refused both, failing a PostgreSQL UUID or TIME column copied there.
- **What each Python type is sent as, for each database, is one table** (`bauta/database/values.py`), tested directly, rather than spread over each dialect. Two things change with it. bauta no longer registers adapters with `sqlite3` for the whole process, which changed how any other code in it wrote dates and Decimals to SQLite. And a watermark is bound as a loaded value is, so a `datetime` watermark keeps its microseconds on SQL Server, where pymssql rounded it to the millisecond.
- **`schema` maps each of MySQL's and MariaDB's unsigned integers to a type holding its whole range**: SMALLINT UNSIGNED to INTEGER, INT UNSIGNED to BIGINT, BIGINT UNSIGNED to a 20-digit decimal. Mapped to the signed type of the same size, the upper half of each was refused as it loaded, and a BIGINT UNSIGNED past 2\*\*63 copied into SQLite silently became a float. Found by a new copy test between every pair of databases over each one's UUID, time, JSON, binary, instant and unsigned types.
- **An integer past 64 bits loads into SQLite** as its exact text. A MySQL unsigned BIGINT above 2\*\*63 raised OverflowError.


## 0.1.8 — 2026-09-21

The native masker covers `number`, masks `fpe` two and a half times as fast and `key` a quarter faster on a whole job, and scales integer columns across threads as well as text ones. `synthesize` no longer holds every generated key in memory, and the docs' throughput figures come from a benchmark in the repository. No mask changes.

### Changed
- **The native masker covers `number`**, four to six times as fast on integer, float and `Decimal` columns alike (a 5,000-row chunk of Decimals: 22 ms to 5; of integers, 18 ms to 3), and spread over `maskingThreads` like the other native strategies. The masks are unchanged: Rust repeats Python's decimal arithmetic at 60 digits, recorded vectors pin every result, and a value it would have to guess at -- a mask that comes out as zero, whose sign Python keeps -- is masked by Python instead.
- **Masked columns are spliced back into rows by rebuilding them from columns** where the table is narrow or a third or more of it is masked, which is faster there; a wide table with a few masked columns keeps the row-by-row splice. Each column is masked as the transpose yields it, which also serves a table masked in every column. About 7% off a typical masked chunk, and 10-28% off splicing a table only partly masked.
- **The native `fpe` masks a value about four times as fast** (4.7 µs to 1.1 for a ten-digit integer): FF1's rounds run in machine integers wherever each half fits 64 bits -- 19 decimal digits a half, so every identifier in practice -- instead of building and taking apart big integers every round. A million rows with two `fpe` columns among six went from 105,000 rows a second to 254,000 on one thread, which makes `fpe` faster than `key` natively. Longer values take the big-integer path as before.
- **The native `key` is 20-30% faster** on the same grounds: its Feistel network runs in machine integers for any domain up to 2\*\*128. Five `key` columns and a `hash`, a million rows: 58,000 rows a second to 74,000.
- **Integers cross into and out of the native masker through the C API** wherever they fit 64 bits. The stable ABI the extension is built against converted every integer by calling `int.to_bytes` and `int.from_bytes`, a Python call per value made while every masking thread waited: an integer `key` column at eight threads ran at half a text column's throughput, and now keeps pace with it.
- **`synthesize` remembers generated keys only for a table keyed wholly by foreign keys**, the only kind that can repeat one. It held every row's key for the whole run whatever the table: 116 MiB a million rows of a table keyed by one integer.
- **`benchmarks/masking.py` measures the throughput figures the docs quote**, from a million generated rows copied SQLite to SQLite under each policy and each masker, and fails if any two copies of a policy differ. The figures in the docs are all re-measured with it, on a stated dataset. See [benchmarks](docs/development.md#benchmarks).
- All the above leave every mask as it was: the machine-integer paths are tested against the big-integer ones on every width they take, and the recorded vectors pass on both sides of each boundary.

### Fixed
- **A `number` value past its 60 significant digits now fails as a masking error naming its column** -- an integer of more than 60 digits, or a float held to more `decimals` than 60 digits reach at its size. It raised Python's bare `decimal.InvalidOperation`, which said neither which column nor why. No mask changes.


## 0.1.7 — 2026-09-21

A watermark could leak an unmasked value, the package is reorganized into groups a newcomer can find their way around, run-state connections are closed, and loads spend less time in Python. No mask changes.

### Breaking
- **A `watermarkColumn` that `defaultStrategy` masks is refused by `validate`.** Only a column the policy named was checked, so a watermark column left to a masking `defaultStrategy` passed, and every run wrote its raw, unmasked value into run state and logged it. `audit --connect` was the only thing that noticed. Watermark on a column masked with `keep`, or name the column in the policy with `keep`. `run` also refuses it before reading a row.
- **Modules moved into subpackages, and the old import paths are gone.** Every module is now in a package named for what it does: `configuration`, `database` (with `dialects`), `jobs` (`runner`, `pipeline`, `workers`, `keys`, `dependencyGraph`, `memory`, `reporting`), `log` (with `scrubbing`), `transform`, `masking`, `generate`, `review` and `cli`. `bauta.databaseDialects` is now `bauta.database.dialects`, split into one module per database. `bauta.builtinMasking`, `bauta.fakeData` and `bauta.fpe` are now `bauta.masking.strategies`, `.fakeData` and `.fpe`, beside `bauta.masking.core`. `discovery`, `builtinDiscovery`, `subset`, `schema` and `synthesize` moved under `bauta.generate`, and `audit`, `coverage` and `references` under `bauta.review`. `bauta.masking.Strategy`, which custom strategies subclass, is unchanged, as are the `bauta` command and every configuration setting.
- **`bauta` exports less.** The dialect classes and `ColumnCategory` are imported from `bauta.database.dialects`, and `BaseJobConfig`, `Transform` and `resolveTransformer` are no longer exported at all. What `bauta` exports is now exactly what [library.md](docs/library.md#api-stability) documents, and anything reached only through a submodule is internal.
- **A `MemoryBackend` implements all six methods.** Watermarks and key fingerprints had defaults so that backends written before them kept working, and the fingerprint default turned the masking-key-change check off without a word. A backend that leaves any method out now can't be created.
- **`bauta.transform.builtinTransforms`** is the module path of the built-in transformers in `sourceQueryColumnTransforms`, in place of `bauta.builtinTransforms`: `bauta.transform.builtinTransforms:truncate(50)`.
- **A native masker that refuses a strategy's options raises**, where it used to fall back to masking in Python without a word. The two are always the same version, so a refusal is a bug to report, not an older extension to work around.
- **Run state recorded before the masking implementation was** reads as an implementation change, which logs a warning once on the next run of each masked upsert job; nothing is refused.
- **`bauta.masking` no longer finds names lazily.** The shim that let the strategies and fake-data lists be reached through the old `bauta.masking` module is gone; `bauta.masking` exports them itself.

### Changed
- **PostgreSQL loads COPY through psycopg's own encoders**, twice as fast as the Python encoder they replace against a real server, with identical rows stored. Chunks holding an array, a JSON object or an interval still go statement by statement.
- **Each chunk is checked for the types it holds by its distinct types, not value by value**: three to five times faster for every dialect's check, and SQL Server converts only the columns that need it.
- **A PostgreSQL upsert creates its staging table once per connection**, not before every chunk: a round trip a chunk saved, 14% of a chunk's time at 10 ms of latency.
- **`run --dry-run` and `audit --connect` open one connection per database alias**, where they opened one per check, three or four a job, each running `passwordCommand` again.
- **Whether a column is masked is decided by its strategy's `PASSTHROUGH`, not its name.** A custom strategy that returns values unchanged is now treated like `keep` by the watermark and primary-key checks. `canonical`, the bytes the built-in strategies key a value on, is public in `bauta.masking` for custom strategies.

### Fixed
- **Run-state connections were never closed.** `DatabaseMemory` holds one connection per process and nothing closed it: each job process dropped its connection when it exited rather than closing it, and `bauta run`, `jobs` and `clear` held theirs until exit. A `MemoryBackend` now has `close()` and works as a context manager; the commands close theirs, and each job's process closes its copy once its outcome is sent.
- **A connection whose session setup failed was left open**, such as a PostgreSQL `SET search_path` or an Oracle `ALTER SESSION` naming a schema that doesn't exist, and each retry opened another. It is now closed before the error is raised.
- **SIGTERM under `--forever` waited out `cycleSleepSeconds` before stopping**, since Python resumes a sleep after a signal's handler returns; a long pause outlived a container's grace period. It now stops within a second.
- **A `swap` on SQL Server failed for a table whose name contains a single quote**: `sp_rename` took the name as a string literal the quote ended.
- **`run --dry-run` skipped a job's checks when another job was named like a database alias** whose check had failed.

### Added
- [SECURITY.md](SECURITY.md), for reporting a vulnerability privately, and [CONTRIBUTING.md](CONTRIBUTING.md).
- `ruff check` runs in CI, for errors rather than style.
- The test suite fails on a connection or file left open, the package's or a test's.
- The tests mirror the package: the tests for `bauta/jobs/` are in `tests/jobs/`, and so on, with the helpers each needs beside them. See [where the tests are](docs/development.md#where-the-tests-are).
- The operations every database must perform are one suite run against all six, where they were copied into a file per database and had drifted: MariaDB lacked four of them, including run state in a table, and SQLite several.
- CI measures coverage across the unit and integration suites, job processes included, and fails below 95%.
- GitHub Actions are pinned to commits, and Dependabot keeps them, the dev tools and the Rust crates current.


## 0.1.6 — 2026-09-20

Masking and provability: a misspelled setting can no longer produce an unmasked
copy, `coverage` answers what the jobs don't cover at all, and masking itself is
faster without any mask changing.

### Breaking
- **An unknown field in `jobs.yaml` or `database.yaml` is now an error.** It used to be ignored, so a misspelled key was silently dropped — and `maskng:` instead of `masking:` produced a job that looked masked in the file and copied every column as it stood, with `validate`, `run --dry-run` and `audit --connect --strict` all reporting success. Fix any misspelled setting a run was quietly ignoring; `bauta validate` names each one, offline. A top-level key beginning with `x-` is still allowed, for YAML anchors.
- **`audit` reports every unmasked job.** It used to say nothing unless another job masked the same source, so the first table copied from a new source, and any single-job configuration, went unreported. An unmasked job is now a warning, an **error** when its columns look like personal data, and `--strict` exits 1 on either. A job that copies as it stands declares [`unmasked: true`](docs/configuration.md#copying-without-masking) and is recorded rather than flagged.
- **`--accept-key-change` is refused for an upsert job whose policy masks the target's primary key.** An upsert matches rows on the primary key, so a new masking key gave those rows new keys: the run inserted a second generation of rows beside the first instead of updating it, and where a new key landed on an existing one it overwrote a different row. `verify-references` reported the result clean, because every foreign key still pointed at some row. Empty those targets with `bauta clear` and run again; see [rotating the masking key](docs/operations.md#rotating-the-masking-key).
- **`BoundMasking.apply` refuses rows whose width doesn't match the policy it was bound to.** Too-wide rows used to have their extra columns ignored in silence, which is at odds with every column having to be covered. No job the runner builds can reach it, since `bind()` settles the width; a library caller applying a plan to other rows now hears about it.

### Added
- **`bauta coverage`** lists every table in a source database and what the jobs do with each: copied and masked, copied as it stands, not copied and declared, or **NOT COVERED**. It exits 1 on anything uncovered, so it can follow `run` in CI and fail when production grows a table the copy doesn't account for. `audit` checks the jobs that exist and cannot see a table nobody wrote a job for. See [coverage](docs/masking.md#coverage-what-the-jobs-do-not-cover).
- **`acknowledged` in `jobs.yaml`** records the tables no job copies and why, so leaving one out is a decision on the page rather than an omission. `coverage` also reports a declaration for a table the database no longer has. See [acknowledged](docs/configuration.md#acknowledged).
- **`requireMasking` on a `database.yaml` alias** refuses any job that reads from or writes to it without a masking policy, at `validate`, before anything connects. The line a reviewer signs: this copy can only ever hold masked data. Nothing overrides it, including a job's own `unmasked`. See [requiring masking](docs/configuration.md#requiring-masking).
- **`unmasked: true` on a job** says it was reviewed and copies its rows as they stand, as `keep` says it of a single column.
- **`defaults` in `jobs.yaml`** supplies what every job would otherwise repeat: `sourceDatabase`, `targetDatabase`, `insertStrategy`, `chunkSize`, `active`, `refresh`, `retries`, `retryDelaySeconds`, `timeoutSeconds` and `masking.key`. A masking policy's `columns` stays with its job, and a job with no `masking` block does not grow one. See [defaults](docs/configuration.md#defaults).
- **`discover --all-tables`** proposes a policy for every table in a database, with `--schema NAME` for another schema, instead of naming each with a repeated `--table`.
- **`discover --mask-keys` and `subset --mask --mask-keys`** propose `key` for numeric surrogate keys too, in the domain each foreign key already shares, instead of `keep`. Both ends move together, so the copy's references still match, and a generated policy needs no hand-editing to mask its ids. See [proposing a policy](docs/masking.md#proposing-a-policy-discover).
- `Database.listTables()` lists a database's base tables on all six databases, excluding views and anything the server ships.
- Three runbooks that were missing: [rotating the masking key](docs/operations.md#rotating-the-masking-key), [after a failed cycle](docs/operations.md#after-a-failed-cycle) — which documents `refresh` as the resume mechanism — and [one production, several environments](docs/operations.md#one-production-several-environments), which already worked and was written down nowhere.

### Changed
- **`active`, `chunkSize` and `workers` have defaults** (`true`, `5000` and `1`), so a job says only what is particular to it. `insertStrategy` stays required: it is the one setting where a wrong value gives wrong data rather than an error.
- **`run --dry-run` compares the column counts it already printed.** A target that had gained or lost a column, against a query that hadn't, passed the dry run and failed the next real run — at whatever hour it was scheduled for.
- **A connection error names what it tried to reach**, resolved: `cannot connect to sqlite file /srv/copy.db`, rather than a driver message naming neither the path nor the directory it resolved against. Never the password.
- `audit --connect` resolves every job's columns, not only a masked job's, so an unmasked job's can be checked for personal data.
- The starter configuration in `example/starter/` shows `defaults:`, and drops the empty keys and Oracle-only fields it carried on every job and connection.

### Performance
Masks are unchanged by all of this: the reference vectors pass unaltered, in pure Python and with the native masker.

- **A job with no transforms no longer copies every chunk twice.** `sourceQueryColumnTransforms` is unset on most jobs, and each chunk was still converted to lists and back to tuples to change nothing: 3.5 ms per 10,000-row chunk, on every job in the product. A job with one transform now walks one column rather than all of them.
- **A masking policy reads and rewrites only the columns it masks.** A realistic policy keeps far more columns than it masks, and all of them were extracted, copied and reassembled. Per 10,000-row × 30-column chunk: 5 masked columns 29.9 → 19.2 ms (−36%), 1 masked column 18.3 → 6.1 ms (−66%), all 30 masked 106.3 → 96.3 ms (−9%).
- **`dateShift` derives each day's shift once** rather than once per row: 32.0 → 4.3 ms per 10,000 rows spread over two years (−87%). The shift was already per day; only the work is new.
- **The per-value scans each dialect made before a load are now one walk**: a clean 10,000-row chunk into SQL Server 35.5 → 19.7 ms (−45%).
- **PostgreSQL's bulk load spells each value through an exact-type lookup** instead of a chain of `isinstance` checks, which every row into a PostgreSQL target crossed: 100.7 → 77.9 ms per 10,000-row × 30-column chunk (−23%). A subclass of a handled type still takes the chain, so nothing is spelled differently.
- **Run state in a table reuses one connection per process** instead of opening one per read and write, which also ran `passwordCommand` again each time for cloud IAM tokens.
- **`bauta history` reads backwards from the end of the file** instead of parsing all of it to show the last 20 records.

### Fixed
- **Run state, history and manifest tables whose names need quoting** were written correctly and read back unquoted, so a table called `order` worked until something read it.
- `bauta/masking.py` and `bauta/builtinMasking.py` no longer import each other: a strategy declares `PASSTHROUGH` rather than the core naming a built-in strategy to recognise one.

### Removed
- `proposeTable`'s `primaryKeys` parameter, which nothing ever passed. The primary keys it would have supplied are read through the database's own cache.


## 0.1.5 — 2026-09-20

### Breaking
- **`charset: hex` is case-sensitive, so its masks change.** It used to mask case-insensitively, so `aB12cd34` and `ab12cd34` got one mask and two rows of a key could merge silently. `key` now keeps each character's case (digits to digits, `a-f` to `a-f`, `A-F` to `A-F`); `fpe` masks both cases within one alphabet, as FF1 needs a single one, so a masked value may mix cases. Re-mask any copy whose `hex` columns must match one made with an earlier version.
- **Table names are quoted into the statements a run builds**, as column names already were, so `targetTableFinal` and `targetTableStage` are read as names rather than passed through as SQL. A name written plainly still means the table it meant unquoted. A value that was really a fragment of SQL — an Oracle database link (`orders@remote`), say — no longer works and has to become a view or a table the job names directly. See [how names are written](docs/design.md#how-names-are-written).
- **`dateShift` is keyed on the day, so timestamp masks change.** It used to key on the whole value, so two timestamps hours apart moved by different numbers of days: a day's rows scattered across the month, and a `DATE` column and a `TIMESTAMP` column holding the same day disagreed about where that day went. Every value on a day now moves to one day, which is what the strategy is documented to do. Dates and ISO date text mask as before; re-mask any copy whose shifted timestamps must match one made with an earlier version.
- **SQLite connections enforce declared foreign keys**, as every other database does; SQLite ignores them unless each connection asks. A load into a SQLite target whose rows break a declared key now fails instead of loading them, and so does emptying a table other rows still reference. For a job that has to load such rows, put `PRAGMA foreign_keys=OFF` in its `preTargetAdhocQueries`.

### Added
- **`bauta verify-references`** counts, for each foreign key on a table the jobs load, the rows whose key points at nothing: the keys the target declares, and those of the sources copied into it. Counts only, never values; exits 1 on any orphan, so it can follow `run` in CI. See [checking the copy's references](docs/masking.md#verify-references-checking-the-copys-references).
- `bauta audit --connect` warns when a job copies only part of a table that another job's table references (it's incremental, or its query has a `WHERE`), and the referencing job isn't limited to match, so the copy can reference rows it lacks. See [tables that reference each other](docs/design.md#tables-that-reference-each-other).
- `bauta audit --connect` warns when a job's table references another job's table, and the job can load before the other: it doesn't wait for it through `predecessors`, the other is inactive, or a job on the way has a longer `refresh`.
- `bauta audit --connect` reports an error for a `swap` job whose target is referenced by a foreign key the target database declares. The key stays on the old table, now the stage, so it stops checking the target and the next run can't empty the stage. See [how a swap works](docs/design.md#how-a-swap-works).
- `bauta audit --connect` warns when a `swap` job replaces a table that declares foreign keys of its own, since its stage table has none and the copy stops enforcing them after a run.
- `bauta audit --connect` warns when a foreign key and the key it references are both masked with `shuffle`, which moves values between rows rather than mapping them, so the references point at other rows.
- `bauta audit --connect`'s partial-parent check also recognises `LIMIT`, `TOP`, `FETCH FIRST` and a query that joins another table, not only `WHERE` and watermarks.

### Changed
- `schema` maps `TIME` into Oracle as `VARCHAR2(32 CHAR)` rather than `VARCHAR2(16 CHAR)`, which couldn't hold the day-long values MySQL's driver returns (`-34 days, 1:00:01`).
- The cycle message from `schema`, `synthesize` and `clear` no longer offers `--no-foreign-keys`, which only `schema` has: it now says to take those tables one at a time, or leave one of the keys out of the set.

### Fixed
- **A table whose name has to be written in quotes was unusable.** A reserved word (`group`, `order`), a name whose case the database folds, a name with a space: every catalog lookup bound the quotes along with the name, so the table appeared to have no columns and no primary key, and `run` refused an upsert into it with "has no primary key". Names are now unquoted before a lookup binds them and quoted into every statement bauta builds, on all six databases. `audit` and `verify-references` match such a job to the catalog's keys, `schema` quotes the tables it creates as it already quoted their columns, and `subset` and `discover` quote the names they write into what they generate. A name written plainly still means what it means unquoted, so `orders` finds Oracle's `ORDERS`. See [how names are written](docs/design.md#how-names-are-written).
- **A decimal lost its digits in a SQLite copy.** `schema` created `DECIMAL(38,10)`, which gives SQLite's numeric affinity, so the exact text was converted to an integer or a float as it was stored: `123456789012345678.1234567890` came back as `123456789012345680`, and `0.1` as the nearest double, both silently. A decimal is now created as `TEXT`, which keeps every digit, and the conversion is noted. Tables created by earlier versions keep the type they have; recreate them to hold exact values.
- **A MySQL `TIME` was corrupted or refused everywhere it was copied to.** Its driver returns a duration, which nothing else accepts: SQL Server's and SQLite's drivers refused it outright, Oracle stored Python's own `-35 days, 1:00:01`, and PostgreSQL squeezed `-838:59:59` into a `TIME` column as `01:00:01` without a word. Durations are now written as `[-]HH:MM:SS[.ffffff]`, which every database parses back, and a value no `TIME` column can hold is refused as it loads.
- **MySQL's `INT UNSIGNED` was indistinguishable from `INT`**, so `schema` created a column that holds half its range and said nothing; a value above 2147483647 was found only when it failed to load. MySQL and MariaDB columns are now read as their full declared type, so both `INT UNSIGNED` and `BIGINT UNSIGNED` are noted on the table `schema` creates.
- **Two tables that would share a generated job name silently became one job.** `orders` and `Orders`, or the same table name in two schemas, both rendered as `maskOrders`, and the second replaced the first as a duplicate key in the generated YAML. Names are now made distinct, and a schema-qualified name no longer puts a dot in a job name.
- **A `sourceQuery` returning fewer columns than the target has failed with a driver message that named nothing** -- "the current statement uses 5, and there are 3 supplied". The job now says how many columns the query returns, how many the target has and what they are, and points at `targetColumns`.
- **An upsert into a target without a primary key wrote first and refused afterwards**, so its `preTargetAdhocQueries` had already run against the live target and its stage table held every row. The target is now checked before anything is written.
- **A `json` or `jsonb` column couldn't be copied on PostgreSQL.** psycopg reads one as a dict and then refuses to write one back ("cannot adapt type 'dict'"), so any job whose query returned such a column failed on its first chunk. Dictionaries are now written back as JSON. A `jsonb` column holding a top-level JSON *array* still has to be selected as text, since a list is what psycopg writes to a `text[]` column and the two can't be told apart.
- **A load that a masked value overflowed blamed the column.** `key` keeps an integer's digit count, so a 10-digit value can leave an `INT` column's range, and `number` varies a value that may already be at its column's limit; the driver's message named only the column. Such a failure now says which mask is likely responsible and what to do about it, and [masking.md](docs/masking.md#key) documents both limits.
- **A swap on Oracle could lose the stage table for good.** Oracle commits each DDL statement, so a rename blocked by another session (`ORA-00054`) left the stage table under the temporary name: it was gone, and every run after that failed with `ORA-00942` until someone renamed it back by hand. A failed rename now undoes the ones that went through.
- **A swap on SQLite repointed every view on the target at the stage table.** SQLite rewrites the views that name a renamed table, to follow it, so a view over the copy started reading the old rows -- and the next run emptied them. The renames now leave views naming the target.
- **A `postTargetAdhocQuery` that failed after a swap reported the job as having moved 0 rows**, though the swap had already replaced the target with the new rows. The failure now says the load finished, names the target and reports the rows it holds.
- **A run killed outright left its jobs running.** `kill -9`, an out-of-memory kill or a scheduler that doesn't wait runs none of the run's cleanup, so each job carried on loading rows and writing run state — while the run lock, held by the process that died, was already free, so the next `bauta run` started alongside it and the two loaded over each other. A job now ends the moment the run that started it does. See [workers](docs/design.md#workers).
- **A name longer than the target keeps was loaded into whatever table shared its first 63 characters.** PostgreSQL cuts a name to 63 bytes without saying so, and the other servers to their own limits, so two jobs whose targets differed only past that truncated and loaded the same table, and the second job's swap renamed over the first's rows although both jobs reported failure. Such a name is now refused before anything is written, and `schema` notes it on the table it would create.
- **A swap of such a table built a temporary name its own parser refused**: the `_tmp` went outside the quotes, so SQL Server got `sp_rename '[group_stage]', '[group]_tmp'` and failed after the stage table had been loaded. The suffix now goes inside them, and sp_rename is given the new name bare, since brackets in it would become part of the name.
- **`schema` copied a foreign key's constraint name verbatim**, so `--apply` failed partway, leaving half a schema, wherever two keys shared a name — legal on PostgreSQL and SQLite, which name constraints per table, but not on MySQL, MariaDB, Oracle or SQL Server. Names are now made unique across one invocation's statements, including where cutting to the length limit made two of them equal.
- **`schema` emitted foreign keys it couldn't create**: a key to a unique column that isn't the primary key — the shape `subset --root` produces — was refused by every dialect, since `schema` never copied the `UNIQUE` constraint behind it. It now copies the unique constraints its keys need.
- **`schema` named a table after the first foreign key that matched it**, mixing Oracle's upper-case catalog names with the names as typed in one script, so jobs written against the lower-case names found only half the tables. Every table is created under the name it was asked for.
- **`schema` was silent about several lossy conversions** it is documented to note: SQLite's 64-bit `INTEGER` into a narrower integer, MySQL's `BIGINT UNSIGNED`, a decimal declaring no precision, a decimal clamped to the target's limits, and a time-zone-aware timestamp into Oracle, which keeps the loading session's offset rather than the source's.
- **`synthesize` repeated itself**: every generator was indexed from row zero, so a second run regenerated the first run's values and a `UNIQUE` column refused them. Generators now continue from the rows already in the table, and generated text ends in the row's number so it stays distinct once cut to a column's width. A row the database still refuses is reported as a `SynthesisError` naming the table and the rows already inserted, rather than a raw driver traceback.
- **`digits` copied a value through unmasked when `keepLeading` and `keepTrailing` covered every digit of it** — `555-0100` under the docs' own card-number options — while the manifest said it was masked. Such a value now fails the job.
- **A masked column could be the watermark**, which is read before masking and then written to run state and logs and shown by `bauta jobs`, leaking the value the job exists to hide. `validate` now refuses it, and `audit --connect` reports a column that falls to a masking `defaultStrategy`.
- **A foreign key pointing into another schema was reported without it**, so every check matched a different, same-named table in the connection's own schema and `subset` read the wrong parent. MySQL, MariaDB, PostgreSQL, Oracle and SQL Server now name such a parent `schema.table`.
- **On SQL Server, foreign keys of tables outside the login's default schema were invisible**, so `subset` silently dropped the parents of a table named `schema.table`, as configuration.md tells SQL Server users to do. Keys of every schema are listed, qualified when they aren't in the default one.
- **Oracle dropped the fraction of a second from every timestamp written**, since the driver binds a datetime as `DATE`. `2026-01-01 10:00:07.123456` became `10:00:07` even in a `TIMESTAMP(6)` column.
- **Oracle returned every `NUMBER` with a scale as a float**, losing digits (`123456789012345.6789` arrived as `123456789012345.67`) and turning large values into ones no target could store — an Oracle-to-Oracle copy of a `NUMBER(38,10)` failed with ORA-01438. They are now `Decimal`; a scale of 0 stays an `int`, and `BINARY_FLOAT`/`BINARY_DOUBLE` stay floats.
- **SQL Server truncated timestamps to milliseconds**, since pymssql renders a bound datetime that way. Times are now sent as text, which SQL Server converts exactly.
- **SQL Server rewrote whole columns of a chunk.** One `VALUES` list takes one type per column, so a number beside `'00001'` stored it as `'1'`, and a decimal spelled `1E-10` typed its column float and rounded the exact values beside it. Such a chunk now loads row by row, where each value is converted on its own.
- **A `Decimal` watermark was written to a run-state file as a float**, which rounds: `12345678901234567.1` came back as `1.2345678901234568e+16`, above the highest row read, so the rows in between were never extracted again. It is now written exactly (`!decimal '12345678901234567.1'`); floats written by earlier versions still read.
- **Masking in place left the unmasked original in the stage table**, where the swap had moved it, beside the masked copy. A masked job now empties its stage table after the swap.
- **On Oracle, foreign keys were read from the login's own schema rather than `currentSchema`**, so `subset`, `schema`, `clear`, `synthesize`, `audit --connect` and `verify-references` all worked from the wrong keys — usually none — wherever `currentSchema` named another schema.


## 0.1.4 — 2026-09-19

### Added
- `bauta --version` prints the version, and which masker a run would use: `bauta-rs` and its version, or Python and why.

### Changed
- The built-in masking strategies moved from `bauta/masking.py` to `bauta/builtinMasking.py`, and the lists the `fake*` strategies pick from to `bauta/fakeData.py`. Old import paths such as `bauta.masking.STRATEGIES` and `bauta.masking.LOCALES` still work.
- `bauta audit` no longer offers `--memory`, `--memory-database` or `--memory-table`, which it never read.


## 0.1.3 — 2026-09-18

### Added
- **Masking on several cores.** `jobs.yaml`'s `maskingThreads` sets how many threads the native masker spreads each chunk over: `1` by default, a number up to the cores available, or `auto` to divide the cores between the jobs running as each starts. `BAUTA_MASKING_THREADS` overrides it. Results are identical for any count. See [masking threads](docs/masking.md#masking-threads).
- The `fake*` strategies are masked by the native masker too, from the lists Python hands it.
- The native masker remembers masks across chunks for `key`, `fpe` and `fake*`, and allocates through mimalloc.
- The native-masking demo compares Python and Rust on a narrow table, and one core against all of them on a 25-column one.

### Changed
- A 25-column masked table runs at about 73,000 rows a second on ten cores, from about 18,000 in 0.1.2. See [speed](docs/masking.md#speed) for the measurements and their conditions.
- The documentation was made consistent throughout, and every speed figure re-measured.


## 0.1.2 — 2026-09-18

### Breaking
- **`bauta init` is removed.** Copy the starter configuration from `example/starter/configuration/` instead.
- **Prometheus metrics are removed:** `--metrics`, `--metrics-push`, `writeMetricsFile` and `pushMetrics`. Take the flags out of cron entries and scripts. Run history in a table covers alerting on jobs that stop completing; see [run history](docs/operations.md#run-history).
- **PostgreSQL uses psycopg 3** (`psycopg[binary]` 3.2.10 or newer) in place of psycopg2. `bauta[postgresql]` now installs without a compiler. psycopg2 is no longer used, so a leftover install can be removed. Connection `options` are still libpq parameters.

### Added
- `memory`, `history` and `manifest` settings in `jobs.yaml`, each a file relative to it or a table in one of `database.yaml`'s aliases. Flags still override them, and `--history-database`, `--manifest-database` and the `--*-table` flags are new.
- Masking manifests can be kept in a table, and `bauta verify-manifest` reads the latest, or `--run RUN_ID` an earlier one.
- `discovery.yaml`: rules of your own for recognising personal data, used by `discover`, `subset --mask`, `audit` and `synthesize` ahead of the built-in rules, which it can leave out by name. `--rules FILE` names one elsewhere.

### Changed
- The built-in discovery rules moved to `bauta/builtinDiscovery.py`.


## 0.1.1 — 2026-09-18

### Added
- **`bauta-rs`, the native masker, on PyPI.** `pip install "bauta[native]"` installs the version that matches; wheels for Linux (x86-64 and ARM) and macOS (Apple silicon and Intel).
- `bauta` uses `bauta-rs` only at its own version, and masks in Python, with a warning, alongside any other.
- `bauta init`, which wrote a starter configuration. Removed again in 0.1.2.


## 0.1.0 — 2026-09-18

The first release on PyPI, as `bauta`: masking, discovery, subsets, synthetic data, `audit`, sealed manifests, incremental loads and a dependency graph, across Oracle, SQL Server, PostgreSQL, MySQL, MariaDB and SQLite.

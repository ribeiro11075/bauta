# Changelog

What changed in each release of `bauta` and `bauta-rs`, which are always released together at the same version. **Breaking** lists what can stop an existing setup from working after an upgrade; read it before upgrading.

Masks never change between releases unless an entry here says so: the same value, key and domain give the same mask in every version so far.


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

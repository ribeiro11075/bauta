# Changelog

What changed in each release of `bauta` and `bauta-rs`, which are always released together at the same version. **Breaking** lists what can stop an existing setup working after an upgrade; read it first.

Masks never change between releases unless an entry says so: the same value, key and domain give the same mask in every version so far.


## Unreleased

Studio signs people in through your identity provider (OpenID Connect single sign-on, for Okta, Entra ID, Google Workspace, Auth0, Keycloak and the rest), and can ask for a code from an authenticator app after a password. See [single sign-on](docs/guides/studio.md#single-sign-on) and [two-factor sign-in](docs/guides/studio.md#two-factor-sign-in).

### Breaking
- **A Studio database from an earlier release must be made again**: its tables were changed in place, not by a migration. Stop Studio, move the database aside (`studio.db` in the data directory, or the one `BAUTA_STUDIO_DATABASE_URL` names), start it, and add the accounts again.
- **`/api/auth/me`'s refusal and `/api/users` say more**: how the sign-in page offers to sign in, and how each account signs in.
- **A right password may not sign in yet**: `/api/auth/login` answers `{"twoFactorStep": ...}` for an account that needs a code next, or must set an app up first.

### Added
- **Single sign-on for Studio**, by the authorization code flow with PKCE. The ID token is checked against the provider's published keys, by asymmetric algorithms only. Once linked, a person is matched by the provider's issuer and subject, never by email; before that, only by an email the provider vouches for, so a provider letting people set their own email can't hand over an account.
  - **Accounts** are made by an admin, or at first sign-in for the email domains you allow.
  - **Roles** are set on the Users page, or from the person's groups at the provider on every sign-in (`BAUTA_STUDIO_OIDC_ROLES=provider`).
  - **Passwords** stay for everyone, for admins only (a break-glass account for when the provider is down), or are turned off (`BAUTA_STUDIO_PASSWORD_LOGIN`). Linking removes a password the policy wouldn't allow; a break-glass admin is never linked; the provider's groups can't demote the last admin.
  - **Sessions** through the provider last 8 hours by default, against a password's 12, so someone disabled there loses access sooner.
  - **Admins** can add an account with no password, and unlink one whose identity at the provider was remade. `bauta studio create-user --sso-only` does the same.
  - **The callback's authorization code** is kept out of the access log.
  - **Google Workspace** sign-ins can be limited to your workspaces (`BAUTA_STUDIO_OIDC_HOSTED_DOMAINS`), and Entra ID's `xms_edov` vouches for an email.
- **Two-factor sign-in for passwords**, with any authenticator app (TOTP), set up from a QR code on a new Account page, with ten one-time recovery codes. `BAUTA_STUDIO_REQUIRE_2FA` requires it of admins (the default once single sign-on is on) or of everyone. Secrets are encrypted with a key in `secret.key`, or where `BAUTA_STUDIO_SECRET_KEY_FILE` says; each code is taken once, and wrong ones are throttled per account. An admin resets it for someone who lost their app.
- **Changing your own password** on the Account page, given the current one; wrong ones count as failed sign-ins, and your other sessions end. An admin sets someone's password on the Users page.
- **`bauta studio set-password`**, the way back in for a locked-out admin, from the server's shell; `--reset-2fa` also turns off two-factor sign-in.
- **`BAUTA_STUDIO_SESSION_HOURS`** sets how long a password session lasts.
- **A new sign-in ends the session the browser had.**
- The `studio` extra installs httpx and PyJWT for single sign-on, and pyotp, segno and cryptography for two-factor sign-in.

## 0.2.11 — 2026-11-21

The native masker no longer crashes Studio, a `--forever` run's manifest lists what it masked, DuckDB connections are locked down, MySQL upserts drop the deprecated `VALUES()`, and a dozen smaller fixes. No mask changes, though `dateShift` writes some ISO text back closer to how it came.

### Breaking
- **A DuckDB connection's queries reach no file outside its database.** DuckDB's SQL could read and write any file the process can, attach databases and load extensions, so a query in the jobs files, one edited in Studio included, could reach the machine as bauta's account. Set `options: {enable_external_access: true}` where queries are meant to read files.
- **DuckDB must be 1.5.1 or later**: 1.5.0 refused its own write-ahead log once file access was off.
- **A `readOnly` DuckDB connection opens its file read-only**, so DuckDB refuses writes too: a `sourceQuery` such as `DELETE ... RETURNING` emptied the table it read.
- **A relative `${file:...}` is read beside the file that names it**, as every other path in a file is, not from the working directory, where cron and a shell found different files.
- **MySQL must be 8.0.19 or later.** An upsert names its new row `AS bauta_new` instead of reading `VALUES(column)`, deprecated since 8.0.20; an older server is refused as it connects. (MySQL 8.0 left support in April 2026.) MariaDB, which has no row alias, is unchanged.
- **Studio refuses an edit naming a masking strategy from a module the jobs files don't already use**, as it refuses a transform: naming one imports its module, which ran before the check. `BAUTA_STUDIO_TRANSFORM_MODULES` allows a module for both.

### Fixed
- **The native masker crashed the process when a thread other than the main one loaded it first** (Studio's first masked request, or an orchestrator's threaded tasks). It allocates through mimalloc 2 now, at the same speed. Every bauta-rs since 0.1.3 was affected.
- **A `--forever` run's manifest lists what its cycles masked.** It was written once, at the end, from the last (usually idle) cycle, so it listed no job. It is now written as each cycle that ran a job ends, and an idle cycle leaves it as it was.
- **A signal between `--forever` cycles stops the run there**, rather than starting one more cycle that marked every due job skipped.
- **A `--forever` cycle with nothing due logs at debug**, not two lines every `cycleSleepSeconds` all day.
- **Run state in the layout from before watermarks no longer fails every run** on its missing `maskingKeys` section.
- **`verify-references` checks an undeclared key on a table named for a reserved word**, such as `order`, which failed as a syntax error.
- **A manifest kept in a table whose name needs quoting reads back**; its content was read from the name unquoted.
- **`clear` holds the run lock**, so it can't empty a table a run of the same jobs is loading.
- **Scrubbing a driver's message takes time in proportion to it**, not quadratic: 16,000 names took over a minute.
- **bauta reports its own version, from `bauta/_version.py`**, which `pyproject.toml` now reads too. An editable install reported the version it was installed at, in manifests and `bauta --version`, and accepted an older native masker as matching.
- **`dateShift` keeps ISO text as written but for its date**: `.5` came back as `.500000` and `Z` as `+00:00`, and on Python 3.10 neither could be read, failing the job.
- **`--accept-key-change` checks an Iceberg upsert's key** (its `targetKey` or identifier fields) as it does a table's primary key, where it only warned.
- **Studio:**
  - Reuses the loaded configuration until one of its files changes, where every request re-read and re-validated it: a fifth of a second for 500 masked jobs.
  - A job measured, or a database checked, before 200 other checks no longer shows as never measured or checked.
  - An edit spanning several files, as accepting discovery's proposals makes, is written whole or not at all.
  - A run started before a restart no longer shows as running because another process took its id.
  - A finished run no longer lingers as a zombie process until its page is opened.
  - Two admins demoting each other at once can no longer leave no admin.
  - A jobs file is on disk before it replaces the old one, and the data directory, run logs and backups are readable by Studio's account alone.
  - A failed benchmark's message is scrubbed.
  - The web app says what went wrong when a proxy answers in Studio's place.

## 0.2.10 — 2026-11-16

`redact` masks natively, five to fifteen times as fast, and in linear time in Python. No mask changes.

### Changed
- **The native masker covers `redact`**, five to fifteen times as fast on one thread and spread over `maskingThreads`: notes a third of which hold an identifier mask at 0.6 microseconds each, not 8.6. It finds exactly what Python finds, checked by the vectors and by 20,000 generated texts under four policies. Non-ASCII text and a policy's own `patterns` are still masked in Python.

### Fixed
- **`redact` in Python takes time in proportion to a long run of digits or letters**, not quadratic: 50,000 digits took three seconds. The email search now starts only where the text holds an @.

## 0.2.9 — 2026-11-10

Jobs can read tables of files, Studio's failures keep values out of its logs, and its tables upgrade in place. No mask changes.

### Breaking
- **Studio refuses an edit that starts calling a transform from a module the jobs files don't already use** (bauta's own aside): a transform is any callable in any module, so `os:system` could run commands as Studio's account. Allow modules with `BAUTA_STUDIO_TRANSFORM_MODULES`, or add the transform where the file is managed.
- **Studio's sign-in limit counts by email and address together**, with wider limits by each. Behind a proxy on another machine, name it in `--forwarded-allow-ips`, or every sign-in counts as the proxy's.

### Added
- **Files as a source.** A job can name a `files` connection as its `sourceConnection` and query its Parquet, CSV or JSON Lines with SQL, locally or in S3, Google Cloud Storage or Azure, with the same streaming, masking, watermarks and partitions as a database. An overwrite job's table reads as its newest snapshot, a table's columns are those of all its files, and CSV is read as text, so a zip code keeps its leading zeros. The query reaches nothing outside the root. The `files` extra now installs DuckDB.
- **`readOnly` on a files connection**, as on a database.
- **Studio's tables upgrade in place** with Alembic, which the `studio` extra now installs. A database from 0.2.7 is recognized and kept.

### Fixed
- **Studio no longer writes a value a driver quoted into its activity or server log.** A scheduled check's failure was kept raw, and an unhandled failure written out by the server as it stood; both are scrubbed now.
- **Editing in Studio works where `include` reaches outside the configuration directory**: every edit was refused. An edit is now checked in place rather than in a copy of the directory, and two edits at once can't both pass the check that the file is as it was read.
- **Studio's sign-in can't be used to lock an owner out from elsewhere, or to fill its memory**, and checks at most four passwords at once.
- **Two requests archiving the same manifest at once no longer fail one of them.**
- **Studio removes expired sessions as it starts.**
- **Studio's sign-in page names the right command for the first account**, `bauta studio create-user`.
- **A benchmark interrupted by a restart is marked failed**, rather than staying `running` and disabling its job's Measure button for good. A job is measured once at a time, at most two at once.

### Changed
- **A release runs all of CI first**, real databases included, and publishes nothing if it fails. CI builds and tests Studio's web app, and the dependency audit and Dependabot cover it.

## 0.2.7 — 2026-10-17

bauta Studio: a web console a team runs on its own infrastructure, in the same package and licence. Sign in to watch runs, start, rerun and cancel them, prove the copy safe and hand a reviewer its evidence, review new columns, build subsets, and edit jobs, each change validated and recorded. The CLI gains what Studio is built on and is otherwise unchanged: `bauta status`, one run id from a cycle's first log line to its history and manifest, and `bauta.project`. Nothing breaking; no mask changes.

### Added
- **bauta Studio**: `pip install "bauta[studio]"`, then `bauta studio serve`. Viewers, operators and admins sign in to watch runs as they go; start, rerun and cancel them; see the schedule and stale jobs; read each job's trend; check coverage, the audit and every manifest, and download a run's evidence; accept discover's proposals; build a subset; measure a job; compare two runs; and edit the jobs files as text or a form. It runs `bauta run` as cron does, under the same lock, checks coverage, the audit, discovery and references nightly, and never shows a key, a password or a table's value. See [Studio](docs/guides/studio.md).
- **`Project`, a configuration directory as the command reads it**, for a program built on bauta: `Project.load()` reads and validates the configuration and knows where state is kept, and `validationProblems()`, `dryRunProblems()`, `audit()` and `coverage()` return what those commands find, as data. The commands use it themselves. See [a configuration directory](docs/reference/python-api.md#a-configuration-directory).
- **`bauta status`: what's running now**, and where each job stands: waiting, running since when, or completed, failed or skipped with its rows or error. While a cycle runs, `run` keeps a small file beside its lock; one left by a process that's gone shows as `ended without finishing`. From Python, `Project.running()` reads them, and `runDataJobs(onProgress=...)` is told each change as a `RunProgress`. See [what's running now](docs/guides/watch-what-ran.md#whats-running-now).
- **A cycle's run id is made as it starts** (`RunResult.runId`), and is the same in history, a manifest table, the manifest's new `runId`, `status` and the `Starting cycle` log line. After `run --job`, each manifest entry records its own run. See [what it records](docs/guides/keep-a-manifest.md#what-it-records).
- **`status` shows the rows each running job has written so far**, about once a second (`RunProgress.rows`).
- **For a program built on bauta**: `Project.drift()`, `verifyReferences()` and `schedule()`, as `discover --update`, `verify-references` and `jobs` give them; `bauta.generate.subset.draftSubset()`, with row counts by `subsetCountQuery()`; and `bauta.generate.schema.createStageTable()`. See [the Python API](docs/reference/python-api.md).

## 0.2.6 — 2026-10-07

Closes ways a copy could leak, lose rows or lock readers out without an error. MySQL and MariaDB no longer merge a row into another sharing a unique value; a JSON document's names, birth dates and addresses are masked by what their keys say; microdegree coordinates are moved, not left as they were; an append that fails while publishing takes back what it published; an Oracle swap killed between renames is put right; and a swapped table keeps its grants and indexes every run. `requireEncryption` and `readOnly` make the rest failures rather than log lines. `coverage` stops counting a table a query only mentions, and a run of small jobs is about eight times faster. **Read Breaking first**: masks change for some JSON fields, and some coordinates and upserts that loaded before are refused.

### Breaking
- **Masks change inside a JSON document under keys that name personal data**, where a `json` policy sets no `otherwise`. Such a value (under `name`, `dob`, `address`, `phone`, `ssn` and the like) is masked as that kind (`fakeName`, `dateShift`, `key`...) in its key's domain, not by `redact`, which finds only shapes and copied `{"name": "Ann Smith", "dob": "1984-03-02"}` as it stood. A value its key's kind can't mask fails the job, naming its path. Setting `otherwise` keeps the old behaviour exactly. See [`json`](docs/reference/strategies.md#json).
- **`coordinate` refuses a value outside its axis** (beyond 90° for a latitude, 180° for a longitude), naming the column. A column of microdegrees was moved as if it held degrees, by under a millimetre, and an integer column rounded it straight back, so the copy held every location exactly. The new **`scale`** option reads such columns. Values in range mask as before.
- **An upsert into MySQL or MariaDB fails, rather than overwriting another row, when a new row carries a unique value another row already has**, as on the other databases. `ON DUPLICATE KEY UPDATE` fires on any unique key, so the existing row took the new row's values and the new row was never written: two people merged into one silently, as a colliding masked value would. Not retried. See [how a data job moves rows](docs/concepts/how-it-works.md#how-a-data-job-moves-rows).
- **`email`'s `length` is at least 12**, as `hash`'s is: at 8 hex characters, two of 100,000 addresses collided more often than not. 12 is the default, so masks are unchanged. A unique email column wants `length: 24` ([limits](docs/reference/strategies.md#limits)).
- **A watermarked job that shuffles any column is refused where its source or target has `requireMasking`**: an incremental chunk often holds one row, which keeps its own value.
- **`coverage` counts a table as covered only where a job reads rows from it** (after `FROM` or `JOIN`, directly or through a CTE, derived table or `UNION`). A mention in a comment, string or filtering subquery counted before. A table read through a view must still be declared. See [coverage](docs/guides/prove-the-copy-is-safe.md).
- **`discover` proposes the new `ip` strategy for IP addresses**, and `key` with `charset: hex` for MAC addresses, where it proposed `hash` (which an `inet` column refused) and `keep`. Policies already written are unchanged.

### Added
- **The `ip` strategy**: an address as another of its family, one-to-one, by `key`'s permutation, so a mask is always a valid address an `inet` or `cidr` column takes. `keepPrefix` keeps the leading bits. Text, PostgreSQL's address types, bytes and `INET_ATON` integers come back as they came. See [`ip`](docs/reference/strategies.md#ip).
- **`discover` finds IPv6 and MAC addresses** by value, and MAC addresses by name.
- **`statementTimeoutSeconds`** on a server connection, so a runaway query on production can't run for hours. Each database measures it its own way, set out in [statement timeout](docs/reference/connections.md#statement-timeout); a job it stops isn't retried.
- **Partitions slice columns that aren't numbers** (a UUID key, text, a date), by `count` even shares in the database's own order, the bounds bound as parameters and never logged; `partitions: auto` slices a UUID primary key so. See [partitions](docs/concepts/how-it-works.md#partitions).
- **`schema` creates the source's plain indexes** on the tables `--apply` creates, leaving out with a note those it can't recreate and any on DuckDB.
- **`synthesize` keeps to CHECK constraints** that list values or bound a number, and names any other it can't follow. **`synthesize --profile ALIAS`** shapes rows like the same table elsewhere (NULL rates, ranges, a small column's labels) from aggregates alone, never a personal-looking column, a key, or a label fewer than five rows share. See [generate data](docs/guides/generate-data.md).
- **A dependency audit in CI**: `pip-audit` and `cargo audit`, on every change and weekly.
- **`requireEncryption`** on a server connection closes any session the server doesn't report as encrypted, before a statement runs. See [requiring encryption](docs/reference/connections.md#requiring-encryption).
- **`readOnly`** on a database connection refuses every write bauta would make there, and makes PostgreSQL, MySQL, MariaDB and SQLite sessions read-only on the server too. See [read-only connections](docs/reference/connections.md#read-only-connections).
- **`coordinate`'s `scale`**, for degrees stored multiplied (`1000000` for microdegrees, `10000000` for E7), which `discover` proposes where sampled values are too large for degrees, saying to check it.

### Changed
- **Jobs start in milliseconds, not a fifth of a second**: they fork from a forkserver that has imported bauta once. 60 small jobs took 13.2 seconds and 11 of CPU, and take 1.6 and 0.2. Each job gets the run's environment as it stands when it starts. `BAUTA_START_METHOD=spawn` starts each afresh. See [workers](docs/concepts/how-it-works.md#workers).

### Fixed
- **`{{ watermark }}` may appear more than once in a `sourceQuery`**, bound at each: such a query passed `validate` and failed every run. One inside a comment or string is left alone, and a query whose only placeholder is there fails validation.
- **`watermarkColumn` matches the query's column ignoring case**, so Oracle's `UPDATED_AT` matches `updated_at`.
- **A failing `passwordCommand`'s message drops what looks like a token**, and its stdout; its stderr reached the log, history and webhooks whole.
- **A webhook post is retried once, three seconds later**, after a network error, timeout, 5xx or 429.
- **`synthesize` generates dates to the end of the current year**, not 2026-12-31.
- **Offline `audit` says a column renamed in `sourceQuery` passes it**, and to gate CI with `audit --connect --strict`.
- **A swapped table keeps its grants and plain indexes every run**, not every other run: the stage is given the target's table grants and non-unique indexes before loading, on the five server databases. One that can't be given is a warning. See [how a swap works](docs/concepts/how-it-works.md#how-a-swap-works).
- **An Oracle swap killed between renames is put right by the next run**, finished or undone as the tables left say, where every later run failed with `ORA-00942`. A job past `timeoutSeconds` lets its renames finish before it stops.
- **A files `append` failing while it moves its parts takes back what it published**, and one killed meanwhile is taken back by the table's next run: 12,000 of 20,000 rows had been left visible, then appended again.
- **`run --dry-run` no longer logs a SQL Iceberg catalog's password** from its `uri`, and the connection's repr leaves the `uri` out.

## 0.2.5 — 2026-10-06

Know where a job's time goes, keep a copy from taking more of production than it was given, and move every value as it was. History records each job's seconds reading, masking and writing; `bauta bench` measures a job against its source before anything is copied; `maxRowsReadPerSecond` caps what jobs read from a connection. An upgrade leaving the native masker behind stops the run rather than slowing every masked job tenfold. A policy naming a column twice is refused. PostgreSQL's JSON crosses exactly; Oracle's binary doubles, SQL Server's empty binaries and DuckDB's mixed JSON load; a NaN a target can't hold fails by name; and large subsets no longer crawl on PostgreSQL. **Read Breaking first.** Masks change only for a MySQL or DuckDB JSON column masked with a plain strategy.

### Breaking
- **A key given twice in one mapping of any configuration file is an error**, naming both lines. YAML silently keeps the last, so `actor_email: email` then `actor_email: keep` validated, read as masked, and copied every address. A key given again over a merge (`<<: *defaults`) still overrides it. See [the security model](docs/concepts/security.md#what-stops-a-copy-going-out-unmasked).
- **PostgreSQL's `json` and `jsonb` are read as their text**, as the other databases return JSON, so documents cross exactly: parsed, `"123"` came back as `123`, a bare number or string failed the job, and `12345678901234567890.123` lost digits through a float. A JSON column masked with anything but `keep`, `json` or `null` is decoded before masking and encoded after, so it masks as before. **A transform** on a kept JSON column now receives text, not a dictionary. JSON Lines writes the column as the JSON it holds, and fails the job on a value that isn't JSON.
- **A PostgreSQL JSON `null` stays JSON `null`** where kept or masked with `json`, where it loaded as SQL NULL; a check such as `doc IS NULL` may count differently.
- **Masks change for a MySQL or DuckDB JSON column masked with a plain strategy** (such as `email` on bare JSON strings): it keyed on the quoted text, so it masked unlike the same value elsewhere, and a JSON column refused the result. It is now decoded and encoded as PostgreSQL's is. `keep`, `json` and `null` are unchanged, as is MariaDB.
- **A NaN or infinity a target can't hold fails the load, naming the table and column**: MySQL, MariaDB and SQL Server read NaN as a column name, an Oracle `NUMBER` refused it, and **SQLite stored NaN as NULL silently**. The new `nullIfNotFinite` transform turns them into NULL. Not retried.
- **A run with masked jobs stops before it starts where `bauta-rs` is another version than `bauta`'s**, and `validate` fails, rather than masking ten times slower with a warning, as upgrading `bauta` alone did. Install the matching version, or set `requireNative: false` (or `BAUTA_REQUIRE_NATIVE=0`). Where the extension was never installed or is turned off, runs go on, now saying they mask in Python. See [the native masker](docs/guides/make-it-faster.md#the-native-masker).
- **`bauta history`'s table has four more columns**, `READ`, `MASK`, `WRITE` and `WAITED`, before `ERROR`; `--format json` gives the same four fields.

### Added
- **Where the time went.** Every completed job records its seconds busy reading, masking, writing (with its swap or commit) and waiting on a read limit, in history and notifications (`readSeconds`, `maskSeconds`, `writeSeconds`, `throttledSeconds`), its JSON log (`stages`) and `bauta history`. The stages overlap, so the largest set the pace. A history table from 0.2.4 or earlier works without them; [four columns](docs/reference/tables.md) keep them. See [where the time went](docs/guides/watch-what-ran.md#where-the-time-went).
- **`bauta bench`** reads up to `--rows` of each job's query from its real source, masks it as `run` would, writes nothing, and reports reading and masking rates and which would set the pace. See [measure a job](docs/guides/make-it-faster.md#measure-a-job).
- **`maxRowsReadPerSecond`** on a database connection caps what all jobs and partitions together read from it, across processes. See [limiting what a job reads](docs/guides/make-it-faster.md#limiting-what-a-job-reads).
- `BAUTA_REQUIRE_NATIVE=0`, as `requireNative: false`.
- **The `nullIfNotFinite` transform**: NULL for a NaN or infinite float, which `nullIf` can't match, since NaN equals nothing.
- From Python: `masking.requiresNative`, `masking.nativeUnavailableReason`, `JobOutcome.stages` and `bauta.jobs.throttle`.

### Changed
- **Subset queries select a table wanted for several reasons as a `UNION ALL`**, a branch per reason, where selections are materialized (PostgreSQL, SQLite, DuckDB), not one `WHERE` joining `EXISTS` with `OR`, which PostgreSQL ran by rereading the selection for every row once it outgrew `work_mem`: 2,000 customers of 200,000 took 18 seconds, and now 0.03. The rows are the same. See [copy a subset](docs/guides/copy-a-subset.md).
- **`subset` proposes masking without `--mask` where `--target` has `requireMasking`**, whose jobs would otherwise fail validation, and warns where its jobs copy every column as it is.
- **`schema` keeps a column's spelling and a PostgreSQL array's type between two databases of the same kind**: `"Order"` became `order`, and `integer[]` became `TEXT`. Across kinds, columns fold and arrays become text as before, with a note.
- **With `run --job`, a manifest file gains the run's jobs rather than being replaced**, under a lock, so runs of single jobs side by side don't drop each other's. What the file held must verify first; an altered one is moved aside as `<name>.rejected-<time>`, and one signed with a key the run can't check is kept, the run's manifest written beside it as `<name>.unmerged-<time>`. A manifest file is always replaced in one step. See [keep a manifest](docs/guides/keep-a-manifest.md).
- **`json` writes a number it leaves unmasked as it came**, `1.10` included, not through a float. Masks are unchanged.
- **`discover` and `audit --connect` find personal data in JSON whatever else the sample holds**: one null document, or a column of bare JSON strings, hid every email and SSN in the rest.

### Fixed
- **Oracle `BINARY_DOUBLE` and `BINARY_FLOAT` take NaN, the infinities and values past 1e126**, which binding as `NUMBER` refused.
- **SQL Server takes an empty binary value**, which pymssql sent as an empty varchar.
- **DuckDB takes a JSON array mixing numbers and objects**, which it tried to cast to a `STRUCT`.
- The README said a second `run` refuses to start while one is going; runs of different jobs with `--job` go side by side.
- **`bauta --version` and `validate` say why masking runs in Python**, rather than suggesting an install that changes nothing.

## 0.2.4 — 2026-10-06

Masked copies that leak less and stay truer to production. Driver errors in unknown formats fail closed, and DuckDB's errors and the drivers' own loggers are scrubbed. `json` masks identifiers held as numbers and in keys. Weak keys are refused or warned about, and append jobs are guarded against a key change. `audit --connect` checks kept columns and shared domains by their values. New: `normalize`, `dateShift` by a column, a `coordinate` strategy, larger fake-name lists that agree across columns, `requireNative`, partitions, a swap that keeps the target's keys, and HTML reports. **Read Breaking first.** Masks change only where `json` used to copy a value unmasked.

### Breaking
- **DuckDB 1.5 or newer is required**: every 1.4 release refuses to reload a table emptied while another connection holds a transaction, so a swap job reading and writing one file failed every third run.
- **A masking or signing key of fewer than 8 different characters is refused**: `aaaaaaaaaaaaaaaa` met the length rule, and its fingerprint gave it away offline. A key estimated under 128 bits is warned about; a future release will refuse it. See [keys](docs/concepts/security.md#keys).
- **An append job writing files or Iceberg stops the run when its masking key changed**, as an upsert job does: its target holds rows masked under the old key. Remove what it wrote and run with `--accept-key-change`. See [rotating the masking key](docs/guides/rotate-the-key.md).
- **`json` masks identifiers held as numbers and in object keys**, under its default `otherwise`: a number long enough to be a phone or card number is masked as its digits and stays a number, and a document keyed by email has its keys masked. Both were copied unmasked. `otherwise: keep` keeps both.
- **`audit --connect` samples up to 1,000 rows of each query**, not one, and may newly warn about kept columns that look personal and domains held differently. `--sample 0` reads none.
- **A `swap` job whose source repeats a key now fails** where the target has a primary or unique key, since the stage now gets the target's keys (see Changed). It reached the target every other run. Deduplicate in `sourceQuery`.
- **A driver error in an unknown format loses its quoted text** before it is logged, recorded or posted. An alert matching on such text may need to match on less.

### Added
- **`normalize`** on `key`, `fpe` and `hash`: `strip` and `lower` make values a database compares as equal (a `CHAR`'s padding, a case-insensitive collation) mask alike, and `integer` masks an id held as text as the integer it spells. See [values held more than one way](docs/reference/strategies.md#values-held-more-than-one-way-normalize).
- **`dateShift` with `shiftBy`** keys the shift on another column, a person's id say, so all of one person's dates move together and keep their order. See [`shiftBy`](docs/reference/strategies.md#dateshift-by-a-column-shiftby).
- **A `coordinate` strategy** moves a point by a keyed distance on the ground, wherever it is, where `number`'s variance barely moved one near the equator. See [`coordinate`](docs/reference/strategies.md#coordinate).
- **`lists: 2`** on the `fake*` strategies picks from longer lists and makes the name strategies agree: a full name masks part by part as `fakeFirstName` and `fakeLastName` do, in one domain, whatever its case. **`matchGender: true`** keeps a known first name's gender. Masks without `lists: 2` are unchanged. See [the larger lists](docs/reference/strategies.md#the-larger-lists-lists-2).
- **`requireNative: true`** (or `BAUTA_REQUIRE_NATIVE=1`) stops a masked run before it starts, and fails `validate`, whenever the native masker isn't in use. See [the native masker](docs/guides/make-it-faster.md#the-native-masker).
- **`audit --connect` questions kept columns by their values**, so `SELECT ssn AS ref` with `ref: keep` no longer passes, notes kept UUIDs, and warns about a domain held as a number in one column and text or padded text in another. No sampled value appears in the report. See [reviewing policies](docs/guides/prove-the-copy-is-safe.md#reviewing-policies-audit).
- **`partitions: {column: id, count: 8}`** reads a job as that many ranges at once, each with its own connections, reader, masker and writer, all loading one table. The job succeeds only if every slice does. A masked two-million-row PostgreSQL copy ran 3.1 times as fast in four slices with the native masker; without it, slices take turns at Python's lock and gain nothing. See [partitions](docs/concepts/how-it-works.md#partitions).
- **`partitions: auto`** chooses as each job starts: its target's primary key, and the fewest of its share of the cores, its connections' free places and one slice per 250,000 rows. A job it can't slice is read as one stream, so `auto` can sit under `defaults`. See [`partitions: auto`](docs/guides/make-it-faster.md#partitions-auto).
- **`audit --format html` and `coverage --format html`**: one self-contained page for a reviewer, fetching nothing, never holding a value or a key.
- From Python: `keyStrengthBits`, `warnIfWeakKey`, `requireNativeProblem`, `installDriverScrubbing`, `discovery.valueHint`, `log.scrubbing.scrubForeignText`; and for a custom strategy, `CONTEXT_OPTION`, `contextColumn()`, `maskColumnWith()` and `defaultDomain()`.

### Changed
- **`discover` proposes `null` for gender, race, ethnicity, religion and nationality**, special categories under GDPR, where `shuffle` left every real value in the table.
- **`discover` proposes `key` with `charset: hex` for UUIDs**, which other systems may hold too, where it proposed `keep`.
- **`discover` proposes `coordinate` for latitudes and longitudes**, where it proposed `number`.
- **A configuration that fails validation is reported without pydantic's error chained behind it**, which quotes its input, keys and passwords included.
- **A swap gives its stage the target's primary and unique keys** before loading, unnamed, so the live table has them every run; a key that can't be added is a warning.
- `audit` no longer counts a difference in `normalize` alone as a domain masked two ways.

### Fixed
- **DuckDB's error messages were not scrubbed**: conversion, duplicate-key, foreign-key and cast errors reached logs, history and webhooks with values in them.
- **The drivers' own loggers were not scrubbed**; the command and every job process now scrub each record as it is made.
- A warning that old snapshots couldn't be removed quoted its error unscrubbed.

## 0.2.3 — 2026-09-30

Configuration scales to hundreds of tables: jobs split across files, generated jobs sharing `defaults`, and `discover --update` when production's columns change. `run --full-refresh` takes deleted rows out of incremental copies. Foreign keys are read from each table's own schema, and JSON and `inet` columns are masked and discovered. The documentation is a website, at [ribeiro11075.github.io/bauta](https://ribeiro11075.github.io/bauta/), one copy per release. **Read Breaking first: relative database paths move, and `requireMasking` refuses `defaultStrategy: keep`.** No mask changes.

### Breaking
- **A relative path in `connections.yaml` is relative to that file**, not the working directory, as `jobs.yaml`'s already were: a SQLite or DuckDB `path`, a files `root`, an Iceberg `warehouse`. A configuration written for commands started above `configuration/` needs `path: ../prod.db` for `path: prod.db`.
- **A SQLite or DuckDB file that doesn't exist is an error**, except for a job's target and `schema --apply`: a misspelled source path opened an empty database. From Python, pass `Database(..., create=True)` to create one.
- **`--quiet` leaves errors on stderr**, one line each, where it left nothing (and Python then printed every traceback itself). A job's failure is reported once. Under cron, that is a mail when a run fails and none when it succeeds.
- **`discover` and `subset` write what their jobs share under `defaults`**, and leave out built-in values. `--self-contained` writes every setting into every job as before.
- **`bauta history` heads its first column `FINISHED (UTC)`**; a script splitting the header on spaces sees one more field.
- **`requireMasking` refuses a `defaultStrategy` that copies values as they are**: `masking: {defaultStrategy: keep, columns: {}}` satisfied it and copied everything. See [requiring masking](docs/reference/connections.md#requiring-masking).
- **`verify-references` exits 1 when it checked no key but keys exist in schemas the jobs don't use**, where it passed with "Checked 0 foreign key(s)". With no key declared anywhere it still succeeds.

### Added
- **`include` in `jobs.yaml`** merges more files of jobs and `acknowledged` tables, by path or glob, such as `jobs.d/*.yaml`, one per team. `defaults` reaches them all; a job or table in two files, or a pattern matching nothing, is an error; the manifest records each file's digest. See [splitting the jobs across files](docs/reference/configuration.md#splitting-the-jobs-across-files).
- **`bauta discover --update`** compares each masked job with what its query returns now, proposing a policy for each new column and reporting each gone one; it exits 1 on drift, for CI. **`--apply`** writes the change into each job's file, leaving every other line as it was. See [keeping a policy up to date](docs/guides/propose-a-policy.md#keeping-a-policy-up-to-date).
- **`bauta run --full-refresh`** removes rows deleted from the source, which watermarks can't see: each incremental job loads its whole source and replaces the target, by swap or one Iceberg commit, carrying on incrementally from what it read. A job it can't refresh is refused beforehand, and `audit` warns of it. Schedule it within your erasure deadline; see [deletes](docs/concepts/how-it-works.md#deletes).
- **`bauta schema --all-tables`**, with `--schema`, creates every table in a database.
- **Examples for Airflow and Dagster** in `example/orchestrators/`, each with a weekly full refresh, and a demo running a task per job without either. See [run from Airflow or Dagster](docs/guides/run-from-an-orchestrator.md).
- **`python -m bauta`**, where the script isn't on `PATH`.
- **A `json` strategy** masks inside a JSON document: each path `fields` names (`contact.email`, `items[].card`) with its own policy and domain, and everything else with `otherwise`, `redact` by default. See [`json`](docs/reference/strategies.md#json).
- **`discover` looks inside JSON documents** and proposes a `json` policy naming each path holding personal data, where it proposed `keep`.
- **A `cardSecurityCode` rule** proposes `null` for CVV and similar columns, which PCI DSS forbids storing, and `audit` questions one kept.
- **`audit --connect` warns when foreign keys are declared only in schemas the jobs don't use**, and notes keys a source declares that its target doesn't.
- From Python: `readJobsFile(path)`, `validateConnectionConfiguration(raw, directory=...)`, `Database(..., create=True)`, `Database.getForeignKeys(schema)`, `getForeignKeysFor(tables)`, `foreignKeysElsewhere(schemas)`, and `Strategy.bindKey(key)`.

### Changed
- **`run --job` locks only the jobs it runs**, so runs of different jobs go side by side, as an orchestrator's tasks do. The same job never runs twice at once. On Windows, which has no shared lock, they still wait for each other.
- **Jobs opening a new SQLite file at once no longer fail** with "database is locked" while switching it to WAL.
- An `OSError` during a run, such as a full disk, is reported as itself, not as another run holding the lock.
- **A masking key changed under many jobs is reported once**, grouped by old and new fingerprint.
- The package's `Documentation` link points to the website.
- **The documentation is reorganized by task**: getting started, guides, reference and concepts. `docs/masking.md`, `docs/configuration.md` and `docs/operations.md` were split into pages under `docs/guides/` and `docs/reference/`, so links to the old files no longer resolve.
- **Every command has a page**, generated from its parser.

### Fixed
- **Foreign keys in a schema other than the connection's own were never read**, so `schema --apply` created none, `discover` gave keys the wrong domain, and `verify-references` passed copies of orphans. Keys are now read from each table's schema on all seven databases.
- **`email`, `digits`, `key`, `fpe` and `redact` take a JSON document or an IP address** (as psycopg returns `jsonb` and `inet`), where they failed the job.
- **`discover` proposes a mask for an `inet` address**, where it proposed `keep`.
- **`discover --update` recognises a query reading a schema-qualified table**, so a new foreign key gets its parent's domain.
- **`audit --connect` blames a target table it couldn't read**, not the job's `sourceQuery`.
- **`--schema other` on Oracle** looked for `"other"` where Oracle holds `OTHER`.
- `discover --help` and `subset` named a `--database` flag, which is `--connection`.

## 0.2.2 — 2026-09-27

Faster jobs, most of all wide masked tables and anything written to files or Iceberg: 25 masked columns on ten cores went from 78,900 rows a second to 106,100 into SQLite with `maskingThreads: auto`, on half the threads, and from 22.2 to 10.2 seconds a million rows into Parquet. No mask changes, and nothing is written differently.

### Changed
- **`maskingThreads: auto` masks on half the cores**, not all: masking only has to keep up with reading and writing, which need cores too, and on ten cores four threads copied a wide table as fast as six or eight, and ten were slower. A number still sets exactly that many. See [masking threads](docs/guides/make-it-faster.md#masking-threads).
- **The native masker takes a chunk in one call**, masking its columns at once rather than handing the GIL back after each, and frees its strings without holding the GIL. Values Python finishes are finished in the same order, so a refusal says the same.
- **Loading a chunk does less work in Python**: the values a driver takes differently are found from each row's types in one pass, in Rust where it can, not by scanning every column.
- **Writing files and Iceberg does a fraction of the work in Python**: a column of plain types goes to Arrow without checking each value, which took over half a wide table's time.
- **Reading, masking and writing overlap two chunks deep**, absorbing a slow chunk on either side; a job holds four chunks rather than three.

## 0.2.1 — 2026-09-27

Jobs can write Iceberg tables through a Glue, REST or SQL catalog, in any of the three clouds: one commit a run, `upsert` by key, and old snapshots' files deleted with them. **Read Breaking first: pyarrow and DuckDB need newer versions.** No mask changes.

### Breaking
- **pyarrow 21 or newer for the `files` and `duckdb` extras, and DuckDB 1.4 or newer.** pyarrow 21 has every Azure credential and copies of any size; DuckDB before 1.4 reported a DECIMAL without its precision.
- From Python: `bauta.files` is now `bauta.lake`, and `bauta.configuration.fileTypes` is `columnTypes`. The connection models are in `bauta.configuration.connections` (still exported by `bauta.configuration`), and `DataJobConfig.writesFiles()` and `targetMismatch()` are replaced by `targetProblems(job, connection)`, `targetKind()` and `isLake()`.

### Added
- **An `iceberg` connection** (`pip install "bauta[iceberg]"`) writes Iceberg tables through pyiceberg, with no JVM, via a `glue`, `rest` or `sql` catalog. `append` and `overwrite` commit a run once; `upsert` merges by `targetKey` or the table's identifier fields, a commit per row group. A missing table is created; an existing one decides each column's type, and refuses a new column unless `evolveSchema`. Each run keeps `keepSnapshots` snapshots and deletes the files only older ones referenced. See [Iceberg](docs/reference/connections.md#iceberg) and [Iceberg tables](docs/concepts/how-it-works.md#iceberg-tables).
- `run --dry-run` checks an Iceberg catalog answers and its warehouse can be written.
- Iceberg on Azure, which pyiceberg 0.12 alone can't write.

## 0.2.0 — 2026-09-26

Jobs can write files: a `files` connection is a directory, or a bucket in S3, Google Cloud Storage or Azure, of tables in Parquet, CSV or JSON Lines. No mask changes, and nothing existing changes behaviour.

### Added
- **A `files` connection** (`pip install "bauta[files]"`) writes each job's rows as a table of files under its `root`: `append` adds a run's parts beside those already there, and `overwrite` publishes the whole table as a new `snapshot=<run>/`, keeping the newest `keepSnapshots`. `singleFile: true` writes a small table as one file. Nothing is visible until every row is written, and memory stays flat. Masking, `requireMasking`, retries, timeouts and `audit` work as for any job. See [files](docs/reference/connections.md#files) and [files as a target](docs/concepts/how-it-works.md#files-as-a-target).
- **Each column's Parquet type is fixed before the first row group**, declared in `targetColumnTypes` or settled by its first values, and a value that doesn't fit fails the job, naming the column, rather than being cast (pyarrow writes 1.5 into an integer column as 1).
- **S3** roots, with credentials from the AWS default chain or settings, a `roleArn` to assume, and an `endpoint` for MinIO and the like. Parts are staged and published by a copy, so a failed job leaves nothing visible. See [files](docs/reference/connections.md#files).
- **Google Cloud Storage and Azure** roots (`gs://`, `az://`, `abfss://`), with each cloud's default credentials or settings.
- **CSV and JSON Lines**, gzipped by default, each type spelled one way in both. See [formats](docs/concepts/how-it-works.md#formats).
- `run --dry-run` checks a files root can be written and listed.
- A files connection is a target only, for now; commands that need a catalog refuse it.
- From Python: `FilesConnection`, `DatabaseConfig`, `InsertStrategy.APPEND` and `OVERWRITE`, and `bauta.jobs.targets.LoadTarget`, of which `TableTarget` and `FileTarget` are the two kinds.

## 0.1.9 — 2026-09-26

DuckDB joins as a source or target, and connections replace databases in the configuration: `connections.yaml`, `sourceConnection` and `targetConnection`, each type checked for its own settings. Copies between databases load lists, JSON, UUIDs, times and unsigned integers that failed before. **Read Breaking first: every configuration needs editing.** No mask changes.

### Breaking
- **`database.yaml` is now `connections.yaml`**, and `sourceDatabase` and `targetDatabase` are `sourceConnection` and `targetConnection`, in `defaults:` too; the old names are refused. Rename the file, then the two settings.
- **The flags follow:** `--databases` is `--connections`, and `--memory-database`, `--history-database` and `--manifest-database` are `--memory-connection` and so on. A state table names its alias with `connection:`.
- **Each connection type takes only its own settings**: SQLite and DuckDB name their file with `path`, Oracle no longer takes an unused `database`, and a setting of another type is refused rather than ignored. From Python, `DatabaseConnectionConfig` becomes one class per type, `ConnectionConfig` is their union, and `connectionConfig(type=..., ...)` builds one.
- **`discover`, `subset`, `schema`, `synthesize` and `coverage` take `--connection`**, not `--database`. From Python, `validateDatabaseConfiguration` is `validateConnectionConfiguration`, and `validateJobGraph` takes `connections=`.

### Added
- **DuckDB** (`pip install "bauta[duckdb]"`), for jobs and every command. A chunk loads as one Arrow table: 50,000 rows in a tenth of a second, against 13 seconds row by row. See [DuckDB](docs/reference/connections.md#duckdb).
- **`maxConcurrentJobs` on a connection** caps the jobs using it at once, whatever `workers` allows; always 1 for DuckDB, which one process at a time may open.
- A DuckDB swap of a table in a foreign key or with an index fails before renaming anything, since DuckDB refuses or mishandles those renames. DuckDB sessions run in UTC, and `clear` empties DuckDB tables one transaction each.
- `discover` warns, masking DuckDB in place, that a table in a foreign key can't be swapped.

### Fixed
- **Lists and dictionaries load into SQLite, MySQL, MariaDB, Oracle and SQL Server as JSON text**, as `schema` maps them, where every such copy failed.
- **A UUID loads into MySQL and MariaDB, and a time of day into Oracle,** as text.
- **What each Python type is sent as, for each database, is one table** (`bauta/database/values.py`). bauta no longer registers process-wide adapters with `sqlite3`, and a `datetime` watermark keeps its microseconds on SQL Server.
- **`schema` maps MySQL's and MariaDB's unsigned integers to a type holding their range**, where the upper half of each was refused, or became a float in SQLite.
- **An integer past 64 bits loads into SQLite** as its exact text.

## 0.1.8 — 2026-09-21

The native masker covers `number`, masks `fpe` two and a half times as fast and `key` a quarter faster, and spreads integer columns across threads as well as text. `synthesize` no longer holds every key in memory, and the docs' throughput figures come from a benchmark in the repository. No mask changes.

### Changed
- **The native masker covers `number`**, four to six times as fast (a 5,000-row chunk of Decimals, 22 ms to 5), repeating Python's decimal arithmetic at 60 digits and handing back what it would have to guess at.
- **Masked columns are spliced back into rows by rebuilding them from columns** where that is faster (narrow tables, or a third or more masked): 7% off a typical masked chunk.
- **The native `fpe` is about four times as fast** (4.7 µs to 1.1 a value): FF1's rounds run in machine integers wherever a half fits 64 bits. Two `fpe` columns among six went from 105,000 to 254,000 rows a second, faster than `key`.
- **The native `key` is 20-30% faster**, its Feistel network in machine integers up to 2\*\*128.
- **Integers cross into and out of the native masker through the C API** where they fit 64 bits, where each was a Python call that held up every masking thread.
- **`synthesize` remembers generated keys only for a table keyed wholly by foreign keys**: it held every key, 116 MiB a million rows.
- **`benchmarks/masking.py` measures the figures the docs quote**, and fails if two copies of a policy differ. See [benchmarks](docs/project/development.md#benchmarks).
- None of this changes a mask: the fast paths are tested against the general ones at every width, and the vectors pass on both sides of each boundary.

### Fixed
- **A `number` value past 60 significant digits fails as a masking error naming its column**, not as a bare `decimal.InvalidOperation`.

## 0.1.7 — 2026-09-21

A watermark could leak an unmasked value; the package is reorganized into groups a newcomer can find; run-state connections are closed; and loads spend less time in Python. No mask changes.

### Breaking
- **A `watermarkColumn` that `defaultStrategy` masks is refused**, by `validate` and `run`: only named columns were checked, so such a watermark wrote its unmasked value to run state and logs.
- **Modules moved into subpackages, and the old import paths are gone**: `configuration`, `database` (`dialects`), `jobs`, `log`, `transform`, `masking`, `generate`, `review` and `cli`. `bauta.databaseDialects` is `bauta.database.dialects`; `bauta.builtinMasking`, `bauta.fakeData` and `bauta.fpe` are `bauta.masking.strategies`, `.fakeData` and `.fpe`; discovery and subsets moved under `bauta.generate`, and the reports under `bauta.review`. `bauta.masking.Strategy`, the command and every setting are unchanged.
- **`bauta` exports less**: exactly what [API stability](docs/reference/python-api.md#api-stability) documents. The dialect classes and `ColumnCategory` come from `bauta.database.dialects`.
- **A `MemoryBackend` implements all six methods**; the default for fingerprints silently turned off the key-change check.
- **The built-in transforms are `bauta.transform.builtinTransforms`**, not `bauta.builtinTransforms`.
- **A native masker refusing a strategy's options raises**, rather than silently masking in Python: the two are always one version, so it is a bug to report.
- **Run state from before the masking implementation was recorded** logs a warning once on each masked upsert job's next run.
- **`bauta.masking` exports the strategies and fake-data lists itself**, without the shim that found them lazily.

### Changed
- **PostgreSQL COPY uses psycopg's own encoders**, twice as fast, storing identical rows.
- **Each chunk is checked by its distinct types, not value by value**: three to five times faster.
- **A PostgreSQL upsert creates its staging table once per connection**, not every chunk.
- **`run --dry-run` and `audit --connect` open one connection per alias**, not one per check.
- **Whether a column is masked is decided by its strategy's `PASSTHROUGH`**, so a custom passthrough strategy counts as `keep`. `canonical` is public in `bauta.masking`.

### Fixed
- **Run-state connections were never closed.** A `MemoryBackend` has `close()` and works as a context manager, and every command and job closes its own.
- **A connection whose session setup failed was left open**, one more each retry.
- **SIGTERM under `--forever` waited out `cycleSleepSeconds`**, outliving a container's grace period; it stops within a second.
- **A SQL Server swap of a table whose name holds a single quote failed.**
- **`run --dry-run` skipped a job's checks when another job shared the name of an alias whose check had failed.**

### Added
- [SECURITY.md](SECURITY.md) and [CONTRIBUTING.md](CONTRIBUTING.md).
- `ruff check` in CI.
- The test suite fails on a connection or file left open.
- The tests mirror the package. See [where the tests are](docs/project/development.md#where-the-tests-are).
- One suite runs the operations every database must perform against all of them; the copies per database had drifted.
- CI measures coverage across unit and integration suites, and fails below 95%.
- GitHub Actions are pinned to commits, and Dependabot keeps them, the dev tools and the Rust crates current.

## 0.1.6 — 2026-09-20

A misspelled setting can no longer produce an unmasked copy, `coverage` answers what the jobs don't cover at all, and masking is faster without any mask changing.

### Breaking
- **An unknown field in `jobs.yaml` or `database.yaml` is an error**: `maskng:` for `masking:` was dropped silently, leaving a job that read as masked and copied every column, with every check passing. `validate` names each, offline. Top-level `x-` keys are still allowed, for YAML anchors.
- **`audit` reports every unmasked job**, as a warning, or an **error** when its columns look personal; it was silent unless another job masked the same source. A reviewed job declares [`unmasked: true`](docs/reference/jobs.md#copying-without-masking).
- **`--accept-key-change` is refused for an upsert whose policy masks the target's primary key**: new keys made it insert a second generation of rows, sometimes over other rows, which `verify-references` passed. Use `bauta clear`, then run again; see [rotating the masking key](docs/guides/rotate-the-key.md).
- **`BoundMasking.apply` refuses rows wider than its policy**, whose extra columns it ignored.

### Added
- **`bauta coverage`** lists every table in a source database and what the jobs do with it, exiting 1 on any **NOT COVERED**, so CI fails when production gains a table. See [coverage](docs/guides/prove-the-copy-is-safe.md#coverage-what-the-jobs-do-not-cover).
- **`acknowledged` in `jobs.yaml`** records the tables left out on purpose, and why. See [acknowledged](docs/reference/jobs.md#acknowledged).
- **`requireMasking` on an alias** refuses any job reading or writing it unmasked, at `validate`, whatever the job says. See [requiring masking](docs/reference/connections.md#requiring-masking).
- **`unmasked: true` on a job** says it was reviewed and copies as it stands.
- **`defaults` in `jobs.yaml`** supplies what every job would repeat, the masking key included but never a policy's columns. See [defaults](docs/reference/jobs.md#defaults).
- **`discover --all-tables`**, with `--schema`, proposes for every table.
- **`--mask-keys`** on `discover` and `subset --mask` proposes `key` for numeric surrogate keys too, in each relationship's shared domain. See [propose a policy](docs/guides/propose-a-policy.md).
- `Database.listTables()`, for base tables on every database.
- Runbooks for [rotating the masking key](docs/guides/rotate-the-key.md), [after a failed cycle](docs/guides/run-on-a-schedule.md#after-a-failed-cycle) and [one production, several environments](docs/guides/run-on-a-schedule.md#one-production-several-environments).

### Changed
- **`active`, `chunkSize` and `workers` have defaults** (`true`, `5000`, `1`). `insertStrategy` stays required: a wrong one gives wrong data, not an error.
- **`run --dry-run` compares the column counts it prints**, so a target that gained a column fails there, not at the next scheduled run.
- **A connection error names what it tried to reach**, resolved, never the password.
- `audit --connect` resolves every job's columns, an unmasked job's included.
- The starter configuration uses `defaults:` and drops empty and Oracle-only fields.

### Performance
None of this changes a mask: the reference vectors pass, in Python and natively.

- **A job with no transforms no longer copies every chunk twice**: 3.5 ms saved per 10,000 rows, on nearly every job.
- **A masking policy reads and rewrites only the columns it masks**: one masked column of 30 went from 18.3 to 6.1 ms a 10,000-row chunk.
- **`dateShift` derives each day's shift once**, not per row: 32.0 to 4.3 ms per 10,000 rows.
- **The checks each dialect made before a load are one pass**: 35.5 to 19.7 ms a chunk into SQL Server.
- **PostgreSQL's bulk load looks up each value's spelling by exact type**: 100.7 to 77.9 ms a 30-column chunk.
- **Run state in a table reuses one connection per process**, not one per read and write.
- **`bauta history` reads from the end of the file** rather than parsing it all.

### Fixed
- **State tables whose names need quoting** were written right and read back unquoted.
- `bauta/masking.py` and `bauta/builtinMasking.py` no longer import each other.

### Removed
- `proposeTable`'s unused `primaryKeys` parameter.

## 0.1.5 — 2026-09-20

### Breaking
- **`charset: hex` is case-sensitive, so its masks change.** Masked case-insensitively, `aB12cd34` and `ab12cd34` shared a mask and two keys could merge. `key` keeps each character's case; `fpe` masks both cases in one alphabet, so its masks may mix cases. Re-mask copies that must match earlier ones.
- **Table names are quoted into the statements a run builds**, as column names were, so a name means a table, not SQL: an Oracle database link (`orders@remote`) must become a view or a table. See [how names are written](docs/concepts/how-it-works.md#how-names-are-written).
- **`dateShift` is keyed on the day, so timestamp masks change.** Keyed on the whole value, a day's rows scattered across the month, and a `DATE` and a `TIMESTAMP` of one day disagreed. Dates and ISO date text mask as before; re-mask copies whose timestamps must match earlier ones.
- **SQLite connections enforce declared foreign keys**, as other databases do, so a load breaking one, or emptying a referenced table, fails. Add `PRAGMA foreign_keys=OFF` to a job's `preTargetAdhocQueries` where it must load such rows.

### Added
- **`bauta verify-references`** counts, per foreign key on the loaded tables, the rows pointing at nothing, from the target's keys and its sources'. Counts only; exits 1 on any orphan. See [checking the copy's references](docs/guides/copy-a-subset.md#verify-references-checking-the-copys-references).
- `audit --connect` warns about a job copying part of a referenced table that its referencing job doesn't match, a job that can load before the table it references, a swap of a table that declares foreign keys, and keys whose two ends are both shuffled; and reports an error for a swap of a table a foreign key references. See [tables that reference each other](docs/concepts/how-it-works.md#tables-that-reference-each-other) and [how a swap works](docs/concepts/how-it-works.md#how-a-swap-works).
- The partial-copy check recognises `LIMIT`, `TOP`, `FETCH FIRST` and joins, as well as `WHERE` and watermarks.

### Changed
- `schema` maps `TIME` into Oracle as `VARCHAR2(32 CHAR)`, wide enough for MySQL's day-long values.
- The cycle message from `schema`, `synthesize` and `clear` no longer offers `--no-foreign-keys`, which only `schema` has.

### Fixed
- **A table whose name must be written in quotes was unusable**: catalog lookups bound the quotes too, so it seemed to have no columns or key. Names are now unquoted for lookups and quoted into statements, by every command. See [how names are written](docs/concepts/how-it-works.md#how-names-are-written).
- **A decimal lost digits in a SQLite copy**, since `schema`'s `DECIMAL(38,10)` has numeric affinity. A decimal is now created as `TEXT`; recreate older tables to hold exact values.
- **A MySQL `TIME` was corrupted or refused wherever it was copied**, being a duration. It is written as `[-]HH:MM:SS[.ffffff]`, and a value no `TIME` column holds is refused.
- **`schema` couldn't tell MySQL's `INT UNSIGNED` from `INT`**, halving its range silently; unsigned types are now read and noted.
- **Two tables sharing a generated job name became one job** (`orders` and `Orders`, or one name in two schemas); names are now distinct.
- **A `sourceQuery` returning fewer columns than its target failed with a driver message naming nothing**; it now says what each side has.
- **An upsert into a target without a primary key wrote first and refused after**; the target is checked before anything is written.
- **A PostgreSQL `json` or `jsonb` column couldn't be copied**: psycopg refused to write back the dict it read.
- **A load a masked value overflowed blamed the column**; it now names the likely mask and what to do. See [`key`](docs/reference/strategies.md#key).
- **An Oracle swap could lose the stage table for good** when a rename was blocked; a failed rename now undoes the others.
- **A SQLite swap repointed every view on the target at the stage**, which the next run emptied; views now keep naming the target.
- **A `postTargetAdhocQuery` failing after a swap reported 0 rows moved**; it now reports the rows the target holds.
- **A run killed outright left its jobs running**, beside the next run, which the dead run's free lock let start; a job now ends when its run does. See [workers](docs/concepts/how-it-works.md#workers).
- **A name longer than the target keeps loaded whatever table shared its first 63 characters**, since databases truncate silently; such a name is now refused.
- **A SQL Server swap of a bracketed name built a temporary name its parser refused.**
- **`schema` copied constraint names verbatim**, which collide on MySQL, MariaDB, Oracle and SQL Server; they are now made unique.
- **`schema` emitted foreign keys to unique columns it hadn't made unique**; it now copies those constraints.
- **`schema` mixed Oracle's upper-case catalog names with the names typed**; every table is created as it was asked for.
- **`schema` was silent about several lossy conversions** it documents; it notes them now.
- **`synthesize` repeated its values on a second run**; generators continue from the rows already there, and a refused row is reported as a `SynthesisError`.
- **`digits` copied a value unmasked when its keeps covered every digit**; such a value now fails the job.
- **A masked column could be the watermark**, leaking its value into run state and logs; `validate` refuses it.
- **Foreign keys into another schema were reported without it**, so checks matched a same-named table elsewhere; parents are now named `schema.table`.
- **On SQL Server, foreign keys outside the login's default schema were invisible.**
- **Oracle dropped every timestamp's fraction of a second**, binding datetimes as `DATE`.
- **Oracle returned every scaled `NUMBER` as a float**, losing digits; they are `Decimal` now.
- **SQL Server truncated timestamps to milliseconds**; they are sent as text.
- **SQL Server converted whole columns of a chunk to one type**, rounding exact values; such chunks load row by row.
- **A `Decimal` watermark was written to a run-state file as a float**, skipping rows; it is written exactly.
- **Masking in place left the unmasked original in the stage table** after the swap; the stage is emptied now.
- **On Oracle, foreign keys were read from the login's schema, not `currentSchema`.**

## 0.1.4 — 2026-09-19

### Added
- `bauta --version` prints the version and which masker a run would use.

### Changed
- The built-in strategies moved to `bauta/builtinMasking.py`, and the fake-data lists to `bauta/fakeData.py`; old import paths still worked.
- `bauta audit` no longer offers `--memory` flags it never read.

## 0.1.3 — 2026-09-18

### Added
- **Masking on several cores**, set by `maskingThreads` (`1`, a number, or `auto`) or `BAUTA_MASKING_THREADS`, with identical results for any count. See [masking threads](docs/guides/make-it-faster.md#masking-threads).
- The native masker covers the `fake*` strategies, from the lists Python hands it.
- The native masker remembers masks across chunks for `key`, `fpe` and `fake*`, and allocates through mimalloc.
- The native-masking demo compares Python and Rust, and one core against all of them.

### Changed
- A 25-column masked table runs at about 73,000 rows a second on ten cores, from 18,000. See [speed](docs/guides/make-it-faster.md#what-each-strategy-costs).
- The documentation was made consistent, and every figure re-measured.

## 0.1.2 — 2026-09-18

### Breaking
- **`bauta init` is removed.** Copy `example/starter/configuration/` instead.
- **Prometheus metrics are removed** (`--metrics`, `--metrics-push`, `writeMetricsFile`, `pushMetrics`); take the flags out of scripts. History in a table covers alerting; see [run history](docs/guides/watch-what-ran.md#run-history).
- **PostgreSQL uses psycopg 3** (3.2.10 or newer), not psycopg2, so `bauta[postgresql]` installs without a compiler.

### Added
- `memory`, `history` and `manifest` in `jobs.yaml`, each a file or a table, with flags to override them.
- Manifests in a table, read back by `bauta verify-manifest`, or `--run RUN_ID` for an earlier one.
- `discovery.yaml`: rules of your own for recognising personal data, ahead of the built-in ones.

### Changed
- The built-in discovery rules moved to `bauta/builtinDiscovery.py`.

## 0.1.1 — 2026-09-18

### Added
- **`bauta-rs`, the native masker, on PyPI**: `pip install "bauta[native]"`, with wheels for Linux and macOS.
- `bauta` uses `bauta-rs` only at its own version, and masks in Python, with a warning, beside any other.
- `bauta init`, which wrote a starter configuration (removed in 0.1.2).

## 0.1.0 — 2026-09-18

The first release on PyPI: masking, discovery, subsets, synthetic data, `audit`, sealed manifests, incremental loads and a dependency graph, across Oracle, SQL Server, PostgreSQL, MySQL, MariaDB and SQLite.

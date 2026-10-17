# Using it as a library

Most deployments should use the `bauta` command. Embed the library when a load needs to be one step inside a larger Python program.

You load configuration however you like and hand it over as plain data. Each driver is imported only when a connection of its type opens.

- [API stability](#api-stability)
- [Configuration models](#configuration-models)
- [A configuration directory](#a-configuration-directory)
- [Running jobs](#running-jobs)
- [Results](#results)
- [Memory backends](#memory-backends)
- [History, manifests and notifications](#history-manifests-and-notifications)
- [Masking, discovery and subsets](#masking-discovery-and-subsets)
- [Streaming directly](#streaming-directly)


## API stability

Everything importable from `bauta` itself is the public API, and this page describes all of it. A few names this page points to in a submodule -- `bauta.database.dialects`' quoting helpers, `bauta.review.references`, `bauta.generate.schema` and `bauta.generate.subset` -- are public there. Anything else reached through a submodule is internal and may change in any release. A custom masking strategy subclasses `bauta.masking.Strategy`, and may use `KeyedHash`, `MaskingError` and `canonical` from `bauta.masking`; see [your own strategies](strategies.md#your-own-strategies).


## Configuration models

`Configuration.validateConnectionConfiguration(raw, directory=None)` and `Configuration.validateJobConfiguration(raw, DataJobsFile)` turn loaded YAML into these pydantic models, raising `ConfigurationError`. With `directory`, the one `connections.yaml` is in, a relative database path is taken relative to it, as the CLI does; without it, relative to the working directory. `readJobsFile(path)` reads a jobs file with `${NAME}` expanded and the files its [`include`](configuration.md#splitting-the-jobs-across-files) names merged in, returning a `JobsDocument` whose `content` is what to validate, `files` the files read, and `origins` the file each job came from; a mapping that still holds `include` is refused. `Configuration.validateJobGraph(jobs, connections=...)` checks predecessors and aliases. `expandEnvironmentVariables(raw)` does what the CLI does to `${NAME}` first. The fields are the ones [configuration.md](configuration.md) describes.

| Model | Holds |
| --- | --- |
| `ConnectionConfig` | One `connections.yaml` alias: a `PostgreSQLConnection`, `MySQLConnection`, `MariaDBConnection`, `MSSQLConnection`, `OracleConnection`, `SQLiteConnection`, `DuckDBConnection` or `FilesConnection`, as its `type` says. Build one with that class, `SQLiteConnection(path='copy.db')`, or from settings as the file would give them, `connectionConfig(type='sqlite', path='copy.db')`. |
| `DataJobsFile` | A whole `jobs.yaml`: `jobs`, `workers`, `maskingThreads` and the file-level settings, with `defaults` already applied. |
| `DataJobConfig` | One job. `insertStrategy` is an `InsertStrategy`; `masking` a `MaskingConfig`, or `None`. |
| `DiscoveryRulesFile` | A validated `discovery.yaml`, for `discoveryRules`. |

A `Transformer` is any callable taking one value and returning one; a job names it in `sourceQueryColumnTransforms`. `TransformError` is a transformer that raised, and `TransformResolutionError` a reference that doesn't resolve.


## A configuration directory

`Project` is a configuration directory as the `bauta` command reads it, for a program that works with one the way the command does -- a console, or a check of your own -- rather than loading each file itself.

```python
from bauta import Project

project = Project.load('configuration')          # or Project.load() for $BAUTA_CONFIG, else ./configuration

project.jobsFile, project.connections            # the validated DataJobsFile, and each alias's ConnectionConfig
project.validationProblems()                     # what `bauta validate` finds, as a list; [] if nothing
project.dryRunProblems()                         # what `run --dry-run` finds, connecting but moving no rows
project.audit(connect=True)                      # `audit --format json`'s report, as a dict
project.coverage(connection='production')        # `coverage --format json`'s report, as a dict

history = project.history()                      # the RunHistory `run` records into, or None
records = history.read(limit=50) if history else []
manifest = project.readManifest()                # (where it was read from, its JSON), or None
```

`Project.load(directory=None, jobsPath=None, connectionsPath=None)` reads `jobs.yaml`, with its includes, and `connections.yaml`, validated as `bauta validate` validates them. It raises `ConfigurationError` for an invalid file and `bauta.project.UsageError` for one that isn't there. `document` is the jobs file as read: `files` every file its `include` brought in, and `origins` the file each job came from.

| Method | What it returns |
| --- | --- |
| `running()` | What `run` is doing now: a record per live cycle, as [`bauta status --format json`](../guides/watch-what-ran.md#whats-running-now) prints them, the latest first; empty if nothing is running. Each has `alive`: `False` for a run whose process is gone, and `None` for one on another machine. |
| `lockFile()` | The file `run` holds as its lock, beside its run state. |
| `location(setting)` | Where the jobs file keeps `'memory'`, `'history'` or `'manifest'`: a `Path`, relative paths taken from the jobs file's directory, or a `TableLocation`. `None` for history or a manifest it doesn't keep; run state defaults to `memory.yaml` beside the jobs file. |
| `history()` | The `RunHistory` to read, `FileHistory` or `DatabaseHistory`, or `None`. |
| `readManifest(runId=None)` | The manifest `run` last wrote, or a table's for `runId`, as `(where, manifest)`; `None` if none is kept or written yet. Check it with `verifyManifest`. |
| `validationProblems()` | The offline checks `validate` makes beyond loading: transformers that don't resolve, connection settings a driver would refuse, masking settings this machine can't honour. |
| `dryRunProblems(jobs=None)` | What `run --dry-run` finds for `jobs`, a mapping of name to `DataJobConfig` (default: all), logging what it checks as it goes. |
| `audit(jobs=None, connect=False, sample=1000, rulesPath=None)` | The [audit](../guides/prove-the-copy-is-safe.md#reviewing-policies-audit) report for the jobs named (default: all), offline unless `connect`. |
| `coverage(connection=None, schema=None, jobs=None, rulesPath=None)` | The [coverage](../guides/prove-the-copy-is-safe.md#coverage-what-the-jobs-do-not-cover) report for `connection`, by default the one every job reads from. |
| `verifyReferences(jobs=None)` | What [`verify-references`](../guides/copy-a-subset.md#verify-references-checking-the-copys-references) finds for the active jobs, or `jobs`: `(results, notes)`, a `ReferenceResult` per foreign key that applies to a copy, and an `UncheckedNote` per target where none did. `bauta.review.references.referencesReport(results, generatedAt, notes)` makes them JSON. |
| `drift(jobs=None, sample=1000, maskKeys=False, rulesPath=None)` | What [`discover --update`](../guides/propose-a-policy.md) finds: a `JobDrift` for each masked job whose policy no longer names exactly the columns its query returns -- `added`, a proposal for each new column, and `removed`. Empty if none has drifted. `bauta.generate.drift.applyDrift(text, drift)` writes one into its file's text. |
| `schedule()` | Where each job stands, as `bauta jobs` shows it: `state` (`due`, `throttled`, `inactive`), `waitsFor`, `lastRunAt` and `nextDueAt` in seconds since 1970, `refreshMinutes` and `watermark`. |
| `memory()` | The `MemoryBackend` `run` keeps run state in, for reading in a `with` block. |

`audit` and `coverage` use `discovery.yaml` from the directory when there is one, or `rulesPath`. None of these writes anything, and no report holds a value from a table or a key.


## Running jobs

```python
from pathlib import Path

import yaml
from bauta import (Configuration, DataJobsFile, FileMemory,
                   expandEnvironmentVariables, readJobsFile, runDataJobs)

def load(path):
    with open(path) as file:
        return expandEnvironmentVariables(yaml.safe_load(file))

def main():
    connections = Configuration.validateConnectionConfiguration(load('connections.yaml'), directory='.')
    jobsFile = Configuration.validateJobConfiguration(readJobsFile(Path('jobs.yaml')).content, DataJobsFile)
    Configuration.validateJobGraph(jobsFile.jobs, connectionAliases=set(connections))

    result = runDataJobs(
        jobsFile=jobsFile,
        connectionConfiguration=connections,
        memory=FileMemory(memoryFile='memory.yaml'),
        )

    if not result.succeeded:
        raise SystemExit(1)

if __name__ == '__main__':
    main()
```

**Keep the `if __name__ == '__main__':` guard.** Each job runs in a process of its own, started with Python's `spawn` method on every platform, and each one begins by importing your script. Without the guard, that import starts the run again.

`expandEnvironmentVariables` is what resolves `${NAME}` references — the CLI calls it for you, but a library caller must, before validating. It raises `ConfigurationError` naming every unset variable.

```python
runDataJobs(jobsFile, connectionConfiguration, memory,
            logFile=None, runForever=False, logLevel=logging.INFO, logFormat='text',
            acceptKeyChange=False, onCycle=None, onProgress=None)
```

- **`onCycle`** is called with each cycle's `RunResult` as the cycle ends, including under `runForever`. See [below](#history-manifests-and-notifications). An exception it raises is logged, not raised.
- **`onProgress`** is called with a `RunProgress` as each cycle starts, as each of its jobs starts or is decided, and once more as it ends, for watching a run as it goes; see [below](#results). An exception it raises is logged, not raised. The CLI uses it for [`bauta status`](../guides/watch-what-ran.md#whats-running-now), through `bauta.jobs.status.RunStatus(directory)`.
- **`acceptKeyChange=True`** runs upsert jobs whose masking key changed since their last run; see [the key](../guides/mask-a-table.md#the-key).
- **`runForever=False`** makes one pass and returns. `True` keeps running, honouring `refresh`, until `SIGINT` or `SIGTERM`. Either signal stops new jobs from starting and lets running ones finish; see [stopping](../concepts/how-it-works.md#single-runs-not-a-daemon).
- **`logFile`** is optional. Without one, attach a stream yourself: `Log(level=...).addStreamHandler(sys.stderr)`. Workers' records are written by the calling process's handlers, whichever those are.
- **`logFormat='json'`** writes structured records — see [design.md](../concepts/how-it-works.md#structured-logs).
- **`jobsFile.maskingThreads`** sets how many threads each job masks with, as in the CLI; see [masking threads](../guides/make-it-faster.md#masking-threads).

To keep two runs that share run state from overlapping, as the CLI does, hold `exclusiveRun(path)` around the call. It raises `RunInProgressError` if another process holds the same lock file. `exclusiveRun(path, jobs=[...])` is what `run --job` holds: the lock shared, and each of those jobs' own exclusively, so runs of different jobs proceed together.

Validation raises `ConfigurationError`. `runDataJobs` also raises it before starting any work if a job sets `watermarkColumn` against a backend that can't store watermarks, or if `maskingThreads` is more than the cores available, and `DependencyGraph` raises it for predecessors that form a cycle.


## Results

`runDataJobs` returns a `RunResult`, for you to turn into an exit code, an alert or a log line.

| `RunResult` | Meaning |
| --- | --- |
| `succeeded` | `True` only if every active job completed. A skipped job counts against it. |
| `completed`, `failed`, `skipped` | Lists of `JobOutcome`. |
| `rowCount` | Rows moved across all jobs. |
| `outcomes` | Every `JobOutcome`, in completion order. |
| `interrupted` | `True` if a signal stopped the run. The CLI exits with 130. |
| `runId` | The cycle's id, made as it starts: the one its `RunProgress` carried, and the CLI's history and manifest record. |

| `JobOutcome` | Meaning |
| --- | --- |
| `job` | The job name. |
| `status` | A `JobStatus`: `COMPLETED`, `FAILED` or `SKIPPED`. |
| `rowCount` | Rows loaded. |
| `watermark` | The high-water mark reached, for an incremental job that found rows. |
| `error` | The failure, as `ExceptionType: message`; for a skipped job, what it was waiting on. |
| `attempts` | How many tries it took. |
| `durationSeconds` | Wall-clock time. |
| `masking` | For a masked job that completed, the policy applied to each column, as plain dicts. |
| `stages` | For a job that completed, the seconds it was busy at each stage, as a dict of `read`, `mask`, `write` and `throttled`. See [where the time went](../guides/watch-what-ran.md#where-the-time-went). |

`RunResult.maskingManifest(jobsFile.jobs)` builds the [masking manifest](../guides/keep-a-manifest.md#what-it-records) for the run.

A run with `runForever=True` returns only once it has been stopped, with the last cycle's outcomes.

| `RunProgress` | Meaning |
| --- | --- |
| `runId`, `startedAt` | The cycle's id, and when it started, in seconds since 1970. |
| `jobs` | Every job the cycle holds. |
| `running` | The jobs running now, each with when it started. |
| `outcomes` | A `JobOutcome` for each job decided so far: completed, failed or skipped. A job in neither `running` nor `outcomes` is waiting. |
| `finished` | `True` for the last call, as the cycle ends. |
| `rows` | The rows each running job has written so far, as it last said, about once a second; a job that has written none isn't in it. |


## Memory backends

A `MemoryBackend` holds what the scheduler needs *before* a job runs: when it last ran, for `refresh`, and how far it got, for watermarks. It isn't a record of what happened — that's `RunResult`.

| Backend | Use when |
| --- | --- |
| `FileMemory(memoryFile=...)` | A filesystem persists between runs and every worker shares it. |
| `DatabaseMemory(connectionSettings=..., table=...)` | It doesn't: a container without a volume, anything across several machines, or serverless. |

`FileMemory` in the wrong environment doesn't fail loudly: it forgets every watermark and re-extracts from `watermarkInitial`. `DatabaseMemory`'s table must exist first; its shape is `DATABASE_MEMORY_SCHEMA`, shown in [operations.md](tables.md).

### Writing your own

Subclass `MemoryBackend` and implement all six methods; a backend that leaves one out can't be created, since a store that forgot key fingerprints would quietly turn off the check that stops two masking keys mixing in one target.

| Method | Behaviour |
| --- | --- |
| `read()` | every job's last run time |
| `recordRun(job)` | record that `job` ran now |
| `readWatermarks()` | every job's stored watermark |
| `recordWatermark(job, value)` | store a watermark, as the type it was |
| `readKeyFingerprints()` | the masking identity each masked job last completed under |
| `recordKeyFingerprint(job, fingerprint)` | store one, or forget it with `None` |

**Closing.** Whoever creates a backend closes it, with `close()` or `with DatabaseMemory(...) as memory:`; each job's process closes its own copy when its job ends. `close()` does nothing unless overridden, for a backend that holds nothing open.

**One constraint:** the same instance is pickled into every worker process. Hold settings — a path, connection details — rather than an open file or connection, and open what you need inside each method.


## History, manifests and notifications

What the CLI's `history`, `manifest` and `--notify-url` do, as functions to call from `onCycle` or after a run:

```python
import os
from bauta import FileHistory, notify
from bauta.jobs.reporting import newRunId

history = FileHistory('history.jsonl')

def report(result):
    history.append(result, newRunId())
    notify(os.environ['ALERT_WEBHOOK'], result)

runDataJobs(jobsFile, connections, memory, onCycle=report)
```

| Name | What it is |
| --- | --- |
| `FileHistory(path)`, `DatabaseHistory(connectionSettings, table=...)` | `RunHistory` backends: `append(result, runId)`, and `read(limit=20, job=None)` newest first. `DATABASE_HISTORY_SCHEMA` is the table. |
| `DatabaseManifests(connectionSettings, table=...)` | Sealed manifests in a table: `write(manifest, runId)`, and `read(runId=None)`, which returns `(runId, manifest)`, the latest if no run is named. `DATABASE_MANIFEST_SCHEMA` is the table. |
| `notify(url, result, always=False)` | Posts `reporting.notificationPayload(result)` if the cycle didn't succeed, or always; returns whether it posted. |

See [operations.md](../guides/watch-what-ran.md#notifications) for the payload.


## Masking, discovery and subsets

The pieces behind `masking:`, `discover` and `subset` are all importable, and none of them does any I/O except through a `Database` you pass in.

```python
import os
from bauta import Database, MaskingPlan, planSubset, proposeTable

plan = MaskingPlan(key=os.environ['MASKING_KEY'], columns={'id': 'keep', 'email': 'email'})
masking = plan.bind(['id', 'email'])      # raises MaskingError for an uncovered column
masked = masking.apply([(1, 'ann@corp.com')])

with Database(connectionSettings=connections['prod']) as database:
    proposal = proposeTable(database, 'customers', sampleSize=500)
    subset = planSubset(database.getForeignKeys(), root='customers', where="region = 'eu'")
```

| Name | What it is |
| --- | --- |
| `MaskingPlan(key, columns, defaultStrategy=None)` | A validated policy. `bind(columns)` checks coverage and returns an object whose `apply(rows)` masks one chunk, and whose `manifest` lists what each column gets. `fingerprint` is the key's safe identifier. |
| `STRATEGIES` | Strategy name → class, for the built-in strategies, which are in `bauta.masking.strategies`. Each `Strategy` validates its own options in `validateOptions`. |
| `masking.setMaskingThreads(n)` | How many threads the native masker spreads a chunk over, in this process; one until set. `runDataJobs` sets it in each job's process from `maskingThreads`, so call it only when using `MaskingPlan` directly. Results are the same for any `n`. |
| `resolveStrategy(name)` | A built-in strategy, or your own named `module.path:ClassName`; see [your own strategies](strategies.md#your-own-strategies). |
| `LOCALES` | The fake-data locales, as name → `Locale`, from `bauta.masking.fakeData`, which holds every list the `fake*` strategies pick from. |
| `keyFingerprint(key)` | The same fingerprint, for a key on its own. |
| `buildMaskingManifest(outcomes, declared)` | The manifest from outcomes and each masked job's declared target and fingerprint. `RunResult.maskingManifest` wraps it. |
| `sealManifest(manifest, signingKey=None)`, `verifyManifest(manifest, signingKey=None)` | Add a manifest's digest (and signature), and check them. `verifyManifest` returns `digestValid`, `signed`, `signatureValid` and the signing key's fingerprint, and raises `ValueError` for a manifest with no integrity section. See [sealing and verifying](../guides/keep-a-manifest.md#sealing-and-verifying). |
| `auditJobs(jobs, returnedColumns=None, encryption=None, unreachable=None, targetColumns=None, foreignKeys=None, declaredForeignKeys=None, rules=...)`, `renderAudit(report)` | The `audit` report as a dict, and as text. The optional arguments carry what `audit --connect` learns from the databases: `foreignKeys` maps a target alias to its keys and its sources', `declaredForeignKeys` to the target's own. |
| `bauta.review.coverage.coverageReport(alias, tables, jobs, acknowledged=None, columns=None, rules=...)`, `renderCoverage(report)` | What the jobs do with each of `tables`: `masked`, `copied`, `acknowledged` or `uncovered`, with a `summary` of each. `acknowledged` maps a table left out on purpose to why; `columns` maps a table to its columns, so an uncovered one can say which look like personal data. A table counts as covered when a job reading `alias` names it in its `sourceQuery`. |
| `verifyReferences(database, alias, loaded, sourceKeys=())` | A `ReferenceResult` per foreign key on a table the jobs load: the key as the target spells it, whether the target declares it, and its orphaned rows, or the `problem` that stopped it being counted. `loaded` maps each table's upper-cased name to its `targetTableFinal`. From `bauta.review.references`, with `renderReferences(results)`. |
| `Database.getForeignKeys(schema=None)` | Every foreign key on a table in `schema`, or in the connection's current schema, as `ForeignKey(table, columns, referencedTable, referencedColumns, name)`. A table is named as `listTables(schema)` names it: bare in the current schema, `schema.table` otherwise. |
| `Database.getForeignKeysFor(tables)` | The foreign keys of every schema `tables` are in, a bare name being in the current schema. |
| `Database.foreignKeysElsewhere(schemas)` | The other schemas that declare foreign keys, and how many each: where to look when `schemas` held none. |
| `Database.listTables(schema=None)` | Every base table in the connection's schema, or in `schema`. Views and whatever the server ships are left out. Sorted, and each name in the spelling that reads back as the same table. |
| `Database.sample(query, rows)` | Column names and at most `rows` rows, without reading the rest. |
| `Database.isEncrypted()` | Whether the server reports the connection as encrypted; `None` if it can't say. |
| `proposeTable(database, table, sampleSize=1000, rules=..., maskKeys=False)` | A `TableProposal` with a suggested policy and the reason for it, per column. `maskKeys` proposes `key` for numeric surrogate keys as well as text ones, in the domain each foreign key shares. |
| `discoveryRules(rulesFile=None)` | The rules `proposeTable`, `auditJobs` and `synthesizeTable` take as `rules`: the built-in ones, with a validated `discovery.yaml` (`Configuration.validateDiscoveryRules(raw)`) ahead of them. They use the built-in ones alone by default. |
| `Database.getColumnDefinitions(table)`, `getPrimaryColumnNames(table)`, `tableExists(table)` | The catalog facts `schema` and upserts use. `table` may be `schema.table`; otherwise the connection's current schema is searched, and no other. |
| `bauta.generate.subset.relatedTables(foreignKeys, roots, followChildren=True)` | Every table a subset from `roots` would copy. |
| `bauta.generate.schema`: `readTable`, `createStatements`, `renderScript` | A table's shape, CREATE TABLE statements for a target dialect, and the script form. |
| `bauta.generate.schema.clearTables(database, tables)` | Empties tables children-first, in one transaction. |
| `bauta.generate.subset.draftSubset(database, root, where, followChildren=True, ignore=(), mask=False, sampleSize=1000, rules=None, maskKeys=False, count=False)` | What `bauta subset` drafts, from an open `Database`: a `SubsetDraft` with the `plan`, a `JobDraft` per table (with a masking proposal when `mask`) for `bauta.generate.discovery.renderJobs`, and with `count` each table's `rowCounts`, counted by the database. |
| `bauta.generate.schema.createStageTable(database, table, suffix='_stage')` | A stage table for `table`, made in the same database from `table`'s own definition, as `bauta schema --stage-suffix` makes one with the target as its own source. Returns its name and whether it was created; one already there is left as it is. |
| `bauta.generate.subset.subsetCountQuery(query)` | A subset query's rows as `SELECT COUNT(*)`, under the same `WITH`, which every database runs, SQL Server included. |
| `planSubset(foreignKeys, root, where, followChildren=True, ignore=(), materialize=False, quote=None, quoteTable=None)` | A `SubsetPlan`: tables in load order, a query for each, each table's parents, and the foreign keys ignored. Raises `SubsetError` on a cycle, or on a chain deeper than 16 tables. Pass `materialize=database.dialect.supportsMaterializedSelections()`, and (from `bauta.database.dialects`) `quote=lambda name: quoteIdentifier(database.type, name)` and `quoteTable=lambda name: quoteTableName(database.type, name)`, so reserved-word columns and tables work. |
| `synthesizeTable(database, table, rows, seed=0, rules=...)`, `planTable(...)` | Fill a table with generated rows, returning how many; or just describe how, with a row generator. Raise `SynthesisError`. |


## Streaming directly

```python
from bauta import Database

with Database(connectionSettings=connections['app']) as database:
    columns, chunks = database.stream('select * from orders', chunkSize=5000)
    for chunk in chunks:
        ...
```

A SQLite or DuckDB file must already exist: `Database` refuses one that doesn't, rather than letting the driver create an empty database. Pass `create=True` where creating it is the point, as `schema --apply` and a job's target do: `Database(connectionSettings=..., create=True)`.

`stream` returns the column names and a `RowStream`: an iterator of row lists, using each dialect's non-buffering cursor. It closes itself when exhausted; one you stop reading early, close with `chunks.close()` or a `with chunks:` block. Closing the `Database` closes any stream still open.

To bind parameters, pass `parameters=` and use the dialect's own placeholder. On the `%s` dialects — mysql, postgresql and mssql — a literal `%` in a query that binds parameters must then be written `%%`.

On MySQL and MariaDB, an open stream holds its connection: run nothing else on that `Database` until the iterator is finished.

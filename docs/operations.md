# Operating it

Running `bauta` unattended: where its state lives, and how to know what it did. For the reasoning behind single runs, retries and stopping, see [design.md](design.md).

- [Run state](#run-state)
- [Run history](#run-history)
- [After a failed cycle](#after-a-failed-cycle)
- [One production, several environments](#one-production-several-environments)
- [Rotating the masking key](#rotating-the-masking-key)
- [Masking manifest](#masking-manifest)
- [Tables](#tables)
- [Throughput](#throughput)
- [Environment variables](#environment-variables)
- [Notifications](#notifications)
- [A deployment, put together](#a-deployment-put-together)


## Run state

`run` keeps each job's last run time, watermark and masking-key fingerprint between invocations. Losing it doesn't break anything, but it costs: every incremental job re-extracts from `watermarkInitial`, and `refresh` windows start over.

| Where | Set with | Use when |
| --- | --- | --- |
| A file named by `jobs.yaml`'s `memory`, relative to it; `memory.yaml` beside it without one | (default) | The filesystem persists between runs. |
| A table | `memory: {connection: ALIAS}` in `jobs.yaml` | Nothing persists: containers without a volume, several machines. |
| Another file, or a table, for one run | `--memory FILE`, `--memory-connection ALIAS` | It should live somewhere else this time, such as a mounted volume. |

Watermarks in a table are stored as text with a type beside them, and read back as the same type: dates, timestamps, times, integers, floats, decimals, and bytes such as SQL Server's `rowversion`. See [tables](#tables) for its definition.

**Overlapping runs.** `run` holds a lock beside the memory file (`memory.yaml.run.lock`) for as long as it runs, and a second run that finds it held exits with status 1. With run state in a table, the lock is `memory.run.lock` where the memory file would have been, so it only separates runs on one machine. Across machines, let the scheduler do it: a Kubernetes CronJob with `concurrencyPolicy: Forbid`, or Airflow's `max_active_runs=1`.


## Run history

With `history` set in `jobs.yaml`, `run` records one entry per job after each cycle: a JSON line in a file, or a row in a table. Nothing reads it to decide what to run; it's for answering "what happened last night".

```yaml
history: ../transaction/history.jsonl    # a file, relative to jobs.yaml
history:
  connection: warehouse                  # or a table
```

```
bauta history
bauta history --job loadOrders --limit 5
bauta history --format json
```

```
FINISHED             JOB                          STATUS           ROWS   SECONDS  ERROR
2026-09-16 02:00:14  loadInvoices                 skipped             0       0.0  predecessor(s) did not complete: loadCustomers
2026-09-16 02:00:13  loadCustomers                failed              0       1.0  OperationalError: timeout
2026-09-16 02:00:12  loadOrders                   completed        4200      12.5
```

`--history FILE` or `--history-connection ALIAS` overrides the setting, on `run` and on `history`.

Each record has `runId` (shared by the jobs of one cycle), `job`, `status`, `rowCount`, `attempts`, `startedAt`, `finishedAt`, `durationSeconds` and `error`, cut to 2000 characters. A file grows by one line per job per run, so rotate it with `logrotate` or similar.

**Alerting on staleness.** In a table, history answers the question worth alerting on, whether a job has stopped completing, from any dashboard or monitor that runs SQL. Alert on this rather than on one failure, which the next run's retry may already have fixed:

```sql
SELECT job, max(finished_at) AS last_success
FROM bauta_history
WHERE status = 'completed'
GROUP BY job
HAVING max(finished_at) < <now, in seconds since 1970> - 3 * 3600
```


## After a failed cycle

A job that fails doesn't take its dependents with it: they are skipped, so nothing loads rows whose parents are missing, and every skip is recorded with its cause.

```
Cycle finished: 14 completed, 1 failed, 1 skipped, 1374 row(s) moved
```

**`refresh` is the resume mechanism**, and the reason a plain `bauta run` is the right thing to do next. A job that completed is inside its `refresh` window and is skipped; the job that failed is not, so it runs again, and its dependents follow once it succeeds. Nothing re-copies what already arrived.

| What you want | Command |
| --- | --- |
| Retry what failed, leave the rest | `bauta run` |
| Re-run everything, ignoring `refresh` | `bauta run --force` |
| Re-run one job and nothing else | `bauta run --job NAME` (its predecessors don't run; `run` warns about each) |

Without `refresh` set, a plain `run` re-runs every job, which is correct but does more work than it needs to. `bauta history` shows what happened, and a failed job's error is recorded with it.


## One production, several environments

The same `jobs.yaml` can fill dev, staging and UAT from one production database. Two ways, which combine:

**A database file per environment.** `--connections` names it, so the jobs never change:

```
bauta run --connections environments/staging.yaml
bauta run --connections environments/uat.yaml
```

**An alias from the environment.** `${NAME}` expands anywhere in `jobs.yaml`, including in `targetConnection`, and the alias is checked offline:

```yaml
defaults:
  sourceConnection: prod
  targetConnection: ${TARGET_ALIAS:-staging}
```

```
$ TARGET_ALIAS=nosuchalias bauta validate
Invalid job graph:
maskCountries: targetConnection "nosuchalias" is not a known connection alias
```

Give each environment its own masking key, and its copies cannot be joined to each other's — which is usually what you want, since a UAT copy and a staging copy of the same customer should not be recognisably the same person. Give them the same key where a tester needs to follow a record across environments. Either way, [`requireMasking`](configuration.md#requiring-masking) on each target says that none of them can ever receive unmasked rows.


## Rotating the masking key

The masking key is a credential, so a security policy usually says to change it on a schedule. Changing it changes every mask, so what a run does next depends on how the copy is loaded.

`run` refuses to start when the key of an **upsert** job changed since that job last completed, because the target still holds rows masked under the old key. A **swap** job replaces its whole target every run, so it is never affected and needs nothing here.

| The job's policy | What to do |
| --- | --- |
| Does not mask the target's primary key | `bauta run --accept-key-change`. Each row is matched on its unchanged key and rewritten under the new one. |
| Masks the target's primary key | `bauta clear`, then `bauta run --force`. |

**The second case cannot be acknowledged away, and `run` refuses it whatever flags are given.** An upsert matches rows on the primary key. When the key is masked, a new masking key gives every row a new primary key, so the run inserts a second generation of rows beside the first rather than updating it — and where a new key lands on one already there, it overwrites a different row's data. The result is a target holding two key generations at once, whose foreign keys still all resolve, so [`bauta verify-references`](masking.md#verify-references-checking-the-copys-references) reports it clean.

`bauta clear` empties the targets children-first and forgets the recorded key, so the next `run` starts from nothing:

```
export MASKING_KEY=<the new key>
bauta clear --yes           # empties the jobs' targets, children first
bauta run --force           # reloads everything under the new key
```

Use `--force` on that run: `clear` leaves the targets empty, and a job still inside its `refresh` window would otherwise be skipped and leave them that way. Run the jobs together rather than one at a time, so that columns sharing a domain are reloaded under the same key and still join.

Rotating the key does not change [`BAUTA_MANIFEST_KEY`](masking.md#the-manifest), which signs manifests. Manifests written under the old masking key stay verifiable, and record the old key's fingerprint.


## Masking manifest

With `manifest` set in `jobs.yaml`, each run writes a sealed record of what was masked, how, and under which key fingerprint: a file, replaced by each run, or a table, which keeps every run's. It's what an auditor asks for.

```yaml
manifest: ../transaction/manifest.json   # a file, relative to jobs.yaml
manifest:
  connection: warehouse                  # or a table
```

```
bauta verify-manifest                    # the latest; exit 0 intact, 1 altered
bauta verify-manifest --run RUN_ID       # an earlier one, from a table
```

`--manifest FILE` or `--manifest-connection ALIAS` overrides the setting, on `run` and on `verify-manifest`. Set `BAUTA_MANIFEST_KEY` to sign manifests; only a signature shows who wrote one. [The manifest](masking.md#the-manifest) describes its content and how verification works.


## Tables

Run state, history and manifests each need their table to exist before a run uses it. These definitions are `DATABASE_MEMORY_SCHEMA`, `DATABASE_HISTORY_SCHEMA` and `DATABASE_MANIFEST_SCHEMA` in the package, and their types work on all seven databases. Run state can't be kept in DuckDB, which lets one process at a time open a file; history and manifests can. See [DuckDB](configuration.md#duckdb). Times are seconds since 1970.

```sql
CREATE TABLE bauta_memory (
    job VARCHAR(255) PRIMARY KEY,
    last_run DOUBLE PRECISION,
    watermark_value VARCHAR(255),
    watermark_type VARCHAR(32)
    )

CREATE TABLE bauta_history (
    run_id VARCHAR(36) NOT NULL,
    job VARCHAR(255) NOT NULL,
    status VARCHAR(16) NOT NULL,
    row_count NUMERIC(19),
    attempts INT,
    started_at DOUBLE PRECISION,
    finished_at DOUBLE PRECISION,
    error VARCHAR(2000),
    PRIMARY KEY (run_id, job)
    )

CREATE TABLE bauta_manifest (
    run_id VARCHAR(36) NOT NULL,
    part INT NOT NULL,
    written_at DOUBLE PRECISION NOT NULL,
    content VARCHAR(2000) NOT NULL,
    PRIMARY KEY (run_id, part)
    )
```

A manifest is stored in pieces of `content`, in `part` order, because its JSON can be longer than any one text type every database shares. See [manifests in a table](masking.md#in-a-table).


## Throughput

Three things decide how fast a job moves rows, in this order.

**The masking policy**, by about fivefold. `key` is expensive because it must be a permutation; `hash` hides as much for a thirtieth of the work wherever a column needn't stay one-to-one. See [speed](masking.md#speed).

**The [native masker](masking.md#the-native-masker)**, about ten times faster on the same policy, with identical results: a million rows of six masked columns, two of them `key`, take under 8 seconds with it and 74 without. See [speed](masking.md#speed) for the conditions. On a wide table, where masking rather than the database sets the pace, it can also spread each chunk over several cores ([`maskingThreads`](masking.md#masking-threads)): 25 masked columns went from 30,000 rows a second on one thread to 79,000 on ten.

**`chunkSize` — for latency, not throughput.** On a local database, chunks from 500 rows to 200,000 finish the same job in 8.6 to 9.2 seconds. What a chunk costs is a round trip: against a database 25 ms away, a million rows take 123 seconds at `chunkSize: 500` and 8.8 at `10000`. Latency stops mattering once a chunk's masking outlasts its round trips:

```
chunkSize  >  2 x latency / per-row masking cost
```

— a few thousand rows at 5 ms, about six thousand at 25 ms. Cheap policies need *larger* chunks, having less work to hide the wait behind. With the native masker, reading, masking and writing also overlap; a job then holds three chunks (ten million rows held 80 MB).

If a job is still slow, look at the database: the target's indexes and constraints during a bulk load, and a stage table (`targetTableStage`) so the final table is written once.


## Environment variables

| Variable | Effect |
| --- | --- |
| `BAUTA_CONFIG` | The configuration directory, when `--config` isn't given. |
| `BAUTA_NOTIFY_URL` | The webhook, when `--notify-url` isn't given. |
| `BAUTA_MANIFEST_KEY` | Sign manifests, and verify their signatures. |
| `BAUTA_MASKING_THREADS` | Threads the native masker uses per job: a number or `auto`. Overrides `jobs.yaml`'s `maskingThreads`; see [masking threads](masking.md#masking-threads). |
| `BAUTA_NATIVE=0` | Mask in Python even where the extension is installed. |
| `BAUTA_PIPELINE=0` or `=1` | Force reading, masking and writing to take turns, or to overlap. |

The last two are for diagnosis. The implementations are tested to agree, so a difference `BAUTA_NATIVE=0` reveals is a bug worth reporting. By default the stages overlap only with the native masker; overlapping pure-Python masking costs about 2%.


## Notifications

`--notify-url URL`, or `$BAUTA_NOTIFY_URL`, posts JSON to a webhook when a cycle doesn't succeed: a job failed or was skipped, or a signal stopped the run. `--notify-on always` posts after every cycle.

```json
{
  "text": "bauta on etl-7d9f: failed -- 1 completed, 1 failed, 1 skipped, 4200 row(s)\n- loadCustomers failed: OperationalError: timeout\n- loadInvoices skipped: predecessor(s) did not complete: loadCustomers",
  "status": "failed",
  "host": "etl-7d9f",
  "summary": {"completed": 1, "failed": 1, "skipped": 1, "rows": 4200},
  "jobs": [{"job": "loadOrders", "status": "completed", "rowCount": 4200, "durationSeconds": 12.5, "error": null}, "..."]
}
```

Slack, Mattermost and Microsoft Teams incoming webhooks show `text` as it is; anything else can read the rest. The URL usually carries a token, so prefer the environment variable to the flag. Error messages come from the database drivers. Values they quote are replaced with `<redacted>` for every message format the tests know, but not every format a driver can write (see [the security model](security.md#where-unmasked-data-goes)); keep that in mind when choosing the channel.

History and notifications never affect a run's outcome. If one fails, the failure is logged and the run carries on.


## A deployment, put together

In `/etc/etl/jobs.yaml`, beside the jobs:

```yaml
memory: /var/lib/etl/memory.yaml
history:
  connection: warehouse
manifest:
  connection: warehouse
```

```cron
*/15 * * * *  bauta run --config /etc/etl --log-format json --log /var/log/etl/etl.log --quiet
```

With `BAUTA_NOTIFY_URL` set in the environment, a failed run also posts to the team's channel. `bauta history --config /etc/etl` answers what happened overnight, and `bauta verify-manifest --config /etc/etl` checks the latest manifest.

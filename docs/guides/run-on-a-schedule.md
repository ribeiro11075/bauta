# Run on a schedule


Running `bauta` unattended: where its state lives, and how to know what it did. For the reasoning behind single runs, retries and stopping, see [design.md](../concepts/how-it-works.md).


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


## Run state

`run` keeps each job's last run time, watermark and masking-key fingerprint between invocations. Losing it doesn't break anything, but it costs: every incremental job re-extracts from `watermarkInitial`, and `refresh` windows start over.

| Where | Set with | Use when |
| --- | --- | --- |
| A file named by `jobs.yaml`'s `memory`, relative to it; `memory.yaml` beside it without one | (default) | The filesystem persists between runs. |
| A table | `memory: {connection: ALIAS}` in `jobs.yaml` | Nothing persists: containers without a volume, several machines. |
| Another file, or a table, for one run | `--memory FILE`, `--memory-connection ALIAS` | It should live somewhere else this time, such as a mounted volume. |

Watermarks in a table are stored as text with a type beside them, and read back as the same type: dates, timestamps, times, integers, floats, decimals, and bytes such as SQL Server's `rowversion`. See [tables](../reference/tables.md) for its definition.

**Overlapping runs.** `run` holds a lock beside the memory file (`memory.yaml.run.lock`) for as long as it runs, and a second run that finds it held exits with status 1. With run state in a table, the lock is `memory.run.lock` where the memory file would have been, so it only separates runs on one machine. Across machines, let the scheduler do it: a Kubernetes CronJob with `concurrencyPolicy: Forbid`, or Airflow's `max_active_runs=1`.


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

Give each environment its own masking key, and its copies cannot be joined to each other's — which is usually what you want, since a UAT copy and a staging copy of the same customer should not be recognisably the same person. Give them the same key where a tester needs to follow a record across environments. Either way, [`requireMasking`](../reference/connections.md#requiring-masking) on each target says that none of them can ever receive unmasked rows.

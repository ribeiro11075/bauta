# Run on a schedule


Running `bauta` unattended: where its state lives, and what to do after a failure. For why it runs once and exits, see [how it works](../concepts/how-it-works.md).


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
0 3 * * 0     bauta run --config /etc/etl --full-refresh --log-format json --log /var/log/etl/etl.log --quiet
```

The second line is a weekly [full refresh](../concepts/how-it-works.md#deletes), so rows deleted in production leave the copy within a week; a 15-minute run starting during it exits with status 1 rather than overlap. With `BAUTA_NOTIFY_URL` set, a failed run posts to the team's channel; `bauta history` says what happened overnight, and `bauta verify-manifest` checks the latest manifest.


## Run state

`run` keeps each job's last run time, watermark and key fingerprint between invocations. Losing it breaks nothing, but every incremental job re-extracts from `watermarkInitial` and `refresh` windows start over.

| Where | Set with | Use when |
| --- | --- | --- |
| A file named by `jobs.yaml`'s `memory`, relative to it; `memory.yaml` beside it without one | (default) | The filesystem persists between runs. |
| A table | `memory: {connection: ALIAS}` in `jobs.yaml` | Nothing persists: containers without a volume, several machines. |
| Another file, or a table, for one run | `--memory FILE`, `--memory-connection ALIAS` | It should live somewhere else this time, such as a mounted volume. |

In a table, a watermark is stored as text with its type, and read back as that type, bytes such as SQL Server's `rowversion` included. See [tables](../reference/tables.md) for the definition.

**Overlapping runs.** `run` holds a lock beside the memory file (`memory.yaml.run.lock`) while it runs, and a second run finding it held exits with status 1. A `run --job` locks only its own jobs, so runs of different jobs go side by side, as an [orchestrator's task per job](run-from-an-orchestrator.md) does, while the same job never runs twice at once. With run state in a table, the lock is a local file, so it only separates runs on one machine; across machines, let the scheduler do it (a CronJob's `concurrencyPolicy: Forbid`, Airflow's `max_active_runs=1`).


## After a failed cycle

A failed job's dependents are skipped, so nothing loads rows whose parents are missing, and each skip is recorded with its cause.

```
Cycle finished: 14 completed, 1 failed, 1 skipped, 1374 row(s) moved
```

**`refresh` is the resume mechanism**, so a plain `bauta run` is the right next step: completed jobs are inside their window and skipped, while the failed one runs again and its dependents follow.

| What you want | Command |
| --- | --- |
| Retry what failed, leave the rest | `bauta run` |
| Re-run everything, ignoring `refresh` | `bauta run --force` |
| Re-run one job and nothing else | `bauta run --job NAME` (its predecessors don't run; `run` warns about each) |

Without `refresh`, a plain `run` reruns every job: correct, but more work than needed.


## One production, several environments

One `jobs.yaml` can fill dev, staging and UAT from one production, two ways that combine.

**A connections file per environment**, named by `--connections`, so the jobs never change:

```
bauta run --connections environments/staging.yaml
bauta run --connections environments/uat.yaml
```

**An alias from the environment**: `${NAME}` expands anywhere in `jobs.yaml`, `targetConnection` included, and the alias is checked offline:

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

Give each environment its own masking key, so their copies can't be joined to each other's, unless a tester needs to follow a record across them. Either way, [`requireMasking`](../reference/connections.md#requiring-masking) on each target says none can ever receive unmasked rows.

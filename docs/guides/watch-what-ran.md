# Watch what ran


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
FINISHED (UTC)       JOB                          STATUS           ROWS   SECONDS    READ    MASK   WRITE  WAITED  ERROR
2026-09-16 02:00:14  loadInvoices                 skipped             0       0.0       -       -       -       -  predecessor(s) did not complete: loadCustomers
2026-09-16 02:00:13  loadCustomers                failed              0       1.0       -       -       -       -  OperationalError: timeout
2026-09-16 02:00:12  loadOrders                   completed        4200      12.5     2.1     1.4    10.3     0.0
```

`--history FILE` or `--history-connection ALIAS` overrides the setting, on `run` and on `history`.

Each record has `runId` (shared by the jobs of one cycle), `job`, `status`, `rowCount`, `attempts`, `startedAt`, `finishedAt`, `durationSeconds` and `error`, cut to 2000 characters, and for a job that completed, `readSeconds`, `maskSeconds`, `writeSeconds` and `throttledSeconds` (see [where the time went](#where-the-time-went)). A file grows by one line per job per run, so rotate it with `logrotate` or similar.

### Where the time went

`READ`, `MASK` and `WRITE` are the seconds a completed job spent busy at each stage:

- **read:** running the query and fetching rows from the source, and nothing else.
- **mask:** transforming and masking them.
- **write:** loading them into the target, and whatever it takes to finish: a swap, an upsert from the stage table, a file published or an Iceberg commit.
- **waited** (`throttledSeconds`): held back by the source's [read limit](make-it-faster.md#limiting-what-a-job-reads).

They are busy time: with the native masker the stages overlap, so they add up to more than `SECONDS`, and **the largest sets the job's pace**. In the example `loadOrders` waited on its target, so a faster masker would change nothing. Partitions add theirs together. A history table from 0.2.4 or earlier records them once it has [their columns](../reference/tables.md). The same four appear as `stages` in [JSON logs](../concepts/how-it-works.md#structured-logs) and notifications, and [`bauta bench`](make-it-faster.md#measure-a-job) measures them without writing.

**Alerting on staleness.** History in a table answers the question worth alerting on, whether a job has stopped completing, from any monitor that runs SQL. Alert on that rather than one failure, which a retry may already have fixed:

```sql
SELECT job, max(finished_at) AS last_success
FROM bauta_history
WHERE status = 'completed'
GROUP BY job
HAVING max(finished_at) < <now, in seconds since 1970> - 3 * 3600
```


## What's running now

`bauta status` shows each run in progress and where each of its jobs stands: waiting for a predecessor or a free worker, running since when, or completed, failed or skipped, with its rows or its error.

```
bauta status
bauta status --format json
```

```
Run 0b6c1f0e-4d1e-4f7a-9a43-2f0c9f6a1d52, started 2026-10-16 02:00:03 UTC: running, process 48121
  1 completed, 1 running, 1 waiting
  JOB                          STATUS     STARTED (UTC)              ROWS  ERROR
  loadCustomers                completed  2026-10-16 02:00:03       12000
  loadOrders                   running    2026-10-16 02:00:41           -
  loadInvoices                 waiting    -                             -
```

While a cycle runs, `run` keeps a small JSON file for it beside its run-state lock (in `memory.yaml.running/`), rewritten as each job starts or is decided and removed when the cycle ends. It holds what the table shows, never a value from a table. A file left by a run whose process is gone is shown as `ended without finishing`. With run state in a table, `status` sees runs started on its own machine.

A cycle's run id is the same here, in history and in a manifest table. From Python, `Project.running()` reads the same files, and `runDataJobs(onProgress=...)` is told each change ([the library](../reference/python-api.md#a-configuration-directory)).


## Notifications

`--notify-url URL`, or `$BAUTA_NOTIFY_URL`, posts JSON to a webhook when a cycle doesn't succeed: a job failed or was skipped, or a signal stopped the run. `--notify-on always` posts after every cycle.

```json
{
  "text": "bauta on etl-7d9f: failed -- 1 completed, 1 failed, 1 skipped, 4200 row(s)\n- loadCustomers failed: OperationalError: timeout\n- loadInvoices skipped: predecessor(s) did not complete: loadCustomers",
  "status": "failed",
  "host": "etl-7d9f",
  "summary": {"completed": 1, "failed": 1, "skipped": 1, "rows": 4200},
  "jobs": [{"job": "loadOrders", "status": "completed", "rowCount": 4200, "durationSeconds": 12.5, "error": null,
            "readSeconds": 2.1, "maskSeconds": 1.4, "writeSeconds": 10.3, "throttledSeconds": 0.0}, "..."]
}
```

Slack, Mattermost and Teams incoming webhooks show `text` as it is; anything else can read the rest. A post that fails on the network, times out, or gets a 5xx or 429 is sent once more, three seconds later; any other answer is logged. The URL usually carries a token, so prefer the environment variable. Error text is [scrubbed](../concepts/security.md#where-unmasked-data-goes) of the values drivers quote in every known format, not every possible one; choose the channel with that in mind.

History and notifications never affect a run's outcome: a failure is logged, and the run carries on.

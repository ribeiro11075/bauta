<!-- Generated from the parser by website/commands.py. Change the flag, then run `python website/commands.py`. -->

# `bauta run`

Run data jobs.

```
bauta run [options]
```

See [Run on a schedule](../../guides/run-on-a-schedule.md) for how to use it.

## Options

| Flag | Default | Effect |
| --- | --- | --- |
| `--jobs JOBS` |  | Explicit path to the jobs file, overriding --config. |
| `--memory MEMORY` | jobs.yaml's `memory`, else memory.yaml beside jobs.yaml | Path to the run-memory file. |
| `--memory-connection ALIAS` |  | Keep run memory in a table of this connection instead of a file (see docs/guides/run-on-a-schedule.md) |
| `--memory-table MEMORY_TABLE` | jobs.yaml's, else bauta_memory | The run-memory table. |
| `--forever` | off | Keep running, honoring each job's refresh window. Prefer a single run from cron or a CronJob; use this only for freshness below cron's one-minute floor, or where there is no scheduler. |
| `--job JOB` |  | Run only this job (repeatable). Implies --force, and does NOT run its predecessors. |
| `--force` | off | Ignore refresh windows. |
| `--workers WORKERS` |  | Override the worker count from configuration. |
| `--dry-run` | off | Check connections, target tables, primary keys and masking coverage without moving any rows. |
| `--manifest FILE` | jobs.yaml's `manifest` | Write a JSON record of what was masked, how, and under which key fingerprint. |
| `--manifest-connection ALIAS` |  | The masking manifest in a table of this connection. |
| `--manifest-table MANIFEST_TABLE` | jobs.yaml's, else bauta_manifest | The manifest table. |
| `--manifest-key-variable MANIFEST_KEY_VARIABLE` | `BAUTA_MANIFEST_KEY` | Environment variable holding the manifest signing key. |
| `--accept-key-change` | off | Run upsert jobs even though their masking key changed since their last run. |
| `--history FILE` | jobs.yaml's `history` | Run history as JSON lines. |
| `--history-connection ALIAS` |  | Run history in a table of this connection. |
| `--history-table HISTORY_TABLE` | jobs.yaml's, else bauta_history | The history table. |
| `--notify-url URL` | $BAUTA_NOTIFY_URL | Post a JSON summary to this webhook when a cycle does not succeed. |
| `--notify-on {failure,always}` | `failure` | One of `failure`, `always`. |

It also takes the [common flags](index.md#common-flags): `--config`, `--connections`, `--log`, `--log-format`, `--log-level`, `--quiet`.

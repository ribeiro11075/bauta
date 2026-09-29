<!-- Generated from the parser by website/commands.py. Change the flag, then run `python website/commands.py`. -->

# `bauta history`

Show recent job outcomes recorded with run --history.

```
bauta history [options]
```

See [Watch what ran](../../guides/watch-what-ran.md#run-history) for how to use it.

## Options

| Flag | Default | Effect |
| --- | --- | --- |
| `--jobs JOBS` |  | Explicit path to the jobs file, overriding --config. |
| `--history FILE` | jobs.yaml's `history` | Run history as JSON lines. |
| `--history-connection ALIAS` |  | Run history in a table of this connection. |
| `--history-table HISTORY_TABLE` | jobs.yaml's, else bauta_history | The history table. |
| `--job JOB` |  | Only this job. |
| `--limit LIMIT` | `20` | How many records. |
| `--format {text,json}` | `text` | One of `text`, `json`. |

It also takes the [common flags](index.md#common-flags): `--config`, `--connections`, `--log`, `--log-format`, `--log-level`, `--quiet`.

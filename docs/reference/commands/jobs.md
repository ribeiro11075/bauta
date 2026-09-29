<!-- Generated from the parser by website/commands.py. Change the flag, then run `python website/commands.py`. -->

# `bauta jobs`

Show the job graph and which jobs are due.

```
bauta jobs [options]
```

See [Run on a schedule](../../guides/run-on-a-schedule.md) for how to use it.

## Options

| Flag | Default | Effect |
| --- | --- | --- |
| `--jobs JOBS` |  | Explicit path to the jobs file, overriding --config. |
| `--memory MEMORY` | jobs.yaml's `memory`, else memory.yaml beside jobs.yaml | Path to the run-memory file. |
| `--memory-connection ALIAS` |  | Keep run memory in a table of this connection instead of a file (see docs/guides/run-on-a-schedule.md) |
| `--memory-table MEMORY_TABLE` | jobs.yaml's, else bauta_memory | The run-memory table. |

It also takes the [common flags](index.md#common-flags): `--config`, `--connections`, `--log`, `--log-format`, `--log-level`, `--quiet`.

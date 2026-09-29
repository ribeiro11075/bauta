<!-- Generated from the parser by website/commands.py. Change the flag, then run `python website/commands.py`. -->

# `bauta clear`

Empty the target tables of data jobs, children first (destructive)

```
bauta clear [options]
```

See [Copy a subset](../../guides/copy-a-subset.md#clear-emptying-the-copy-before-a-refresh) for how to use it.

## Options

| Flag | Default | Effect |
| --- | --- | --- |
| `--jobs JOBS` |  | Explicit path to the jobs file, overriding --config. |
| `--memory MEMORY` | jobs.yaml's `memory`, else memory.yaml beside jobs.yaml | Path to the run-memory file. |
| `--memory-connection ALIAS` |  | Keep run memory in a table of this connection instead of a file (see docs/guides/run-on-a-schedule.md) |
| `--memory-table MEMORY_TABLE` | jobs.yaml's, else bauta_memory | The run-memory table. |
| `--job JOB` |  | Clear only this job's target (repeatable) |
| `--dry-run` | off | Show which tables would be emptied, and in what order. |
| `--yes` | off | Actually delete the rows. |

It also takes the [common flags](index.md#common-flags): `--config`, `--connections`, `--log`, `--log-format`, `--log-level`, `--quiet`.

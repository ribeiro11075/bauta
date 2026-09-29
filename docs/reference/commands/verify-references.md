<!-- Generated from the parser by website/commands.py. Change the flag, then run `python website/commands.py`. -->

# `bauta verify-references`

Count rows in each target whose foreign key points at nothing.

```
bauta verify-references [options]
```

See [Copy a subset](../../guides/copy-a-subset.md#verify-references-checking-the-copys-references) for how to use it.

## Options

| Flag | Default | Effect |
| --- | --- | --- |
| `--jobs JOBS` |  | Explicit path to the jobs file, overriding --config. |
| `--job JOB` |  | Check only this job's target table (repeatable) |
| `--format {text,json}` | `text` | One of `text`, `json`. |
| `--output OUTPUT` |  | Write the report here instead of stdout; must not already exist. |

It also takes the [common flags](index.md#common-flags): `--config`, `--connections`, `--log`, `--log-format`, `--log-level`, `--quiet`.

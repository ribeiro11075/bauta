<!-- Generated from the parser by website/commands.py. Change the flag, then run `python website/commands.py`. -->

# `bauta bench`

Measure how fast each job reads and masks from its real source, writing nothing.

```
bauta bench [options]
```

See [Make it faster](../../guides/make-it-faster.md#measure-a-job) for how to use it.

## Options

| Flag | Default | Effect |
| --- | --- | --- |
| `--jobs JOBS` |  | Explicit path to the jobs file, overriding --config. |
| `--job JOB` |  | Measure only this job (repeatable; default: every active job) |
| `--rows ROWS` | `100000` | Rows to read from each job's query. |
| `--format {text,json}` | `text` | One of `text`, `json`. |
| `--output OUTPUT` |  | Write the report here instead of stdout; must not already exist. |

It also takes the [common flags](index.md#common-flags): `--config`, `--connections`, `--log`, `--log-format`, `--log-level`, `--quiet`.

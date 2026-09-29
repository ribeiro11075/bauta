<!-- Generated from the parser by website/commands.py. Change the flag, then run `python website/commands.py`. -->

# `bauta coverage`

List a source database's tables and what the jobs do with each.

```
bauta coverage [options]
```

See [Prove the copy is safe](../../guides/prove-the-copy-is-safe.md#coverage-what-the-jobs-do-not-cover) for how to use it.

## Options

| Flag | Default | Effect |
| --- | --- | --- |
| `--jobs JOBS` |  | Explicit path to the jobs file, overriding --config. |
| `--rules FILE` | discovery.yaml in the configuration directory, if there is one | Your own rules for recognising personal data, ahead of the built-in ones. |
| `--connection CONNECTION` |  | The source connection alias to check; required when the jobs read from more than one. |
| `--schema SCHEMA` |  | The schema to list, instead of the connection's own. |
| `--job JOB` |  | Only these jobs count as covering a table. Repeatable. |
| `--format {text,json}` | `text` | Output format. |
| `--output OUTPUT` |  | Write to this file instead of stdout. |

It also takes the [common flags](index.md#common-flags): `--config`, `--connections`, `--log`, `--log-format`, `--log-level`, `--quiet`.

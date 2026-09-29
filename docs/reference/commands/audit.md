<!-- Generated from the parser by website/commands.py. Change the flag, then run `python website/commands.py`. -->

# `bauta audit`

Report what each job does with data, and what a reviewer should question.

```
bauta audit [options]
```

See [Prove the copy is safe](../../guides/prove-the-copy-is-safe.md#reviewing-policies-audit) for how to use it.

## Options

| Flag | Default | Effect |
| --- | --- | --- |
| `--jobs JOBS` |  | Explicit path to the jobs file, overriding --config. |
| `--connect` | off | Also run each masked query for its real columns, and check whether each connection is encrypted. |
| `--job JOB` |  | Audit only this job (repeatable) |
| `--format {text,json}` | `text` | One of `text`, `json`. |
| `--strict` | off | Exit 1 on warnings as well as errors. |
| `--output OUTPUT` |  | Write the report here instead of stdout; must not already exist. |
| `--rules FILE` | discovery.yaml in the configuration directory, if there is one | Your own rules for recognising personal data, ahead of the built-in ones. |

It also takes the [common flags](index.md#common-flags): `--config`, `--connections`, `--log`, `--log-format`, `--log-level`, `--quiet`.

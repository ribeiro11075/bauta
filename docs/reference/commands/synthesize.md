<!-- Generated from the parser by website/commands.py. Change the flag, then run `python website/commands.py`. -->

# `bauta synthesize`

Fill existing tables with generated rows, for data that can't be copied.

```
bauta synthesize --connection CONNECTION --table TABLE[:ROWS] [options]
```

See [Generate data](../../guides/generate-data.md) for how to use it.

## Options

| Flag | Default | Effect |
| --- | --- | --- |
| `--connection CONNECTION` | required | The alias whose tables to fill. |
| `--table TABLE[:ROWS]` | required | A table to fill, and how many rows (repeatable); parents are filled first. |
| `--rows ROWS` | `100` | Rows for a --table without a count. |
| `--profile ALIAS` |  | Shape the rows like the same tables in this connection: each column's share of NULLs, the range of a number or a date, and the labels of a column of few values, as often as there. Never a column named like personal data, a key, or a label fewer than 5 rows share. |
| `--seed SEED` | `0` | The same seed makes the same rows. |
| `--dry-run` | off | Show what each column gets, and sample rows, without writing. |
| `--yes` | off | Actually insert the rows. |
| `--rules FILE` | discovery.yaml in the configuration directory, if there is one | Your own rules for recognising personal data, ahead of the built-in ones. |

It also takes the [common flags](index.md#common-flags): `--config`, `--connections`, `--log`, `--log-format`, `--log-level`, `--quiet`.

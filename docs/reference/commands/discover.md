<!-- Generated from the parser by website/commands.py. Change the flag, then run `python website/commands.py`. -->

# `bauta discover`

Propose a masking policy for tables, from their schema and a sample.

```
bauta discover --connection CONNECTION [options]
```

See [Propose a policy](../../guides/propose-a-policy.md) for how to use it.

## Options

| Flag | Default | Effect |
| --- | --- | --- |
| `--connection CONNECTION` | required | The alias to read from. |
| `--table TABLE` |  | A table to propose a policy for (repeatable) |
| `--all-tables` | off | Every table in the database, instead of naming each with --table. |
| `--schema SCHEMA` |  | The schema --all-tables lists, instead of the connection's own. |
| `--target TARGET` | --database, masking in place | The alias the generated jobs load into. |
| `--sample SAMPLE` | `1000` | Rows sampled per table to classify columns. |
| `--key-variable KEY_VARIABLE` | `MASKING_KEY` | Environment variable the generated jobs read the masking key from. |
| `--chunk-size CHUNK_SIZE` | `5000` | chunkSize for the generated jobs. |
| `--mask-keys` | off | Mask numeric surrogate keys too, in the domain each foreign key shares, instead of keeping them. |
| `--output OUTPUT` |  | Write the generated jobs here instead of stdout; must not already exist. |
| `--rules FILE` | discovery.yaml in the configuration directory, if there is one | Your own rules for recognising personal data, ahead of the built-in ones. |

It also takes the [common flags](index.md#common-flags): `--config`, `--connections`, `--log`, `--log-format`, `--log-level`, `--quiet`.

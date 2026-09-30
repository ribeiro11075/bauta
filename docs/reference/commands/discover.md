<!-- Generated from the parser by website/commands.py. Change the flag, then run `python website/commands.py`. -->

# `bauta discover`

Propose a masking policy for tables, from their schema and a sample.

```
bauta discover [options]
```

See [Propose a policy](../../guides/propose-a-policy.md) for how to use it.

## Options

| Flag | Default | Effect |
| --- | --- | --- |
| `--jobs JOBS` |  | Explicit path to the jobs file, overriding --config. |
| `--connection CONNECTION` |  | The alias to read from; required unless --update. |
| `--table TABLE` |  | A table to propose a policy for (repeatable) |
| `--all-tables` | off | Every table in the database, instead of naming each with --table. |
| `--schema SCHEMA` |  | The schema --all-tables lists, instead of the connection's own. |
| `--target TARGET` | --connection, masking in place | The alias the generated jobs load into. |
| `--sample SAMPLE` | `1000` | Rows sampled per table to classify columns. |
| `--key-variable KEY_VARIABLE` | `MASKING_KEY` | Environment variable the generated jobs read the masking key from. |
| `--chunk-size CHUNK_SIZE` | `5000` | chunkSize for the generated jobs. |
| `--self-contained` | off | Write the connections, insert strategy and key into every job rather than under defaults:, for jobs to paste into a file with defaults of its own. |
| `--mask-keys` | off | Mask numeric surrogate keys too, in the domain each foreign key shares, instead of keeping them. |
| `--output OUTPUT` |  | Write the generated jobs here instead of stdout; must not already exist. |
| `--rules FILE` | discovery.yaml in the configuration directory, if there is one | Your own rules for recognising personal data, ahead of the built-in ones. |
| `--update` | off | Compare each masked job in the jobs file with what its sourceQuery returns now, and propose a policy for each new column; exits 1 if any job has drifted. |
| `--job JOB` |  | With --update, only this job (repeatable) |
| `--apply` | off | With --update, write the proposals into the file each job is defined in, and drop the columns its sourceQuery no longer returns. |

It also takes the [common flags](index.md#common-flags): `--config`, `--connections`, `--log`, `--log-format`, `--log-level`, `--quiet`.

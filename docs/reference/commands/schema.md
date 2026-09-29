<!-- Generated from the parser by website/commands.py. Change the flag, then run `python website/commands.py`. -->

# `bauta schema`

Generate or apply CREATE TABLE statements for a target, from source tables.

```
bauta schema --connection CONNECTION --target TARGET --table TABLE [options]
```

See [Copy a subset](../../guides/copy-a-subset.md#schema-creating-the-targets-tables) for how to use it.

## Options

| Flag | Default | Effect |
| --- | --- | --- |
| `--connection CONNECTION` | required | The alias to read table definitions from. |
| `--target TARGET` | required | The alias the tables are for; its dialect decides the types. |
| `--table TABLE` | required | A table to create (repeatable) |
| `--related` | off | Also every table a subset rooted at --table would copy. |
| `--no-children` | off | With --related, only the tables --table references. |
| `--no-foreign-keys` | off | Leave foreign keys out of the generated tables. |
| `--stage-suffix STAGE_SUFFIX` |  | Also create <table><suffix> stage tables, for swap jobs; alone if --target is --database. |
| `--apply` | off | Create the tables in --target, skipping any that already exist. |
| `--output OUTPUT` |  | Write the SQL here instead of stdout; must not already exist. |

It also takes the [common flags](index.md#common-flags): `--config`, `--connections`, `--log`, `--log-format`, `--log-level`, `--quiet`.

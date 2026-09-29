<!-- Generated from the parser by website/commands.py. Change the flag, then run `python website/commands.py`. -->

# `bauta subset`

Generate jobs that copy a referentially complete subset.

```
bauta subset --connection CONNECTION --target TARGET --root ROOT --where WHERE [options]
```

See [Copy a subset](../../guides/copy-a-subset.md) for how to use it.

## Options

| Flag | Default | Effect |
| --- | --- | --- |
| `--connection CONNECTION` | required | The alias to read from. |
| `--target TARGET` | required | The alias the generated jobs load into. |
| `--root ROOT` | required | The table the subset starts from. |
| `--where WHERE` | required | SQL filter on the root table, e.g. "created_at >= '2026-01-01'". |
| `--no-children` | off | Copy only the root rows and what they reference, not rows referencing them. |
| `--ignore-foreign-key TABLE.COLUMN` |  | Do not follow this foreign key (repeatable); needed to break a cycle. |
| `--mask` | off | Also propose a masking policy for every table, as discover does. |
| `--sample SAMPLE` | `1000` | Rows sampled per table to classify columns. |
| `--key-variable KEY_VARIABLE` | `MASKING_KEY` | Environment variable the generated jobs read the masking key from. |
| `--chunk-size CHUNK_SIZE` | `5000` | chunkSize for the generated jobs. |
| `--mask-keys` | off | Mask numeric surrogate keys too, in the domain each foreign key shares, instead of keeping them. |
| `--output OUTPUT` |  | Write the generated jobs here instead of stdout; must not already exist. |
| `--rules FILE` | discovery.yaml in the configuration directory, if there is one | Your own rules for recognising personal data, ahead of the built-in ones. |

It also takes the [common flags](index.md#common-flags): `--config`, `--connections`, `--log`, `--log-format`, `--log-level`, `--quiet`.

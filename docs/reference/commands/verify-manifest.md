<!-- Generated from the parser by website/commands.py. Change the flag, then run `python website/commands.py`. -->

# `bauta verify-manifest`

Check that a manifest is unaltered, and who signed it.

```
bauta verify-manifest [MANIFEST] [options]
```

See [Keep a masking manifest](../../guides/keep-a-manifest.md#sealing-and-verifying) for how to use it.

## Options

| Flag | Default | Effect |
| --- | --- | --- |
| `MANIFEST` | --manifest-connection, else jobs.yaml's `manifest` | A manifest file. |
| `--manifest-connection ALIAS` |  | The masking manifest in a table of this connection. |
| `--manifest-table MANIFEST_TABLE` | jobs.yaml's, else bauta_manifest | The manifest table. |
| `--run RUN_ID` |  | From a table, this run's manifest rather than the latest. |
| `--jobs JOBS` |  | Explicit path to the jobs file, overriding --config. |
| `--manifest-key-variable MANIFEST_KEY_VARIABLE` | `BAUTA_MANIFEST_KEY` | Environment variable holding the manifest signing key. |

It also takes the [common flags](index.md#common-flags): `--config`, `--connections`, `--log`, `--log-format`, `--log-level`, `--quiet`.

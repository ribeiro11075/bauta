# Configuration files


The field reference. For *why* things behave as they do, see [design.md](../concepts/how-it-works.md). `example/starter/configuration/` is a complete set of these files, validated on every test run — copying it is the fastest start.


## Where configuration is found

The CLI reads its configuration from one directory, found in this order:

1. `--config DIR`
2. `$BAUTA_CONFIG`
3. `./configuration`

| File | Required | Holds | Read by |
| --- | --- | --- | --- |
| `connections.yaml` | yes | The [connection aliases](connections.md). | Every command, except `history` and `verify-manifest` given a file. |
| `jobs.yaml` | for jobs | The [data jobs](jobs.md), and where run state, history and the manifest are kept. | `run`, `validate`, `jobs`, `audit`, `clear`; `history` and `verify-manifest` when no flag names a file or table. |
| `discovery.yaml` | no | [Rules of your own](../guides/propose-a-policy.md#your-own-rules-discoveryyaml) for recognising personal data. | `discover`, `subset --mask`, `audit`, `synthesize`, `validate`. |

`--connections FILE`, `--jobs FILE` and `--rules FILE` name a file anywhere else.

Paths inside `jobs.yaml` are relative to `jobs.yaml`, so a cron entry and a shell started elsewhere find the same files. Paths on the command line are relative to the working directory. A layout that keeps what you write apart from what runs write:

```
configuration/    connections.yaml, jobs.yaml, discovery.yaml
transaction/      run state, history and the manifest, which jobs.yaml points at as ../transaction/...
```

`example/starter/configuration/` is set up this way.


## Credentials

Any string in any of these files may read from the environment:

```yaml
password: ${PROD_DB_PASSWORD}
port: ${PROD_DB_PORT:-5432}
key: ${file:/run/secrets/masking-key}
```

| Form | Behaviour |
| --- | --- |
| `${NAME}` | The variable's value. If it's unset, the run **stops before connecting to anything**, naming every missing variable at once. |
| `${NAME:-default}` | The variable, or `default` if unset. Use for ports, hosts and schema names — **never for a secret**, which would just put the credential back in the file. |
| `${file:/path}` | The file's content, without a trailing newline — how Docker, Kubernetes and secret-store drivers mount secrets. An unreadable file stops the run like an unset variable. |
| `$${NAME}` | A literal `${NAME}`, for SQL that contains one. PostgreSQL dollar-quoting (`$$body$$`) needs no escaping. |

Kept this way, `connections.yaml` holds references to secrets rather than secrets, and is safe to commit.


## Run state, history and the manifest

Each is kept in a file or in a database table, set in [`jobs.yaml`](jobs.md#file-level) or overridden for one run by its flags. A file set in `jobs.yaml` is relative to `jobs.yaml`; a file flag is relative to the working directory.

| What | `jobs.yaml` setting | File | Table | Without either |
| --- | --- | --- | --- | --- |
| **Run state**: last runs, watermarks and key fingerprints | `memory` | `--memory FILE` | `--memory-connection ALIAS`, `--memory-table NAME` | `memory.yaml` beside `jobs.yaml` |
| **History**: one record per job per cycle, for `bauta history` | `history` | `--history FILE` | `--history-connection ALIAS`, `--history-table NAME` | Not recorded. |
| **Masking manifest**: what was masked and how, sealed, for `bauta verify-manifest` | `manifest` | `--manifest FILE` | `--manifest-connection ALIAS`, `--manifest-table NAME` | Not written. |

Tables default to `bauta_memory`, `bauta_history` and `bauta_manifest`, and must exist first; [operations.md](tables.md) has their definitions. A manifest is signed when `$BAUTA_MANIFEST_KEY` is set.


## Validation

`bauta validate` checks everything above without connecting to anything: every field, every alias (including those `memory`, `history` and `manifest` name), every predecessor and that they form no cycle, every transformer reference, every masking strategy, option and key length, `maskingThreads` against the cores available, and `discovery.yaml` if there is one. Problems are reported all at once, as `ConfigurationError`, rather than one per run. It then prints where run state, history and the manifest resolve, how many masking threads a run would use, and which discovery rules apply.

`bauta run --dry-run` adds the checks that need a connection: that each database is reachable, that target tables exist, that upsert targets have a primary key, and that each masking policy covers every column its query returns.

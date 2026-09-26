# Bauta

**Put a mask on production:** a safe, realistic stand-in for your production data, in whichever database you need it. Bauta masks what it copies, copies only the slice you need with every relationship intact, generates what may not be copied at all, and moves data between databases on a schedule you already run, with nothing to host.

- **Masking:** consistent across tables and runs, one-to-one for keys (NIST FF1 where policy requires it), applied before anything reaches the target, and every column must be covered.
- **Discovery, subsets and synthetic data:** propose a masking policy from a live schema, copy a referentially complete slice of production, create the copy's tables in whichever database it goes to, and fill tables that can't be copied with generated rows.
- **Audit:** report what every job does with data and what a reviewer should question, and seal each run's masking manifest so it can be verified later.
- **Provable coverage:** list every table in production and what the jobs do with each, so a table nobody wrote a job for fails the build rather than going unnoticed. A database can require that nothing reaches it unmasked.
- **Seven databases:** Oracle, SQL Server, PostgreSQL, MySQL, MariaDB, SQLite and DuckDB, as source or target in any combination.
- **Streaming:** memory stays flat however large the table, and PostgreSQL and SQL Server targets load in bulk.
- **Fast:** a million rows of six masked columns, two of them one-to-one keys, in under 8 seconds on one core with the optional native masker, and 74 without; more cores for wide tables. Either way the masks are the same.
- **Incremental loads:** extract only what changed since the last successful run.
- **A dependency graph:** jobs run in order, concurrently where they can, each in its own process with an optional timeout.
- **Operable:** webhook alerts; run state, history and manifests each in a file or a table; and passwords from a command for cloud IAM tokens.
- **No infrastructure:** a `pip install`, some YAML, and a command you run from cron.


## Install

Python 3.10 or newer. Choose the drivers you need as extras; each is loaded only when a connection uses it.

```
pip install "bauta[postgresql,oracle]"
```

| Extra | Installs | Needs besides pip |
| --- | --- | --- |
| `mysql`, `mariadb` | mysql-connector-python | nothing |
| `postgresql` | psycopg 3, with its own libpq | nothing |
| `oracle` | oracledb, in thin mode | nothing — no Oracle client |
| `mssql` | pymssql | nothing |
| `sqlite` | Python's own `sqlite3` | nothing |
| `duckdb` | duckdb, with pyarrow for fast loads | nothing; one process at a time per file, see [DuckDB](docs/configuration.md#duckdb) |
| `fpe` | cryptography, for the `fpe` masking strategy | nothing; `oracle` already brings it |
| `native` | `bauta-rs`, the native masker (below) | nothing on Linux (x86-64, ARM) or macOS; elsewhere, [Rust](https://rustup.rs) 1.83 or newer |
| `all` | every driver above | nothing |

**The native masker (optional).** `bauta-rs` masks in Rust: about ten times the throughput on one core, with identical masks. It can also mask on several cores: `jobs.yaml`'s `maskingThreads` is `1` by default, a number up to the cores available, or `auto` to divide the cores between the jobs running (see [masking threads](docs/masking.md#masking-threads)). `pip install "bauta[postgresql,native]"` installs the version that matches, which is the only one Bauta uses. Without it, everything works, only slower. See [the native masker](docs/masking.md#the-native-masker).


## Quickstart

Start from the configuration in [`example/starter/configuration/`](example/starter/configuration/), from a clone or downloaded from GitHub:

```
mkdir configuration
cp example/starter/configuration/*.yaml configuration/
```

Edit `configuration/connections.yaml` and `configuration/jobs.yaml` for your databases, then supply the credentials they reference:

```
export SOURCE_DB_PASSWORD=...  TARGET_DB_PASSWORD=...  MASKING_KEY=...

bauta validate           # check the configuration, offline
bauta run --dry-run     # check connections and tables, moving nothing
bauta run                # run every job once
```

To see it work without any of that, the demos in a clone of this repository use throwaway SQLite databases:

```
git clone https://github.com/ribeiro11075/bauta.git && cd bauta
pip install -e ".[fpe]"

python example/walkthrough/demo.py       # the whole workflow: discover, subset, audit, mask, verify, synthesize
python example/incremental/demo.py       # streaming and incremental loads
python example/masking/demo.py           # masking, discovery and a subset, from Python
python example/native-masking/demo.py    # Python against Rust, and Rust on one core against all of them
```


## The command

```
bauta run                run every job that's due, once
bauta validate           check the configuration, without connecting
bauta jobs               show the job graph and which jobs are due
bauta history            show recent job outcomes

bauta discover           propose a masking policy for tables
bauta subset             generate jobs that copy a referentially complete subset
bauta schema             create target tables from source ones, in the target's dialect
bauta synthesize         fill tables with generated rows, for data that can't be copied
bauta clear              empty the jobs' target tables, children first

bauta audit              report what each job does with data, and what to question
bauta coverage           list a source database's tables and what the jobs do with each
bauta verify-manifest    check a masking manifest is unaltered, and who signed it
bauta verify-references  count rows in the copy whose foreign key points at nothing

bauta --version          print the version, and which masker it would use
```

| Exit code | Meaning |
| --- | --- |
| `0` | Every job completed. |
| `1` | A job failed, or was skipped because a predecessor failed, or the command failed on a database error. |
| `2` | Invalid configuration or usage. |
| `130` | Interrupted by a signal: running jobs finished, the rest were skipped. |

`run` makes one pass and exits, so it fits under cron or a Kubernetes CronJob. A second `run` sharing the same run state refuses to start while the first is still going. `bauta <command> --help` lists every flag.

### Configuration and logging

Every command takes these.

| Flag | Default | Effect |
| --- | --- | --- |
| `--config DIR` | `$BAUTA_CONFIG`, else `./configuration` | Read `jobs.yaml` and `connections.yaml` from this directory. |
| `--log FILE` | none | Also write logs to this file. |
| `--log-format json` | `text` | Write one JSON object per log line, for a collector. |
| `--quiet` | off | Don't log to stderr. |

### Running jobs

| Flag | Default | Effect |
| --- | --- | --- |
| `--job NAME` | every job | Run only this job, ignoring its `refresh` window. Its predecessors don't run; `run` warns about each. Repeatable. |
| `--force` | off | Ignore every job's `refresh` window. |
| `--forever` | off | Keep running cycles instead of exiting after one. For freshness under cron's one-minute floor. |
| `--dry-run` | off | Check connections, target tables, primary keys and masking coverage, moving no rows. |
| `--accept-key-change` | off | Run upsert jobs whose masking key changed since their last run. Refused where the policy masks the target's primary key; see [rotating the masking key](docs/operations.md#rotating-the-masking-key). |
| `--notify-url URL` | `$BAUTA_NOTIFY_URL` | Post a JSON summary to this webhook when a cycle doesn't succeed. |

### Run state, history and the manifest

Each is kept in a file or in a database table, set in [`jobs.yaml`](docs/configuration.md#file-level) or overridden for one run by its flags. A file set in `jobs.yaml` is relative to `jobs.yaml`; a file flag is relative to the working directory.

| What | `jobs.yaml` setting | File | Table | Without either |
| --- | --- | --- | --- | --- |
| **Run state**: last runs, watermarks and key fingerprints | `memory` | `--memory FILE` | `--memory-connection ALIAS`, `--memory-table NAME` | `memory.yaml` beside `jobs.yaml` |
| **History**: one record per job per cycle, for `bauta history` | `history` | `--history FILE` | `--history-connection ALIAS`, `--history-table NAME` | Not recorded. |
| **Masking manifest**: what was masked and how, sealed, for `bauta verify-manifest` | `manifest` | `--manifest FILE` | `--manifest-connection ALIAS`, `--manifest-table NAME` | Not written. |

Tables default to `bauta_memory`, `bauta_history` and `bauta_manifest`, and must exist first; [operations.md](docs/operations.md#tables) has their definitions. A manifest is signed when `$BAUTA_MANIFEST_KEY` is set.

### Saying less, and saying it once

A `defaults:` block in [`jobs.yaml`](docs/configuration.md#defaults) supplies what every job would otherwise repeat, so a job says only what is particular to it:

```yaml
defaults:
  sourceConnection: prod
  targetConnection: staging
  insertStrategy: upsert
  masking:
    key: ${MASKING_KEY}

jobs:
  maskCustomers:
    sourceQuery: select id, email from customers
    targetTableFinal: customers
    masking:
      columns: {id: keep, email: email}
```

A masking policy's `columns` stays with its job, so what a job does to its data can be read in one place. Any field a file doesn't recognise is an error, so a misspelling stops `bauta validate` rather than being quietly ignored.

### Proving the copy is safe

| What | Where |
| --- | --- |
| Every column of every job is covered, and references still match once masked | `bauta audit --connect --strict` |
| Every table in production is copied, declared, or fails the build | `bauta coverage` |
| Nothing can ever reach this database unmasked | [`requireMasking`](docs/configuration.md#requiring-masking) on the alias |
| This job copies as it stands, and somebody decided so | [`unmasked: true`](docs/configuration.md#copying-without-masking) on the job |
| This table is deliberately not copied, and why | [`acknowledged`](docs/configuration.md#acknowledged) in `jobs.yaml` |
| What was masked, how, and under which key, sealed | `bauta verify-manifest` |
| No row in the copy points at a row that isn't there | `bauta verify-references` |

### Proposing and reviewing policies

`discover`, `subset --mask`, `audit` and `synthesize` recognise personal data by built-in rules, and by rules of your own:

| Flag | Default | Effect |
| --- | --- | --- |
| `--rules FILE` | `discovery.yaml` in the configuration directory, if there is one | Check these rules before the built-in ones. See [your own rules](docs/masking.md#your-own-rules-discoveryyaml). |

`discover` takes `--all-tables` for a whole database instead of a repeated `--table`, and `discover`/`subset` take `--mask-keys` to mask numeric surrogate keys as well as text ones, in the domain each foreign key already shares.

### Environment variables

The ones you'd set in a deployment; [operations.md](docs/operations.md#environment-variables) also lists two for diagnosis.

| Variable | Effect |
| --- | --- |
| `BAUTA_CONFIG` | The configuration directory, when `--config` isn't given. |
| `BAUTA_NOTIFY_URL` | The webhook, when `--notify-url` isn't given. |
| `BAUTA_MANIFEST_KEY` | Sign manifests, and verify their signatures. |
| `BAUTA_MASKING_THREADS` | Threads the native masker uses per job: a number or `auto`. Overrides `jobs.yaml`'s `maskingThreads`; see [masking threads](docs/masking.md#masking-threads). |


## Documentation

| Document | Covers |
| --- | --- |
| [Configuration](docs/configuration.md) | every field, how credentials are read from the environment, and connection options such as TLS |
| [Masking](docs/masking.md) | strategies, consistent masks across tables, the key, the manifest, `audit`, `discover`, `subset`, `schema`, `synthesize` and `clear` |
| [How it works](docs/design.md) | streaming, incremental loads, retries, scheduling, and the masking design |
| [Operating it](docs/operations.md) | run state, history and notifications |
| [Security model](docs/security.md) | what masking protects and what it doesn't, the constructions, keys, and a deployment checklist |
| [Library](docs/library.md) | embedding it in Python, results, memory backends |
| [Development](docs/development.md) | running the tests, including against real databases; see also [contributing](CONTRIBUTING.md) |
| [Changelog](CHANGELOG.md) | what changed in each release, breaking changes first |


## Layout

| Path | What it is |
| --- | --- |
| `bauta/configuration/` | reading and validating the YAML: `${NAME}` and `passwordCommand` in `environment.py`, the models in `models.py` |
| `bauta/database/` | streaming and loading rows in `connection.py`; what differs between the seven databases in `dialects/`, one module each |
| `bauta/jobs/` | `runner.py` runs a cycle of jobs, each in a process of its own (`workers.py`) moving rows a chunk at a time (`pipeline.py`); run state in `memory.py`, history and manifests in `reporting.py` |
| `bauta/masking/` | the keyed hash, `Strategy` and masking plans in `core.py`, the built-in strategies in `strategies.py` |
| `bauta/transform/` | per-column transforms, applied before masking, and the ones that ship in `builtinTransforms.py` |
| `bauta/generate/` | `discover`, `subset`, `schema` and `synthesize`: what is built from a live schema |
| `bauta/review/` | `audit`, `coverage` and `verify-references`: reports that move no data |
| `bauta/log/` | logging, and the scrubbing that keeps values out of every message |
| `bauta/cli/` | the `bauta` command, one module per group of subcommands |
| `mask-rs/` | the optional native masker, in Rust — see [its README](mask-rs/README.md) |
| `example/` | runnable demos, each with its `configuration/`, and a starter configuration — see [its README](example/README.md) |
| `docs/` | the documentation above |
| `tests/` | the test suite, laid out like the package; see [where the tests are](docs/development.md#where-the-tests-are) |


## License

[MIT](LICENSE)

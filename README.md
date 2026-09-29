# Bauta

**Put a mask on production:** a safe, realistic stand-in for your production data, in whichever database you need it. Bauta masks what it copies, copies only the slice you need with every relationship intact, generates what may not be copied at all, and moves data between databases on a schedule you already run, with nothing to host.

- **Masking:** consistent across tables and runs, one-to-one for keys (NIST FF1 where policy requires it), applied before anything reaches the target, and every column must be covered.
- **Discovery, subsets and synthetic data:** propose a masking policy from a live schema, copy a referentially complete slice of production, create the copy's tables in whichever database it goes to, and fill tables that can't be copied with generated rows.
- **Audit:** report what every job does with data and what a reviewer should question, and seal each run's masking manifest so it can be verified later.
- **Provable coverage:** list every table in production and what the jobs do with each, so a table nobody wrote a job for fails the build rather than going unnoticed. A database can require that nothing reaches it unmasked.
- **Seven databases:** Oracle, SQL Server, PostgreSQL, MySQL, MariaDB, SQLite and DuckDB, as source or target in any combination.
- **Files as a target:** a masked copy in Parquet, CSV or JSON Lines, in a directory or in S3, Google Cloud Storage or Azure, for a data lake that Athena, Snowflake or Databricks reads: appended to incrementally or published whole as a snapshot, with nothing visible until a run succeeds.
- **Iceberg tables:** a masked copy as Iceberg tables in any of the three clouds, through a Glue, REST or SQL catalog: each run one commit, upserts that merge by key, and old snapshots' files deleted with them.
- **Streaming:** memory stays flat however large the table, and PostgreSQL and SQL Server targets load in bulk.
- **Fast:** a million rows of six masked columns, two of them one-to-one keys, in under 8 seconds on one core with the optional native masker, and 74 without; more cores for wide tables. Either way the masks are the same.
- **Incremental loads:** extract only what changed since the last successful run.
- **A dependency graph:** jobs run in order, concurrently where they can, each in its own process with an optional timeout.
- **Operable:** webhook alerts; run state, history and manifests each in a file or a table; and passwords from a command for cloud IAM tokens.
- **No infrastructure:** a `pip install`, some YAML, and a command you run from cron.


## Documentation

**[ribeiro11075.github.io/bauta](https://ribeiro11075.github.io/bauta/)**, for the release you have installed, or read it here in [`docs/`](docs):

| Start with | For |
| --- | --- |
| [Install](docs/get-started/install.md) and [Quickstart](docs/get-started/quickstart.md) | the drivers, the native masker, and a first run |
| [Guides](docs/guides/mask-a-table.md) | one task each: masking a table, keeping joins, subsets, proving the copy safe, running it on a schedule |
| [Reference](docs/reference/configuration.md) | every file, field, strategy and [command](docs/reference/commands/index.md) |
| [How it works](docs/concepts/how-it-works.md) and the [security model](docs/concepts/security.md) | the design, and what masking protects and what it doesn't |
| [Changelog](CHANGELOG.md) | what changed in each release, breaking changes first |


## Install

Python 3.10 or newer. Choose the drivers you need as extras; each is loaded only when a connection uses it.

```
pip install "bauta[postgresql,oracle]"
```

[Install](docs/get-started/install.md) lists every extra, and the optional native masker, which masks about ten times as fast.


## Quickstart

Start from the configuration in [`example/starter/configuration/`](example/starter/configuration), from a clone or downloaded from GitHub:

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


Every command and flag is on its own page under [commands](docs/reference/commands/index.md).


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
| `tests/` | the test suite, laid out like the package; see [where the tests are](docs/project/development.md#where-the-tests-are) |


## License

[MIT](LICENSE)

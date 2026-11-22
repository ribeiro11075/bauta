# Bauta

**Put a mask on production:** a safe, realistic stand-in for your production data, in whichever database you need it. Bauta masks what it copies, copies only the slice you need with every relationship intact, generates what may not be copied at all, and moves data between databases on a schedule you already run, with nothing to host.

- **Masking:** consistent across tables and runs, one-to-one for keys (NIST FF1 where policy requires it), applied before anything reaches the target. Every column must be covered.
- **Discovery, subsets and synthetic data:** propose a policy from a live schema, copy a referentially complete slice of production, create the copy's tables in the target's dialect, and generate rows for tables that can't be copied.
- **Proof for a reviewer:** an audit of what every job does with data, coverage of every production table, so a table nobody wrote a job for fails the build, and a sealed manifest of each run. A database can refuse anything unmasked.
- **Seven databases:** Oracle, SQL Server, PostgreSQL, MySQL, MariaDB, SQLite and DuckDB, as source or target in any combination.
- **Files and lakes:** read Parquet, CSV or JSON Lines with SQL; write them to a directory, S3, Google Cloud Storage or Azure, or write Iceberg tables through a Glue, REST or SQL catalog. Nothing is visible until a run succeeds.
- **Built for large tables:** rows stream in chunks, so memory stays flat; loads are incremental where you want; a million rows of six masked columns take under 8 seconds on one core with the optional native masker (74 without), with the same masks either way.
- **Operable:** jobs run as a dependency graph, each in its own process; webhook alerts; history with the time each job spent reading, masking and writing; a read limit per source; passwords from a command, for cloud IAM tokens.
- **Studio (preview):** a web console to watch, start and cancel runs, review new columns, edit jobs and hand a reviewer a run's evidence, with roles and an activity log. See [Studio](docs/guides/studio.md).
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
bauta status             show what's running now, and where each job stands
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
bauta bench              measure how fast each job reads and masks from its real source, writing nothing

bauta studio serve       run Studio, the web console (pip install "bauta[studio]")

bauta --version          print the version, and which masker it would use
```

| Exit code | Meaning |
| --- | --- |
| `0` | Every job completed. |
| `1` | A job failed, or was skipped because a predecessor failed, or the command failed on a database error. |
| `2` | Invalid configuration or usage. |
| `130` | Interrupted by a signal: running jobs finished, the rest were skipped. |

`run` makes one pass and exits, so it fits under cron or a Kubernetes CronJob. A second `run` refuses to start while the first is still going, though runs of different jobs (`--job`) go side by side, as an orchestrator starts them; the same job never runs twice at once ([overlapping runs](docs/guides/run-on-a-schedule.md#run-state)). Every command and flag has its own page under [commands](docs/reference/commands/index.md), and `bauta <command> --help` lists them too.


## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setting up, where the code is, and what a change needs.


## License

[MIT](LICENSE)

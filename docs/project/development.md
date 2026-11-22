# Development

```
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]" ./mask-rs/py    # leave off ./mask-rs/py without Rust
pytest
ruff check
mypy                                         # targets Python 3.10, the oldest supported
```


## The default test run

`pytest` needs no servers. It covers everything that doesn't need a real network database, which is most of the package:

- Unit tests against mocked cursors, verifying the SQL each dialect builds and the control flow around it.
- **Real SQLite**, end to end — `tests/integration/test_integration_sqlite.py` runs real streaming and incremental loads through real worker processes. It isn't marked `integration`, since SQLite ships with Python and needs no service.
- The CLI, driven through `main()` against a real SQLite database, including its exit codes, and `discover` and `subset` output that is then run.
- Masking end to end through real worker processes (`tests/masking/test_masking_end_to_end.py`), and the masking strategies' properties (`tests/masking/test_masking.py`): determinism, consistency within a domain, one-to-one keys, and preserved types.
- The shipped examples: every `configuration/` under `example/` is validated against the real models, and every demo is run end to end, so none can drift from what the code accepts.
- With the native masker installed, Python against Rust over the same values (`tests/masking/test_nativeMasking.py`), on one thread and on eight, with identical masks and errors required.

A connection or file left open, by the package or by a test, fails the run: `ResourceWarning` is an error. The garbage collector finds a leak later than it happened, so the failure is charged to whichever test is running then; `PYTHONTRACEMALLOC=20 pytest` shows where the object was opened.

`tests/conftest.py` stubs `oracledb` and `psycopg` where they aren't installed, so the default run needs no client libraries. It also creates the package's logger before any test and detaches the handlers each test adds, since `caplog` only reaches a non-propagating logger that exists when the test starts.

### Where the tests are

`tests/` mirrors the package: `tests/jobs/` tests `bauta/jobs/`, and so on; `tests/test_project.py` covers `bauta/project.py`. Studio's tests run through its API, and are skipped without the `studio` extra. Three folders don't follow a package:

- `tests/integration/`: against real databases (all but SQLite marked `integration`).
- `tests/examples/`: every demo and the masking benchmark run, and every shipped configuration validated.
- `tests/repository/`: the docs' links and the packaging.

Helpers sit beside the tests they serve: `masking/customStrategies.py` (a strategy from outside the package), `jobs/crashingTransforms.py` (transforms that make a job exit, hang or linger), `examples/demos.py` (loads a demo by path), `integration/servers.py` (the Docker databases' settings, and `serverSettings()`, which skips a test when its server can't be reached). `jobConfigs.py` builds a data job with every required setting, which each suite narrows with `functools.partial`.


## The native masker

`mask-rs/` holds `bauta-rs`, the optional Rust extension: a separate distribution, so this package installs anywhere without a Rust toolchain. It is released at `bauta`'s own version, which the Cargo workspace's `version` must match; `tests/repository/test_packaging.py` checks, along with the `native` extra's pin. See [its README](../../mask-rs/README.md) for the layout. Rust 1.83 or newer:

```
cd mask-rs
cargo test --release              # bauta-core: 3,626 recorded vectors, NIST FF1, RFC 4231
cd py && maturin build --release
pip install ../target/wheels/bauta_rs-*.whl
```

**Rebuild it after changing `mask-rs/`, before running the tests**, or they run against the stale build:

```
cd mask-rs/py && maturin develop --release
```

`--release` matters: two tests measure SHA-256 and AES throughput to catch a backend fallen back to software, which a debug build looks like. `cargo test` covers `bauta-core`; the extension crate needs Python to link, so `tests/masking/test_nativeMasking.py` tests it.

**Python is the reference.** Change masking in Python first, port it, then regenerate the vectors with `python3 mask-rs/generate_vectors.py`. `tests/masking/test_maskVectors.py` fails if Python drifts from the file (Python-only strategies included, under `pythonOnly`), so regenerating it is deliberate: it means masks have changed. Run the suite both ways, as CI does:

```
pytest                              # with the extension, if installed
BAUTA_NATIVE=0 pytest               # without
```


## Benchmarks

Every throughput figure in [what each strategy costs](../guides/make-it-faster.md#what-each-strategy-costs) and [make it faster](../guides/make-it-faster.md) comes from one script, so they can be measured again rather than trusted:

```
python benchmarks/masking.py                  # a million rows: about four minutes, one of them the pure-Python run
python benchmarks/masking.py --no-python      # without it
python benchmarks/masking.py --rows 100000    # quicker, for a before-and-after on a change
```

It needs no server: it copies a SQLite table, masked, once per policy and per masker, and fails if two copies of a policy differ. Measure on an idle machine, before and after a change on the same one; the docs' figures are from a ten-core M1 Pro. `tests/examples/test_masking_benchmark.py` keeps it working on 300 rows. For one construction alone, `cargo bench --bench speed` in `mask-rs/core` times an HMAC and a `key` permutation.


## Integration tests

In `tests/integration/`:

| File | What it checks |
| --- | --- |
| `test_integration_databases.py` | What every database must do alike, on all seven: a table's shape, inserts, both upserts, swap, truncate, streaming, run state in a table, whole jobs. A check here runs everywhere; its SQLite and DuckDB runs are part of the default `pytest`. |
| `test_integration_{postgresql,oracle,mssql,sqlite,duckdb}.py` | What only that database does, such as every value type through PostgreSQL's `COPY` and SQL Server's multi-row statements. |
| `test_integration_schema.py` | `schema` and a copy for all 49 pairs of databases, `clear` under live foreign keys, and swaps through `schema`'s stage tables. |
| `test_integration_masking.py` | Foreign-key discovery, subset queries and masked jobs over each server's numeric and date types. |
| `test_integration_keys.py` | Key lookups beside a same-named table in another schema, upserts beside UNIQUE constraints, swaps of qualified and referenced tables. |
| `test_integration_references.py` | That `verify-references` counts rows loaded past a key. |
| `test_integration_scrubbing.py` | That no value from a failing load reaches an error, an outcome or a log. |
| `test_integration_connections.py` | Driver options, encryption and `currentSchema`, as each server reports them. |
| `test_integration_names.py` | Tables only writable in quotes (reserved words, spaces, case), on all seven: read it first when a name is involved. |
| `test_integration_cross_database.py` | One job from MySQL, transformed, into PostgreSQL. |
| `test_integration_files.py`, `_clouds.py`, `_iceberg.py` | Parquet from each database; file targets on S3, GCS and Azure; Iceberg through SQL, Glue and REST catalogs. |

Two checks outside it need no server: `tests/jobs/test_orphans.py` kills a run outright and checks its jobs die with it (POSIX only), and `tests/masking/test_fpe.py` checks FF1 against NIST's published sample vectors.

The integration tests are marked `integration` and excluded from the default run:

```
docker compose up -d mysql postgresql oracle mssql mariadb s3 gcs azure iceberg-rest
pip install -e ".[all,dev]"
pytest -m integration
docker compose down
```

| Service | Port | Image |
| --- | --- | --- |
| mysql | 3307 | `mysql:8.4` |
| postgresql | 5433 | `postgres:16` |
| oracle | 1522 | [`gvenzl/oracle-free`](https://github.com/gvenzl/oci-oracle-free) — free, no Oracle registry login |
| mssql | 1434 | Microsoft's official image — amd64 only, runs under emulation on Apple Silicon |
| mariadb | 3308 | `mariadb:11` |
| s3 | 5055 | [`motoserver/moto`](https://github.com/getmoto/moto) — S3 in memory, for a files connection's tests; MinIO no longer publishes images |
| gcs | 4443 | [`fsouza/fake-gcs-server`](https://github.com/fsouza/fake-gcs-server) — Google Cloud Storage, read anonymously |
| azure | 10000 | [Azurite](https://github.com/Azure/Azurite) — Azure Blob Storage without a hierarchical namespace, its account `devstoreaccount1` |
| iceberg-rest | 8181 | [`apache/iceberg-rest-fixture`](https://hub.docker.com/r/apache/iceberg-rest-fixture) — the Apache Iceberg project's REST catalog, its files in the `s3` service |

Each test creates its own uniquely named table and drops it afterwards, so the suite is safe to re-run against running containers. A missing driver or server skips the affected tests with a reason, rather than failing them.

**A sandbox to explore by hand.** `python tests/integration/sandbox.py create NAME DIR` builds a deliberately awkward schema on SQLite and the five servers (composite keys, mutual references, reserved-word names, values at each type's edges) with a ready `DIR/configuration/connections.yaml`; `drop` removes it. It is for trying commands against shapes that break data tools.

Run the integration tests before trusting a change to anything database-facing; they have found bugs the mocked suite couldn't.

**Coverage.** CI's pinned-versions integration job runs the unit and integration suites together, job processes included, and fails below 95% of lines covered; it was 96.3% when the floor was set. To see it locally, with the servers up:

```
COVERAGE_FILE=$PWD/.coverage pytest -m "integration or not integration" --cov
```


## Dependency versions

`pyproject.toml` gives ranges, not pins, so the package installs beside other tools that have their own. Two files in `constraints/` pin them:

- **`lowest.txt`** is the bottom of every range. CI installs it on Python 3.10 and runs everything, the integration suite included, so a lower bound that stops working fails there first.
- **`image.txt`** pins every package `bauta[all,studio]` installs, to one tested set of newer versions. CI's other integration run installs it.

**Known vulnerabilities.** `.github/workflows/audit.yml` runs `pip-audit` over `image.txt` and `cargo audit` over `mask-rs/Cargo.lock` on every change and every Monday, since an advisory is published against code that hasn't changed. `lowest.txt` isn't audited: its versions are old on purpose. To run them locally:

```
pip install pip-audit && pip-audit --strict -r constraints/image.txt
cargo install --locked cargo-audit && (cd mask-rs && cargo audit)
```

`tests/repository/test_packaging.py` checks that `lowest.txt` matches the lower bounds and that `image.txt` is within the ranges. To raise a lower bound, change both `pyproject.toml` and `lowest.txt`. To move the pinned set to newer versions, edit the direct pins in `image.txt` and regenerate the rest with the command at its top.


## Studio's web app

Studio's API is Python, under `bauta/studio/`, and tested with the rest. Its web app is React and TypeScript, under `studio/web/`, built with Node (the release uses Node 24). The package ships it built: the release workflow runs `npm ci && npm run build`, which writes `bauta/studio/static/`, before building the wheel. A clone has no build until you make one:

```
cd studio/web && npm install && npm run build      # into bauta/studio/static, which `bauta studio serve` serves
npm run dev                                        # or Vite on :5173, passing /api to a `bauta studio serve` on :8710
npm run typecheck
npm test                                           # Vitest, beside what each test covers
```

To try it on something, `python studio/demo/setup.py demo-workspace` makes a small SQLite shop with five jobs, a few runs of history, a failing job and a column for discovery to find; its docstring has the commands to add an account and serve it.


### Studio's database

Studio's tables are declared in `bauta/studio/db.py` and created and upgraded by Alembic, as it starts, from `bauta/studio/migrations/versions/`. **Changing a model needs a migration:** add a file there whose `down_revision` is the latest one's `revision`, using `render_as_batch`-friendly operations (`with op.batch_alter_table(...)`), since SQLite rebuilds a table to alter it. `tests/studio/test_studioMigrations.py` fails if the migrations and the models disagree. Never edit a released migration.


## The documentation site

The site is `docs/` built with [MkDocs](https://www.mkdocs.org) and Material for MkDocs, configured in [`mkdocs.yml`](../../mkdocs.yml):

```
pip install -e ".[docs]"
mkdocs serve             # http://127.0.0.1:8000, rebuilt on every save
mkdocs build --strict    # what CI runs
```

- **Pages are Markdown that also reads on GitHub.** Link with relative paths, as anywhere in the repository. [`website/hooks.py`](../../website/hooks.py) turns a link out of `docs/` into a link to that file on GitHub, and shows `CHANGELOG.md`, `CONTRIBUTING.md` and `example/README.md` as pages of the site, so each has one copy.
- **A new page goes in `mkdocs.yml`'s `nav`**, in the section its folder names: `get-started/`, `guides/` (one task each), `reference/`, `concepts/` or `project/`. A test fails on a page left out.
- **Command pages are generated.** `docs/reference/commands/` is written from the parser by [`website/commands.py`](../../website/commands.py); after changing a flag or a command's help, run `python website/commands.py`. A test fails while the pages don't match the parser.
- **Anchors are GitHub's**, so a heading's link is the same on the site and on GitHub. Renaming a heading breaks the links to it, which the tests and `mkdocs build --strict` both report.
- The theme's overrides and stylesheet are in [`website/overrides/`](../../website/overrides/), and the landing page is `website/overrides/home.html`.

The `docs` extra pins exact versions, like `dev`: MkDocs 2 drops the plugins and theme overrides the site depends on, so moving past 1.6 is a migration rather than an upgrade.


## Continuous integration and releases

`.github/workflows/ci.yml` runs ruff, mypy and the default tests on every supported Python, with the newest dependency versions the ranges allow. It runs the integration suite against the `docker-compose.yml` servers twice: with `image.txt`'s versions on Python 3.14, together with the unit suite and under the coverage floor, and with the lowest versions on Python 3.10.

**Actions are pinned to commits**, the release in a comment, so a tag moved upstream can't change what the workflows (which publish to PyPI) run. [Dependabot](../../.github/dependabot.yml) proposes weekly updates for the actions, `pyproject.toml` and the Rust crates. It leaves alone `constraints/` (each file is regenerated whole), `bauta-rs` (which must equal bauta's version), and `num-bigint` below 0.5 until PyO3's feature for it moves.

`.github/workflows/release.yml` publishes a release when a tag matching the version in `bauta/_version.py`, the one place bauta's is written, and in `mask-rs/Cargo.toml`, is pushed. Bump both, the `native` extra's pin in `pyproject.toml`, and the two packages in `mask-rs/Cargo.lock`, together, and add the release to [CHANGELOG.md](../../CHANGELOG.md), breaking changes first:

```
git tag v0.1.3 && git push origin v0.1.3      # the version in bauta/_version.py
```

It builds `bauta`'s sdist and wheel, and `bauta-rs`'s sdist and wheels for Linux x86-64 and ARM and macOS Apple silicon and Intel, each on its own hardware, and runs the suite against each wheel on the oldest and newest Python before anything is published. Then both go to PyPI together, by trusted publishing, so there is no token. Set it up once per project, before its first tag:

1. On PyPI, add a trusted publisher for each of `bauta` and `bauta-rs`: this repository, workflow `release.yml`, environment `pypi`.
2. In the repository's settings, create an environment named `pypi`. Requiring a reviewer there makes each release wait for approval.

Once both are on PyPI, the documentation for that version is published to GitHub Pages with [mike](https://github.com/jimporter/mike), as a copy of its own beside the earlier ones, with `latest` moved to it. Set it up once: in the repository's settings, under Pages, publish from the `gh-pages` branch, which the first release creates.

If publishing stops partway, re-run the job: files already on PyPI are skipped. `Run workflow` on the Release workflow builds and tests everything without publishing, to try a change to the workflow before tagging.

To check the distributions locally:

```
python -m build && twine check dist/*
```

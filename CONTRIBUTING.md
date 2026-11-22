# Contributing

Thank you for helping. [docs/project/development.md](docs/project/development.md) is the full guide to the tests, the native masker and releases; this page is what to know before opening a pull request.

## Setting up

```
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"
pytest              # no servers needed
(cd studio/web && npm install && npm test && npm run build)   # Studio's web app, only to serve it
ruff check
mypy
```

Anything that touches SQL, a driver or a name should also pass the integration suite, against the databases in `docker-compose.yml`:

```
docker compose up -d --wait
pytest -m integration
```

## Where things are

| Path | What it holds |
| --- | --- |
| `bauta/configuration/` | reading and validating the YAML: the models in `models.py` and `connections.py`, `${NAME}` and `passwordCommand` in `environment.py` |
| `bauta/database/` | one connection's streaming and loading in `connection.py`, what a swap does beyond loading in `maintenance.py`; what differs between databases in `dialects/`, one module each |
| `bauta/lake/` | files and Iceberg tables: column types in `columns.py` and `columnar.py`, each format in `formats.py`, each cloud in `stores.py`, the targets in `target.py` and `iceberg.py`, reading files in `source.py` |
| `bauta/jobs/` | the cycle in `runner.py`, each job's process in `workers.py`, its chunk pipeline in `pipeline.py`, the key-change checks in `keys.py`; run state in `memory.py`, history and manifests in `reporting.py` |
| `bauta/masking/` | the keyed hash, `Strategy` and masking plans in `core.py`, the built-in strategies in `strategies/`, one module a kind |
| `bauta/transform/` | per-column transforms, applied before masking, and the built-in ones |
| `bauta/generate/` | `discover`, `subset`, `schema` and `synthesize`: what is built from a live schema |
| `bauta/review/` | `audit`, `coverage` and `verify-references`: reports that move no data |
| `bauta/log/` | logging, and scrubbing values out of every message |
| `bauta/cli/` | the `bauta` command, one module per group of subcommands |
| `bauta/project.py` | a configuration directory and its checks, as the commands and Studio share them |
| `bauta/studio/`, `studio/` | Studio: its server and API, and its React web app (`studio/web/`), built into `bauta/studio/static` |
| `mask-rs/` | the optional native masker, in Rust; see [its README](mask-rs/README.md) |
| `example/` | runnable demos and a starter configuration; see [its README](example/README.md) |

Their tests are in the same place under `tests/`: `tests/jobs/` for `bauta/jobs/`, and so on. Tests against real databases are in `tests/integration/`; see [where the tests are](docs/project/development.md#where-the-tests-are).

## What a change needs

- **A test that fails without it.** Name it for the behaviour, and say in its docstring what used to go wrong.
- **Docs, if behaviour changes.** Every configuration field and flag is documented; `tests/repository/test_documentation.py` checks the links. After changing a flag, run `python website/commands.py` to regenerate the command pages. See [the documentation site](docs/project/development.md#the-documentation-site).
- **A changelog entry** under *Unreleased* in [CHANGELOG.md](CHANGELOG.md), breaking changes first.
- **Masking changes go to Python first.** Python is the reference implementation; port the change to `mask-rs/`, then regenerate the vectors (`python3 mask-rs/generate_vectors.py`). A change that alters any mask is a breaking change.

## Style

Follow the code around you: camelCase names and `str.format`. `ruff check` covers errors, not style; there is no formatter to run.

Documentation in the code follows one pattern, so a reader always knows where to look:

- **Every file opens with what it is for**: a docstring in Python, `//!` in Rust, a `//` block in TypeScript.
- **Every function and class says what it does**, in a docstring under its definition (`///` or `/** */` above it in Rust and TypeScript). Write it as prose -- what it returns or does, what it raises -- and add the *why* where the code can't show it, including what went wrong before. Tests are the exception: a test's name says what it checks, so only its module and helpers need one.
- **Constants sit together at the top of a file**, after the imports, with one comment block above them naming each that needs explaining: `# NAME: what it is for.` Not a comment above each constant.
- **Inline comments are for logic that would surprise a careful reader.** What a comment would only restate, the code already says.

## Security

Please don't report a vulnerability in a public issue; see [SECURITY.md](SECURITY.md).

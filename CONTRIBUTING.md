# Contributing

Thank you for helping. [docs/development.md](docs/development.md) is the full guide to the tests, the native masker and releases; this page is what to know before opening a pull request.

## Setting up

```
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"
pytest              # no servers needed
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
| `bauta/configuration/` | reading and validating the YAML |
| `bauta/database/` | streaming and loading rows; everything that differs between databases in `dialects/`, one module each |
| `bauta/jobs/` | running jobs: the cycle in `runner.py`, one job's chunk pipeline in `pipeline.py`, its process in `workers.py`, the key-change checks in `keys.py`; run state, history and manifests |
| `bauta/masking/` | the keyed hash and `Strategy` in `core.py`, the built-in strategies in `strategies.py` |
| `bauta/transform/` | per-column transforms, and the built-in ones |
| `bauta/generate/`, `bauta/review/` | what `discover`, `subset`, `schema` and `synthesize` build; what `audit`, `coverage` and `verify-references` report |
| `bauta/log/` | logging, and scrubbing values out of messages |
| `bauta/cli/` | the command, one module per group of subcommands |

Their tests are in the same place under `tests/`: `tests/jobs/` for `bauta/jobs/`, and so on. Tests against real databases are in `tests/integration/`; see [where the tests are](docs/development.md#where-the-tests-are).

## What a change needs

- **A test that fails without it.** Name it for the behaviour, and say in its docstring what used to go wrong.
- **Docs, if behaviour changes.** Every configuration field and flag is documented; `tests/repository/test_documentation.py` checks the links.
- **A changelog entry** under *Unreleased* in [CHANGELOG.md](CHANGELOG.md), breaking changes first.
- **Masking changes go to Python first.** Python is the reference implementation; port the change to `mask-rs/`, then regenerate the vectors (`python3 mask-rs/generate_vectors.py`). A change that alters any mask is a breaking change.

## Style

Follow the code around you: camelCase names, `str.format`, and docstrings and comments that say *why*, including what went wrong before. `ruff check` covers errors, not style; there is no formatter to run.

## Security

Please don't report a vulnerability in a public issue; see [SECURITY.md](SECURITY.md).

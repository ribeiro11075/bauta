<!-- Generated from the parser by website/commands.py. Change the flag, then run `python website/commands.py`. -->

# Commands

| Command | What it does |
| --- | --- |
| [`bauta run`](run.md) | Run data jobs. |
| [`bauta validate`](validate.md) | Check configuration offline, without connecting to anything. |
| [`bauta audit`](audit.md) | Report what each job does with data, and what a reviewer should question. |
| [`bauta verify-references`](verify-references.md) | Count rows in each target whose foreign key points at nothing. |
| [`bauta coverage`](coverage.md) | List a source database's tables and what the jobs do with each. |
| [`bauta verify-manifest`](verify-manifest.md) | Check that a manifest is unaltered, and who signed it. |
| [`bauta history`](history.md) | Show recent job outcomes recorded with run --history. |
| [`bauta jobs`](jobs.md) | Show the job graph and which jobs are due. |
| [`bauta discover`](discover.md) | Propose a masking policy for tables, from their schema and a sample. |
| [`bauta subset`](subset.md) | Generate jobs that copy a referentially complete subset. |
| [`bauta schema`](schema.md) | Generate or apply CREATE TABLE statements for a target, from source tables. |
| [`bauta synthesize`](synthesize.md) | Fill existing tables with generated rows, for data that can't be copied. |
| [`bauta clear`](clear.md) | Empty the target tables of data jobs, children first (destructive) |
| `bauta --version` | Print the version, and which masker it would use. |

## Exit codes

Every command exits with one of these.

| Exit code | Meaning |
| --- | --- |
| `0` | Every job completed, or the command did what it was asked. |
| `1` | A job failed, or was skipped because a predecessor failed; the command failed on a database error; or a check (`audit`, `coverage`, `verify-manifest`, `verify-references`) found a problem. |
| `2` | Invalid configuration or usage. |
| `130` | Interrupted by a signal: running jobs finished, the rest were skipped. |

## Common flags

Every command takes these.

| Flag | Default | Effect |
| --- | --- | --- |
| `--config CONFIG` | $BAUTA_CONFIG or ./configuration | Directory holding jobs.yaml and connections.yaml. |
| `--connections CONNECTIONS` |  | Explicit path to the connections file, overriding --config. |
| `--log LOG` |  | Also write logs to this file (logs always go to stderr unless --quiet) |
| `--log-level {debug,info,warning,error}` | `info` | One of `debug`, `info`, `warning`, `error`. |
| `--log-format {text,json}` | `text` | Json emits one object per record, carrying job/status/rowCount as fields a log collector can filter and alert on. |
| `--quiet` | off | Do not log to stderr. |

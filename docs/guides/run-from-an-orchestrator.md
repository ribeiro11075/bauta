# Run from Airflow or Dagster


`bauta run` makes one pass and exits, so an orchestrator runs it like any command, its exit code the task's outcome. [`example/orchestrators/`](https://github.com/ribeiro11075/bauta/tree/master/example/orchestrators) has working files for both, and a demo that needs neither.

| File | What it is |
| --- | --- |
| `bautaTasks.py` | What both build from: the job graph, and the command each task runs. Copy it beside your DAG or Dagster code. |
| `airflowDag.py` | One task a run, every 15 minutes, and a weekly full refresh after the checks. |
| `airflowDagPerJob.py` | A task per bauta job, downstream of its predecessors. |
| `dagsterDefinitions.py` | An asset per bauta job, with an incremental and a full-refresh job over them. |
| `demo.py` | Runs a task per job the way both do, with no orchestrator: `python example/orchestrators/demo.py`. |


## Run the command, not the Python API

Each task runs `bauta` in a process of its own: `runDataJobs` starts each job in a new process, which re-imports the calling program, and in a worker that is the orchestrator itself. The command's exit codes also map onto a task's outcome:

| Exit code | The task |
| --- | --- |
| `0` | succeeded: every job completed |
| `1` | failed: a job failed or was skipped, or another run holds the jobs |
| `2` | failed: the configuration or the command is wrong, so retrying won't help |
| `130` | was stopped |

`--quiet` leaves only errors in the task's log, [one line each](../reference/commands/index.md#common-flags); add `--log FILE --log-format json` for the full record. `bautaTasks.command()` runs `python -m bauta` with the worker's interpreter, or whatever `$BAUTA_COMMAND` says (a virtualenv's `bauta`, `docker run … bauta`).

The scheduler parses DAG files often, usually without your secrets, so `jobGraph()` reads the jobs file and its [includes](../reference/configuration.md#splitting-the-jobs-across-files) as written, expanding and validating nothing; a `bauta validate` task checks the configuration when it runs.


## One task a run, or a task per job

**One task a run** (`airflowDag.py`) is simplest: bauta decides what runs, in what order and how many at once, by `workers`, `refresh`, `predecessors` and `maxConcurrentJobs`, as under cron. The orchestrator sees one task.

**A task per job** (`airflowDagPerJob.py`, `dagsterDefinitions.py`) gives each job its own log, duration, retry and place in the graph, and Dagster's lineage shows what each copy is built from. The orchestrator then decides what bauta did:

- **How many at once:** each task runs one job, so `workers` and `maxConcurrentJobs` limit nothing. An Airflow pool or Dagster concurrency key stands in for `workers`; give a connection that needs a limit a pool of its own.
- **When:** `--job` ignores `refresh`, so every task runs whenever the DAG does. Schedule it as often as the most frequent job needs, or split cadences into DAGs of their own.
- **After a failure:** the orchestrator skips a failed job's dependents, as bauta does.

`run --job` holds [only its own jobs' locks](run-on-a-schedule.md#run-state), so tasks for different jobs run side by side, and the same job never twice at once.


## The weekly refresh and the checks

Both examples schedule [`run --full-refresh`](../concepts/how-it-works.md#deletes) weekly, taking deleted rows out of incremental copies. In Airflow it follows two checks, so a policy fallen behind production fails a check rather than the refresh:

1. `bauta audit --strict`: among other things, any incremental job a full refresh can't replace.
2. `bauta discover --update`: any column production has gained or lost that a policy doesn't match ([keeping a policy up to date](propose-a-policy.md#keeping-a-policy-up-to-date)).

A refresh and a regular run can reach the same job at once. With one task a run, give both DAGs' run tasks one slot of one pool; with a task per job, the second to start a job exits 1, so give tasks a retry after a pause.


## Secrets

bauta reads the masking key and passwords from its environment. In Airflow, `airflowDag.py` renders `MASKING_KEY` from a Variable, which a secrets backend keeps out of Airflow's database; in Dagster, set it in the code location's environment. [`passwordCommand`](../reference/connections.md#passwords-that-expire) fetches short-lived database credentials either way.

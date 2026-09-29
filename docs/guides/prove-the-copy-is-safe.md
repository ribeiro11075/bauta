# Prove the copy is safe


| What | Where |
| --- | --- |
| Every column of every job is covered, and references still match once masked | `bauta audit --connect --strict` |
| Every table in production is copied, declared, or fails the build | `bauta coverage` |
| Nothing can ever reach this database unmasked | [`requireMasking`](../reference/connections.md#requiring-masking) on the alias |
| This job copies as it stands, and somebody decided so | [`unmasked: true`](../reference/jobs.md#copying-without-masking) on the job |
| This table is deliberately not copied, and why | [`acknowledged`](../reference/jobs.md#acknowledged) in `jobs.yaml` |
| What was masked, how, and under which key, sealed | `bauta verify-manifest` |
| No row in the copy points at a row that isn't there | `bauta verify-references` |


## Reviewing policies: `audit`

```
bauta audit                      # offline, from the configuration alone
bauta audit --connect --strict   # also asks the databases; fails on warnings
```

`audit` lists every job, whether it masks, and what each masked column gets. It then reports what a reviewer should question — things validation allows, because they can be right:

| Severity | Finding |
| --- | --- |
| error | A masked query returns a column the policy doesn't cover, or names one it doesn't return (`--connect`). |
| error | A masked query couldn't be run to check (`--connect`). |
| error | A `swap` job replaces a table that a foreign key declared in the target references, or one an earlier swap left on its stage table. The key stays on the old table, now the stage, and the next run can't empty the stage (`--connect`). |
| warning | A `swap` job replaces a table that declares foreign keys of its own. Its stage table has none, so after a run the copy stops enforcing them (`--connect`). |
| warning | A column is kept unmasked although its name suggests personal data (`email`, `ssn`, `phone`, ...), by the built-in rules or [your own](propose-a-policy.md#your-own-rules-discoveryyaml). |
| warning | `defaultStrategy` is `keep`, so any column added to the source later is copied unmasked. |
| warning | A job copies from a database without masking while other jobs mask what they read from it. |
| warning | A masked job reads over a connection that isn't encrypted, as the server reports it (`--connect`). |
| warning | `shuffle` on an incremental job, whose small chunks leave values near their own rows. |
| warning | A domain is masked two ways, or under two keys, in one target database, so its masks won't match across the columns that share it. Copies in different target databases may use different keys. |
| warning | A foreign-key column isn't masked exactly like the key it references (strategy, options, domain and key), so the copied references won't match (`--connect`). |
| warning | A foreign key and the key it references are both masked with `shuffle`, which moves values between rows, so the references point at other rows (`--connect`). |
| warning | A job copies only part of a table that another job's table references (it has a `watermarkColumn`, or its `sourceQuery` has a `WHERE`), and the referencing job isn't limited to match, so the copy can reference rows it lacks (`--connect`). A query counts as partial when it has a `WHERE`, `LIMIT`, `TOP` or `FETCH FIRST`, or joins another table. |
| warning | A job's table references another job's table, and the job can load before the other: it doesn't wait for it through `predecessors`, the other is inactive, or a job on the way has a longer `refresh` (`--connect`). |
| note | Columns that fall to `defaultStrategy`, by name (`--connect`). |

Without `--connect`, columns are shown as declared. With it, each masked query is run for a single row, discarded unexamined, to list the columns it really returns and the policy each one gets.

The foreign-key check reads the foreign keys of each target database and of the sources copied into it, since a copy often declares none. A key's tables are matched to jobs by `targetTableFinal`'s name, without its schema, and a masked job's target columns to its query's columns by position, as the load matches them. A job that doesn't mask copies every column as it is, and so does `keep`; a reference masked with `null` points at nothing, so it can't break.

The check for a parent copied in part uses the same keys and the same matching by table name. It doesn't parse SQL. A pair of jobs counts as matched when either query names the other's table: a child limited by `EXISTS` over its parent, as [`subset`](copy-a-subset.md) generates, or a parent that also selects what its children reference, as in [tables that reference each other](../concepts/how-it-works.md#tables-that-reference-each-other). A table that references itself is left to `subset`, which reports it as a cycle. Whether a job waits for another is decided as a run decides it: through active predecessors only, and in the cycles each runs in (see [refresh and predecessors](../concepts/how-it-works.md#refresh-and-predecessors)).

The check on `swap` jobs reads only the keys the target declares, since a key only the source has constrains nothing in the copy. A job whose `postTargetAdhocQueries` name the referencing table is taken to recreate its keys there. See [how a swap works](../concepts/how-it-works.md#how-a-swap-works).

`audit` exits 1 on an error, and with `--strict` on a warning too, so it can gate a CI pipeline. `--format json` writes the same report for other tools, and `--output FILE` writes it to a file. `--job` narrows it.


## `coverage`: what the jobs do not cover

`audit` checks the jobs that exist. It cannot see a table nobody wrote a job for — a table with no job has nothing to audit — and that is exactly what a new release adds to production.

`coverage` starts from the database instead of from the configuration. It lists every table in a source database and says what the jobs do with each:

```
bauta coverage
bauta coverage --connection prod --schema sales
bauta coverage --format json
```

```
prod: 4 table(s)

  audit_log                                NOT COVERED
    actor_email                            looks like personal data
  employees                                NOT COVERED
    salary                                 looks like personal data
  countries                                copied as it stands by copyCountries
  customers                                copied and masked by maskCustomers

Covered: 1 masked, 1 copied as they stand, 0 declared not copied. NOT COVERED: 2.
Add a job for each table above, or declare it in `acknowledged` with the reason it is not copied.
```

| State | Meaning |
| --- | --- |
| copied and masked | A job reads the table and masks what it reads. |
| copied as it stands | A job reads the table with no masking policy. `audit` reports these too. |
| not copied, declared | No job reads it, and [`acknowledged`](../reference/jobs.md#acknowledged) records why. |
| NOT COVERED | No job reads it, and nothing says that was intended. |

A table counts as covered when a job reading that database names it in its `sourceQuery`. That doesn't parse SQL — it is the same approximation [`audit`](#reviewing-policies-audit) makes for tables that reference each other — so a job whose query reaches a table only through a view covers it in fact but not in this report, and has to be declared.

For a table nothing covers, `coverage` reads its column names and marks the ones that look like personal data, by the same rules as [`discover`](propose-a-policy.md), including your own from [`discovery.yaml`](propose-a-policy.md#your-own-rules-discoveryyaml). Columns of covered tables aren't read.

**`coverage` exits 1 when any table is NOT COVERED**, so it can follow `run` in CI and fail the build when production grows a table the copy doesn't account for. `--database` picks the alias when the jobs read from more than one; `--job` narrows which jobs count as covering.

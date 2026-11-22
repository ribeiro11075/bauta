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
| error | A masked job's target table couldn't be read, most often because it doesn't exist yet: create it with [`schema`](../reference/commands/schema.md), or name `targetColumns` (`--connect`). |
| error | A `swap` job replaces a table a foreign key in the target references, so the next run can't empty the stage (`--connect`). |
| warning | A `swap` job replaces a table that declares foreign keys, which the copy stops enforcing every other run (`--connect`). |
| warning | A column is kept unmasked although its name suggests personal data (`email`, `ssn`, `phone`, ...), by the built-in rules or [your own](propose-a-policy.md#your-own-rules-discoveryyaml). |
| warning | A column is kept unmasked although its sampled values look like personal data, whatever it is called (`SELECT ssn AS ref` with `ref: keep`), by [`discover`](propose-a-policy.md)'s value rules (`--connect`). In an unmasked job it is an error. |
| warning | A domain whose columns hold values differently (an id as a number and as text, or padded and not), so their masks and references stop matching; [`normalize`](../reference/strategies.md#values-held-more-than-one-way-normalize) makes them agree (`--connect`). |
| warning | `defaultStrategy` is `keep`, so any column added to the source later is copied unmasked. |
| warning | A job copies from a database without masking while other jobs mask what they read from it. |
| warning | A masked job reads over a connection that isn't encrypted, as the server reports it (`--connect`). |
| warning | `shuffle` on an incremental job, whose small chunks leave values near their own rows. |
| warning | An incremental job that [`run --full-refresh`](../concepts/how-it-works.md#deletes) can't replace (no `targetTableStage`, or a files target), so rows deleted from its source stay in the copy, and a scheduled refresh refuses to run. |
| warning | A domain is masked two ways, or under two keys, in one target database, so its masks won't match across the columns that share it. Copies in different target databases may use different keys. |
| warning | A foreign-key column isn't masked exactly like the key it references (strategy, options, domain and key), so the copied references won't match (`--connect`). |
| warning | A foreign key and the key it references are both masked with `shuffle`, which moves values between rows, so the references point at other rows (`--connect`). |
| warning | A job copies only part of a referenced table (a watermark, `WHERE`, `LIMIT` or join), and the referencing job isn't limited to match, so the copy can reference rows it lacks (`--connect`). |
| warning | A job's table references another job's, and it can load first: it doesn't wait through `predecessors`, the other is inactive, or a longer `refresh` sits between them (`--connect`). |
| warning | A database declares foreign keys only in schemas its jobs don't use, so no reference was checked: set `currentSchema`, or qualify the jobs' tables (`--connect`). |
| note | Columns that fall to `defaultStrategy`, by name (`--connect`). |
| note | A target doesn't declare foreign keys its sources do between the tables copied into it, so nothing there stops a row that references nothing; [`verify-references`](copy-a-subset.md#verify-references-checking-the-copys-references) counts them (`--connect`). |
| note | `redact`, or `json` redacting what its fields don't name, which removes identifiers with a recognisable shape but not names. |
| note | A column kept unmasked whose sampled values are UUIDs, which other systems may hold too (`--connect`). |

Without `--connect`, columns are shown as declared. With it, each job's query is run to list the columns it really returns, and its first `--sample` rows (1,000 by default, `0` for none) are read to question kept columns and shared domains by their values. No value appears in the report, its JSON or its errors.

The foreign-key checks read the keys of each target and of the sources copied into it, as [`verify-references`](copy-a-subset.md#verify-references-checking-the-copys-references) does, and match them to jobs by table name. A pair of jobs counts as matched for a partial copy when either query names the other's table, as [`subset`](copy-a-subset.md)'s queries and [tables that reference each other](../concepts/how-it-works.md#tables-that-reference-each-other) do. Whether a job waits for another is decided as a run decides it ([refresh and predecessors](../concepts/how-it-works.md#refresh-and-predecessors)). The `swap` checks read only the target's own keys, and a job whose `postTargetAdhocQueries` name the referencing table is taken to recreate them.

`audit` exits 1 on an error, and with `--strict` on a warning too, so it can gate CI. **Gate it with `--connect`**: offline, `select email as contact` with `contact: keep` passes on its name, where the sampled values give it away. `--format json` writes the report for other tools, `--format html` [one page for a reviewer](#a-report-for-reviewers), and `--output FILE` to a file; `--job` narrows it.


## `coverage`: what the jobs do not cover

`audit` checks the jobs that exist, so it can't see a table nobody wrote a job for, which is exactly what a new release adds to production. `coverage` starts from the database instead, listing every table and what the jobs do with it:

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

A table counts as covered when a job reads rows from it: names it after `FROM` or `JOIN`, directly or through a CTE, derived table or `UNION`. A table named only in a comment, a string or a filtering subquery is not covered. A query reaching a table through a view covers it in fact but not here, so declare it.

For a table nothing covers, `coverage` reads its column names and marks the ones that look like personal data, by the same rules as [`discover`](propose-a-policy.md), including your own from [`discovery.yaml`](propose-a-policy.md#your-own-rules-discoveryyaml). Columns of covered tables aren't read.

**`coverage` exits 1 when any table is NOT COVERED**, so it can follow `run` in CI and fail the build when production grows a table the copy doesn't account for. `--connection` picks the alias when the jobs read from more than one; `--job` narrows which jobs count as covering.


## A report for reviewers

A privacy or compliance reviewer who doesn't run the CLI can be handed either report as one HTML page:

```
bauta audit --connect --format html --output audit.html
bauta coverage --format html --output coverage.html
```

Each opens with a summary, then a section per job (how it masks each column, its key's fingerprint, its findings) or per coverage state.

- **It holds what the text report holds:** names, strategies, fingerprints and findings, never a value or a key.
- **One self-contained file**, fetching nothing, so it reads the same attached to a ticket or archived beside a release.
- **The exit status is the command's**, so the step that writes it can also fail the build.

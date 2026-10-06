# Rotate the masking key


The masking key is a credential, so a security policy usually says to change it on a schedule. Changing it changes every mask, so what a run does next depends on how the copy is loaded.

`run` refuses to start when the key of an **upsert** job, or of an **append** job writing files or Iceberg, changed since that job last completed, because the target still holds rows masked under the old key. A **swap** or **overwrite** job replaces its whole target every run, so it is never affected and needs nothing here.

| The job's policy | What to do |
| --- | --- |
| Does not mask the target's primary key | `bauta run --accept-key-change`. Each row is matched on its unchanged key and rewritten under the new one. |
| Masks the target's primary key | `bauta clear`, then `bauta run --force`. |
| Is incremental, with a `targetTableStage` | `bauta run --full-refresh`, either way: the target is [replaced whole](../concepts/how-it-works.md#deletes) under the new key, with no moment when it's empty. |
| Appends to files or an Iceberg table | Remove what the job wrote (`bauta clear` empties only database tables), then `bauta run --accept-key-change --force`, which loads it again under the new key and records it. Acknowledging the change without removing the old rows leaves a table half of whose rows won't join to the other half. |

**The second case cannot be acknowledged away, and `run` refuses it whatever flags are given.** An upsert matches rows on the primary key. When the key is masked, a new masking key gives every row a new primary key, so the run inserts a second generation of rows beside the first rather than updating it — and where a new key lands on one already there, it overwrites a different row's data. The result is a target holding two key generations at once, whose foreign keys still all resolve, so [`bauta verify-references`](copy-a-subset.md#verify-references-checking-the-copys-references) reports it clean.

`bauta clear` empties the targets children-first and forgets the recorded key, so the next `run` starts from nothing:

```
export MASKING_KEY=<the new key>
bauta clear --yes           # empties the jobs' targets, children first
bauta run --force           # reloads everything under the new key
```

Use `--force` on that run: `clear` leaves the targets empty, and a job still inside its `refresh` window would otherwise be skipped and leave them that way. Run the jobs together rather than one at a time, so that columns sharing a domain are reloaded under the same key and still join.

Rotating the key does not change [`BAUTA_MANIFEST_KEY`](keep-a-manifest.md#what-it-records), which signs manifests. Manifests written under the old masking key stay verifiable, and record the old key's fingerprint.

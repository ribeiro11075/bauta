# Rotate the masking key


The masking key is a credential, so a security policy usually says to change it on a schedule. A new key changes every mask, so what to do depends on how each copy is loaded.

`run` refuses to start when an **upsert** or **append** job's key changed since it last completed, since its target still holds rows masked under the old key. A **swap** or **overwrite** job replaces its whole target, so it needs nothing here.

| The job's policy | What to do |
| --- | --- |
| Does not mask the target's primary key | `bauta run --accept-key-change`. Each row is matched on its unchanged key and rewritten under the new one. |
| Masks the target's primary key | `bauta clear`, then `bauta run --force`. |
| Is incremental, with a `targetTableStage` | `bauta run --full-refresh`, either way: the target is [replaced whole](../concepts/how-it-works.md#deletes) under the new key, with no moment when it's empty. |
| Appends to files or an Iceberg table | Remove what the job wrote (`bauta clear` empties only database tables), then `bauta run --accept-key-change --force`. Acknowledging the change without removing the old rows leaves a table half of which won't join to the other half. |

**`run` refuses the second case whatever flags are given.** An upsert matches on the primary key, and a new masking key gives every masked key a new value, so the run would insert a second generation of rows beside the first, sometimes over other rows' data, leaving a target whose foreign keys all still resolve, so that [`verify-references`](copy-a-subset.md#verify-references-checking-the-copys-references) passes it.

`bauta clear` empties the targets children-first and forgets the recorded key, so the next `run` starts afresh:

```
export MASKING_KEY=<the new key>
bauta clear --yes           # empties the jobs' targets, children first
bauta run --force           # reloads everything under the new key
```

`--force` stops a job inside its `refresh` window leaving its emptied target empty. Run the jobs together, so columns sharing a domain reload under one key and still join.

The masking key is not [`BAUTA_MANIFEST_KEY`](keep-a-manifest.md#what-it-records), which signs manifests, so older manifests stay verifiable, each recording the key fingerprint it was made under.

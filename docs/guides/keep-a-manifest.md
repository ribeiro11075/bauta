# Keep a masking manifest


With `manifest` set in `jobs.yaml`, each run writes a sealed record of what was masked, how, and under which key fingerprint: a file, replaced by each run, or a table, which keeps every run's. It's what an auditor asks for.

```yaml
manifest: ../transaction/manifest.json   # a file, relative to jobs.yaml
manifest:
  connection: warehouse                  # or a table
```

```
bauta verify-manifest                    # the latest; exit 0 intact, 1 altered
bauta verify-manifest --run RUN_ID       # an earlier one, from a table
```

`--manifest FILE` or `--manifest-connection ALIAS` overrides the setting, on `run` and on `verify-manifest`. Set `BAUTA_MANIFEST_KEY` to sign manifests; only a signature shows who wrote one. [The manifest](#what-it-records) describes its content and how verification works.


## What it records

```yaml
manifest: ../audit/manifest.json      # in jobs.yaml; or --manifest FILE on the command line
```

Every run then writes a record of what was masked, how, and under which key fingerprint. It's the artifact an auditor asks for:

```json
{
  "generatedAt": "2026-09-16T13:25:57+00:00",
  "jobs": [
    {
      "job": "maskCustomers",
      "status": "completed",
      "sourceConnection": "prod",
      "targetConnection": "staging",
      "targetTable": "customers",
      "keyFingerprint": "d5930cf83dea",
      "rowCount": 48210,
      "columns": [
        {"column": "id", "strategy": "key", "domain": "customer", "source": "column"},
        {"column": "notes", "strategy": "null", "domain": null, "source": "column"}
      ]
    }
  ],
  "maskedBy": "bauta-rs/0.1.3",
  "tool": {"name": "bauta", "version": "0.1.3"},
  "configuration": {"jobsFile": "configuration/jobs.yaml", "sha256": "9f2c…"},
  "integrity": {
    "algorithm": "sha256",
    "digest": "4be1…",
    "signatureAlgorithm": "hmac-sha256",
    "signature": "0c7a…",
    "signingKeyFingerprint": "71d04ab2e913"
  }
}
```

- `columns` lists the columns the query actually returned, and the policy applied to each. `source` says whether a column was listed in `columns` or fell to `defaultStrategy`.
- A masked job that failed or was skipped is still listed, with its status and no columns. "This copy was not refreshed" belongs in the record too.
- The manifest is written even when the run fails. It never contains a value or the key.
- `configuration` names the jobs file and its SHA-256, so a reviewer can tell which policy produced the run.
- `maskedBy` is `python`, or the native masker and its version.

From Python, `RunResult.maskingManifest(jobsFile.jobs)` returns the manifest before sealing, without `tool`, `configuration` or `integrity`; `sealManifest` adds the last.

### In a table

A file is replaced by each run. To keep every run's manifest, or to keep them with the data they describe, store them in a table instead:

```yaml
manifest:
  connection: warehouse         # an alias in connections.yaml
  table: audit.bauta_manifest   # optional; bauta_manifest by default
```

or `--manifest-connection ALIAS` for one run. Each manifest is stored exactly as it would be written to a file, in 2000-character pieces so one table definition fits every database, under a new run id that the log line names. The table must exist first: [operations.md](../reference/tables.md) has its definition.

`bauta verify-manifest` with no file reads the latest manifest from there, and `--run RUN_ID` picks an earlier one.

**The table is not what makes a manifest trustworthy.** Whoever can write to it can replace a manifest, and recompute its digest to match. Only a [signature](#sealing-and-verifying) shows who wrote one, wherever it's stored.

### Sealing and verifying

Every manifest carries a SHA-256 digest of its own content, which shows it hasn't been edited since it was written. Anyone can recompute a digest, though, so it doesn't show who wrote it. For that, set a signing key and the manifest is also signed with HMAC-SHA256:

```
export BAUTA_MANIFEST_KEY=...      # at least 16 characters; not the masking key
bauta run

bauta verify-manifest       # the manifest jobs.yaml names; or a FILE, or --manifest-connection ALIAS
```

`verify-manifest` exits 0 for an intact manifest (saying whether it was signed), and 1 if it was altered or its signature doesn't match. With `BAUTA_MANIFEST_KEY` set, an unsigned manifest exits 1 too: otherwise an edited manifest could pass by dropping its signature and recomputing its digest. A signed manifest records its key's fingerprint; verifying it without that key exits 2 rather than half-answering. `--manifest-key-variable` reads the key from another variable, on both commands.

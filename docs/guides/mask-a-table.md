# Mask a table


Masking is a stage of a data job. Rows are masked as they stream from source to target, so **unmasked data never reaches the target**, not even a stage table.

```
extract (prod)  →  transform  →  mask  →  load (staging)
```

Since masking runs inside a data job, it also gets streaming, retries, watermarks, `--dry-run` and structured logs.


## A masked job

Add a `masking` section to any data job:

```yaml
maskCustomers:
  active: true
  sourceConnection: prod
  sourceQuery: select * from customers
  targetConnection: staging
  targetTableFinal: customers
  insertStrategy: upsert
  chunkSize: 5000
  masking:
    key: ${MASKING_KEY}
    columns:
      id:          { strategy: key, domain: customer }
      email:       email
      phone:       { strategy: digits, keepLeading: 1 }
      full_name:   fakeName
      birth_date:  { strategy: dateShift, maxDays: 30 }
      notes:       'null'
      created_at:  keep
```

| Field | Required or default | Meaning |
| --- | --- | --- |
| `key` | required | The secret every mask is derived from, at least 16 characters. Read it from the environment. See [the key](#the-key). |
| `columns` | required | Column name → policy. A policy is a strategy name, or a mapping with `strategy`, an optional `domain`, and that strategy's options. |
| `defaultStrategy` | optional | The policy for any column not listed in `columns`. Leave it unset unless you mean it. See [every column must be covered](cover-every-column.md). |

Masking applies to `sourceQuery`'s result columns, after `sourceQueryColumnTransforms`. Normalize values with a transform first (`strip`, `lower`) so that equal values mask equally. Column names match case-insensitively, since Oracle reports them in upper case.

`'null'` needs its quotes, because a bare `null` in YAML means "no value".

`bauta validate` checks the key length, every strategy name and every option, without connecting to anything.


## The key

The key is what stops someone who knows this scheme from hashing likely values, such as common names or every phone number in an area code, and matching them against the masked output.

- **Read it from the environment**: `key: ${MASKING_KEY}`. Never give it a `${NAME:-default}`, and never commit it.
- It must be at least 16 characters. Use a random one: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- **Rotating it changes every mask.** A copy masked under the old key won't join to one masked under the new key. So each masked job's key fingerprint is recorded when it completes, and an **upsert** job whose key has changed since stops the run: its target still holds rows masked under the old key. A `swap` job replaces its whole target, so it just carries on. What to do next depends on whether the policy masks the target's primary key: see [rotating the masking key](rotate-the-key.md).
- The key never appears in logs, errors or the manifest, and pydantic hides it from the configuration's repr. Runs log a **fingerprint** instead: a short, non-reversible identifier. Two runs with the same fingerprint used the same key.

Whoever holds the key can confirm a guess (for example "is this row Alice?") by masking the guess and comparing, so give it the same care as production credentials. [security.md](../concepts/security.md) sets out what masking does and doesn't protect, for a security review.


## Masking in place

To mask a table where it stands, make the source and target the same and load through a stage table:

```yaml
maskCustomersInPlace:
  sourceConnection: staging
  sourceQuery: select * from customers
  targetConnection: staging
  targetTableStage: customers_masked_stage   # same shape, created beforehand
  targetTableFinal: customers
  insertStrategy: swap
  chunkSize: 5000
  masking: ...
```

Masked rows stream into the stage table, and the stage is then swapped with the original. If a run fails before the swap, the original is untouched.

Use `swap` rather than `upsert` for this if any key column is masked. An upsert matches rows by primary key, and a masked key would add new rows instead of replacing the old ones.

`swap` renames tables. Views follow on every database, but foreign keys from other tables, and PostgreSQL's materialized views, don't; see [how a swap works](../concepts/how-it-works.md#how-a-swap-works). For a table that other tables reference, copy into a separate database instead. `audit --connect` reports a swap of a table the target's foreign keys reference as an error.

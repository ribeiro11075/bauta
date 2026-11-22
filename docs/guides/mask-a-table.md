# Mask a table


Masking is a stage of a data job: rows are masked as they stream from source to target, so **unmasked data never reaches the target**, not even a stage table.

```
extract (prod)  →  transform  →  mask  →  load (staging)
```

So masking gets the job's streaming, retries, watermarks, `--dry-run` and structured logs.


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
| `key` | required | The secret every mask is derived from, from the environment. See [the key](#the-key). |
| `columns` | required | Column name → policy: a strategy name, or a mapping with `strategy`, an optional `domain`, and the strategy's options. See [strategies](../reference/strategies.md). |
| `defaultStrategy` | optional | The policy for any column `columns` doesn't name. Leave it unset unless you mean it; see [every column must be covered](cover-every-column.md). |

Masking applies to `sourceQuery`'s result columns, after its transforms, matching names ignoring case. To mask values a database compares as equal alike (padded, or differing in case), give the strategy [`normalize`](../reference/strategies.md#values-held-more-than-one-way-normalize). `'null'` needs its quotes: a bare `null` in YAML means "no value". `bauta validate` checks the key, every strategy and every option, offline.


## The key

The key stops someone who knows this scheme from masking likely values (common names, every phone number in an area code) and matching them against the copy.

- **Read it from the environment**, `key: ${MASKING_KEY}`, never with a `${NAME:-default}`, and never commit it.
- **Make it random**: `python -c "import secrets; print(secrets.token_urlsafe(32))"`. At least 16 characters of 8 different ones are required, and `validate` warns below an estimated 128 bits ([keys](../concepts/security.md#keys)).
- **Rotating it changes every mask**, so a copy masked under the old key won't join to one under the new. Each masked job records its key's fingerprint, and an **upsert** or **append** job whose key has changed stops the run, since its target still holds rows masked under the old key. A `swap` or `overwrite` job replaces its whole target and carries on. See [rotating the masking key](rotate-the-key.md).
- The key never appears in logs, errors or the manifest. Runs log its **fingerprint** instead, a short, non-reversible identifier: two runs with the same fingerprint used the same key.

Whoever holds the key can confirm a guess ("is this row Alice?") by masking it and comparing, so guard it like production credentials. The [security model](../concepts/security.md) sets out what masking does and doesn't protect.


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

Masked rows stream into the stage, which is then swapped with the original, so a run that fails first leaves the original untouched. Use `swap`, not `upsert`, if a key column is masked: an upsert matches by primary key, so a masked key would add rows rather than replace them.

A swap renames tables. Views follow, but other tables' foreign keys and PostgreSQL's materialized views don't ([how a swap works](../concepts/how-it-works.md#how-a-swap-works)), so copy a referenced table into a separate database instead; `audit --connect` reports a swap of one as an error.

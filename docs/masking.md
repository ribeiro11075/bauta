# Masking

Masking is a stage of a data job. Rows are masked as they stream from source to target, so **unmasked data never reaches the target**, not even a stage table.

```
extract (prod)  →  transform  →  mask  →  load (staging)
```

Since masking runs inside a data job, it also gets streaming, retries, watermarks, `--dry-run` and structured logs.

- [A masked job](#a-masked-job)
- [Strategies](#strategies)
- [Domains: keeping joins intact](#domains-keeping-joins-intact)
- [Every column must be covered](#every-column-must-be-covered)
- [The key](#the-key)
- [Masking in place](#masking-in-place)
- [The manifest](#the-manifest)
- [Reviewing policies: `audit`](#reviewing-policies-audit)
- [`coverage`: what the jobs do not cover](#coverage-what-the-jobs-do-not-cover)
- [Proposing a policy: `discover`](#proposing-a-policy-discover)
- [Copying a subset: `subset`](#copying-a-subset-subset)
- [Creating and refreshing the copy](#creating-and-refreshing-the-copy)
- [Generating data instead: `synthesize`](#generating-data-instead-synthesize)
- [Limits](#limits)


## A masked job

Add a `masking` section to any data job:

```yaml
maskCustomers:
  active: true
  sourceDatabase: prod
  sourceQuery: select * from customers
  targetDatabase: staging
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
| `defaultStrategy` | optional | The policy for any column not listed in `columns`. Leave it unset unless you mean it. See [every column must be covered](#every-column-must-be-covered). |

Masking applies to `sourceQuery`'s result columns, after `sourceQueryColumnTransforms`. Normalize values with a transform first (`strip`, `lower`) so that equal values mask equally. Column names match case-insensitively, since Oracle reports them in upper case.

`'null'` needs its quotes, because a bare `null` in YAML means "no value".

`bauta validate` checks the key length, every strategy name and every option, without connecting to anything.


## Strategies

A NULL stays NULL under every strategy except `constant` and `null`.

| Strategy | Result | Options |
| --- | --- | --- |
| `keep` | Unchanged. The explicit way to say a column was reviewed. | none |
| `null` | NULL. The right choice for free text. | none |
| `constant` | `value` in every row, NULLs included. | `value` (required) |
| `hash` | An opaque hex token, e.g. `cust_9f86d081884c7d65`. | `length` (12–64, default 16), `prefix` |
| `email` | Still an email address, e.g. `u9f86d081884c@example.test`. Keyed on the lower-cased address. | `length` (8–40, default 12), `mailDomain` (default `example.test`), `keepDomain` |
| `digits` | Each digit replaced, everything else kept: `+1 (555) 010-9999` → `+1 (831) 402-5517`. Keyed on the digits alone, so formatting doesn't matter. Integers keep their digit count. Digits in any script (full-width `１２３`, Arabic-Indic `١٢٣`) are masked too, and written back in their own script. A value the keeps cover entirely fails the job rather than being copied through: `555-0100` under `keepLeading: 3, keepTrailing: 4`. | `keepLeading`, `keepTrailing` (e.g. `4` for a card number) |
| `number` | A number of the same type and precision, either within `variance` of the original (default `0.1`) or within `min`–`max`. A value the variance would round back to itself, such as a small integer, moves one step instead; zero stays zero. | `min` + `max`, or `variance` (0–1); `decimals` |
| `dateShift` | Moved by a keyed number of whole days, never zero. The shift is keyed on the day, so a date, a timestamp and ISO text of that same day all move to the same day. Times of day are kept. ISO 8601 text, which is how SQLite stores dates, is written back in the same format. `0001-01-01` and `9999-12-31` are kept, since they mean "no date" or "forever"; a date near either is shifted away from it. | `maxDays` (default 30) |
| `key` | A one-to-one mapping, safe for primary and foreign keys. See below. | `charset`: `alphanumeric` (default), `digits`, `hex` |
| `fpe` | Like `key`, but using NIST's FF1 format-preserving encryption, for policies that must name a standard. See below. | `charset`: `alphanumeric` (default), `digits`, `hex`; `strict` |
| `fakeName`, `fakeFirstName`, `fakeLastName`, `fakeCity`, `fakeCompany`, `fakeStreetAddress` | Realistic values from bundled lists. Not unique. | `maxLength`; `locale`, below |
| `redact` | Free text with each recognisable identifier replaced: emails, phone numbers, US SSNs, card numbers and IBANs (both checksum-verified), IPv4 addresses. **Names aren't found.** See below. | `replacement`: `label` (default) or `mask`; `detect`: a list of `email`, `phone`, `ssn`, `card`, `iban`, `ip`; `patterns`: extra regular expressions |
| `shuffle` | The column's values rearranged among rows in the same chunk. **Not anonymization:** every real value is still in the table, and a small chunk barely moves them. It moves values between rows rather than mapping them, so it breaks a key and its references even where both are shuffled alike; `audit` warns. See [limits](#limits). | none |

A value a strategy can't handle fails the job, for example text given to `number`. The error names the column and the value's type, never the value itself.

### `key`

`key` guarantees that different inputs give different outputs. That's what a primary key needs: a hash reduced to a column's width would eventually collide. It is a keyed permutation: a Feistel network with HMAC-SHA256 rounds. That's the same structure as the NIST FF1 and FF3-1 standards, but not a certified implementation of either, and it adds no dependency.

The output has the same shape as the input:

- An integer keeps its sign and number of digits.
- Text keeps its length, and every character that isn't masked stays put. With `alphanumeric`, digits map to digits and letters to letters of the same case. `digits` masks only digits. `hex` masks hex characters the same way — digits to digits, `a-f` to `a-f`, `A-F` to `A-F` — which suits UUIDs and hex tokens, and keeps two spellings of one value apart.
- A UUID object stays a UUID. Its version digit isn't preserved.

`charset` is set once per column rather than detected from each value, because detection could give two different shapes the same output.

**Only ASCII letters and digits are masked**, so a value with letters or digits in another script fails the job rather than being copied through: `Дмитрий`, `王伟`, `José` or `١٢٣` under `alphanumeric`, and digits outside 0-9 under `digits` or `hex`. Other characters (spaces, punctuation, `€`) are kept as they are. Use `hash`, a `fake*` strategy or `null` for names in any script, and the `digits` strategy for numbers written in other digits.

**Values are limited to 256 characters**, or 256 digits for an integer. `key` is for identifiers, and its cost grows with the square of a value's length; a longer value fails the job with a suggestion of `hash`, `redact` or `null`. The same limits apply to `fpe`.

`number` handles ordinary numeric columns. `key` is for identifiers, whose values have to stay distinct.

**A mask can be wider than the column.** Keeping a value's shape is not the same as fitting where it came from:

- `key` keeps an integer's digit count, and a column's range doesn't stop at a digit boundary. A 10-digit value in an `INT` column masks to another 10-digit value, and most of those are above 2147483647 — the load then fails with the database's own "out of range". A `BIGINT` has the same problem at 19 digits. Mask such a key into a wider column, or with `hash` where it need not stay a number.
- `number` varies a value that may already be at its column's limit: `9999999999.99` in a `DECIMAL(12,2)` with the default variance overflows about half the time. Bound it with `min` and `max`, which is what they are for. SQLite is the exception, and not in your favour: it ignores a column's declared precision and stores the wider value as it is.

The mask is the same for a given value, key and domain wherever it appears, so it cannot be narrowed to fit one column without breaking the joins it exists to keep. A job that fails this way logs which of the two is likely responsible.

### `fpe`

`fpe` is `key`'s alternative for when a security review asks for a published algorithm: FF1 from NIST SP 800-38G Rev. 1, with AES-256. It is checked against NIST's sample vectors, and needs the `cryptography` package (`pip install "bauta[fpe]"`; the `oracle` extra already brings it).

- It keeps shapes the way `key` does: integers keep sign and digit count, text keeps its length and every character outside `charset`. FF1 needs one alphabet for every position, so with `alphanumeric` a letter may become a digit, and with `hex` a masked value may mix cases (`2cAB74Ce`); `key` keeps each character's class and case.
- The masking key is turned into an AES key per domain, and the domain goes into FF1's tweak.
- **FF1 needs at least a million possible values**: six digits, five hex characters or four alphanumerics. Shorter values are masked with `key`'s permutation instead, and still never collide with longer ones, since lengths are kept. **`strict: true`** fails the job on a shorter value instead, for policies that require FF1 for every value; the error gives the minimum length, never the value. `audit` notes each `fpe` column without `strict`.
- It is slower than `key`. With two `fpe` columns among six, a million rows run at 11,000 rows a second in pure Python, against 17,000 with `key`; the native masker takes it to 115,000. Repeated values are remembered, as described under [speed](#speed).

Only encryption is implemented. Nothing in the package can reverse a mask.

### Fake data by country

The `fake*` strategies draw from an international mix of names and places by default. `locale` picks one country's names, cities and address layout instead: `en_US`, `en_GB`, `de_DE`, `fr_FR`, `es_ES`, `it_IT`, `nl_NL` or `pt_BR`.

```yaml
columns:
  full_name: { strategy: fakeName, locale: de_DE }          # Lukas Schneider
  street:    { strategy: fakeStreetAddress, locale: fr_FR } # 12 rue des Lilas
```

Leaving `locale` out keeps the original lists, so existing masks don't change.

### `redact`

`redact` is for free text worth keeping, like support tickets, where `null` would lose too much:

```yaml
columns:
  ticket_body: { strategy: redact }
  # Called Ann at [PHONE], card [CARD] was declined, reply to [EMAIL]
  agent_notes: { strategy: redact, replacement: mask, patterns: ['ACC-\d{6}'] }
  # Called Ann at +7 (362) 248-5677, card 4095 9565 2378 1111 was declined, account redacted-b934cf4da0a9
```

- `label` writes `[EMAIL]`, `[PHONE]`, `[SSN]`, `[CARD]`, `[IBAN]`, `[IP]`, or `[REDACTED]` for your own `patterns`.
- `mask` writes keyed values of the same shape instead: the same address always becomes the same masked address, a card keeps its last four digits, an IP becomes `10.x.x.x`.
- Phone detection is broad on purpose: any run of 7–15 digits that isn't a date counts, order numbers included. Leave `phone` out of `detect` where that removes too much.
- **It can't recognise names, addresses written in words, or anything else without a fixed shape.** "Call Maria about her divorce" passes through unchanged. Where text may hold that, use `null`. `audit` notes every `redact` column for this reason.

### Your own strategies

A policy can name a class of your own as `module.path:ClassName`:

```python
# acme/masks.py
from bauta.masking import Strategy

class Initials(Strategy):
    OPTIONS = {'separator': str}                   # option name -> check that returns the value

    def mask(self, value):                          # called for each non-NULL value
        separator = self.options.get('separator', '.')
        suffix = self.keyedHash.digest(str(value).encode()).hex()[:4]
        return separator.join(word[0] for word in value.split()) + separator + suffix
```

```yaml
columns:
  full_name: { strategy: "acme.masks:Initials", separator: "-" }
```

Derive anything random from `self.keyedHash` (`digest`, `below`, `unit`, `permute`), so the mask stays keyed, consistent within its domain, and reproducible. `validate` imports the class and checks its options; the module must also be importable wherever jobs run. The manifest records the strategy by the name the policy used.

If `mask()` depends on nothing but the value, set `CACHEABLE = True` on the class, and repeated values are remembered rather than masked again. It's off by default, since a strategy could depend on something else.

### Speed

**The policy decides throughput, by about sixfold.** A million rows of six columns plus an id, SQLite to SQLite, with the [native masker](#the-native-masker) on one thread (`maskingThreads: 1`), reading, masking and writing overlapped:

| Policy | Rows a second |
| --- | --- |
| `email`, two `hash`, three `keep` | 277,000 |
| two `key` columns, `email`, two `hash`, `digits` | 149,000 |
| two `fpe` columns, `email`, two `hash`, `digits` | 115,000 |
| five `key` columns, one `hash` | 45,000 |

`key` costs the most because it has to be a *permutation*: a Feistel network per value, about thirty times the work of `hash`'s one digest. Where nothing joins on a column, `hash` hides as much far more cheaply.

`key`, `fpe`, `hash`, `email`, `digits` and the `fake*` strategies remember up to 16,384 masked values per column (text up to 256 characters, integers and UUIDs), so foreign keys and low-cardinality columns mask many times faster. The native masker masks each distinct value in a chunk once, and for `key`, `fpe` and the `fake*` strategies remembers up to 65,536 values per column across chunks (text up to 64 bytes, and integers). For how `chunkSize` and latency interact, see [throughput](operations.md#throughput).

### The native masker

`bauta-rs` is an optional extension that masks in Rust. It changes no result, and everything works without it. Install it as an extra:

```
pip install "bauta[native]"
```

Wheels are published for Linux (x86-64 and ARM, glibc 2.17 or newer) and macOS (Apple silicon and Intel), each covering every supported Python. Elsewhere pip compiles it, which needs Rust 1.83 or newer.

**Only the matching version is used.** `bauta-rs` is released with every version of `bauta`, and the extra pins the one that matches. Any other version is ignored with a warning and masking runs in Python, since two versions aren't certain to mask identically, and a difference would reach a deployment as joins that quietly stop matching. `maskingImplementation`, recorded with each job's key fingerprint and in the manifest, says which one masked.

From a clone, `pip install ./mask-rs/py` builds the extension at the checkout's version.

It covers `key`, `fpe`, `hash`, `email`, `digits` and the `fake*` strategies, which is where the time goes; the `fake*` ones pick from the lists Python hands it, so there is one copy of those. Everything else stays in Python: the strategies that are already cheap, `redact`, and [custom strategies](#your-own-strategies), which are Python by definition and mask on one thread whatever `maskingThreads` says. So do values the extension doesn't handle (`Decimal`, `UUID`, dates, and non-ASCII text for the strategies that read characters), so a value Python would refuse still refuses with the same message.

The second policy above, a million rows, SQLite to SQLite:

| Masker | Rows a second |
| --- | --- |
| Python | 17,000 |
| Rust, one thread, reading, masking and writing in turn | 113,000 |
| Rust, one thread, overlapped with the database (the default) | 149,000 |
| Rust, `maskingThreads: auto` (10 threads) | 277,000 |

**The two implementations compute identical masks**, a release requirement: a difference would silently break joins between old and new copies. See [two implementations](security.md#two-implementations). `BAUTA_NATIVE=0` masks in Python even with the extension installed, and the manifest records which one ran as `maskedBy`.


### Masking threads

The native masker can spread each chunk over several cores. Every mask depends on its value alone, so the result is identical for any number of threads; only the time changes. `jobs.yaml`'s [`maskingThreads`](configuration.md#file-level) sets how many threads each job masks with:

| `maskingThreads` | Each job masks with | Use it when |
| --- | --- | --- |
| `1` (default) | One thread. | You haven't measured a need, or the database shares the machine. |
| A number, such as `4` | That many threads, every job. | You want a fixed share. It can't exceed the cores available: `validate` and `run` refuse it. |
| `auto` | The cores available, divided between the jobs running when it starts. | Bauta has the machine to itself, and you want it all used. |

**How `auto` divides the cores.** When a job starts, it gets `cores ÷ jobs running`, counting the jobs already running and those starting with it. A running job keeps its share; cores freed later go to the next job to start. With `workers: 2` on 8 cores, where `a` and `b` run together and `c` waits for both:

| Job | Starts | Jobs running | Threads |
| --- | --- | --- | --- |
| `a` | first | 2 | 4 |
| `b` | with `a` | 2 | 4 |
| `c` | once `a` and `b` finish | 1 | 8 |

**Cores available** are the ones the process may use: on Linux, a container's CPU limit and CPU affinity, not the host's total; on macOS, the core count.

**A number applies to every job.** It's checked against the cores, not multiplied by `workers`: `workers: 4` with `maskingThreads: 4` on 8 cores runs 16 masking threads at once. Only `auto` shares the cores between jobs.

**`BAUTA_MASKING_THREADS`** overrides the setting for one environment, as a number or `auto`, and is checked the same way.

**What never uses more than one thread:** masking without the extension (not installed, or `BAUTA_NATIVE=0`), and strategies the extension doesn't cover, such as `redact`, `shuffle`, `dateShift` and [your own](#your-own-strategies).

**Seeing what it chose.** `bauta validate` prints the plan:

```
masking: bauta-rs 0.1.3, 5 to 10 thread(s) per job (maskingThreads: auto; 10 core(s)): 5 with 2 jobs running, 10 for a job running alone
```

and each masked job logs what it got as it starts:

```
maskCustomers: masking with 4 thread(s) (2 job(s) running, 8 core(s))
```

**When it helps.** Threads help where masking, not the database, sets the pace: wide tables with many masked columns. A million rows of 25 masked columns, SQLite to SQLite on ten cores, from [the native-masking demo](../example/README.md#native-masking):

| `maskingThreads` | Rows a second |
| --- | --- |
| `1` | 25,000 |
| `auto` (10 threads) | 73,000 |

A narrow table gains little, since its time goes to writing.

## Domains: keeping joins intact

Every mask is derived from the key, a **domain**, and the value itself:

```
mask = strategy( HMAC(key, domain, value) )
```

The same value in the same domain always masks the same way, in every table and on every run. So joins survive masking as long as both sides share a domain and a strategy:

```yaml
# customers
id:          { strategy: key, domain: customer }
# orders
customer_id: { strategy: key, domain: customer }
```

The domain defaults to the column's lower-cased name, so `email` in two tables already agrees without any configuration. Set `domain` explicitly whenever the two sides of a relationship have different names.

Masking is also reproducible. Masks don't depend on row order or a random seed, so a re-run or next week's incremental load produces the same values. The one exception is `shuffle`, which depends on how rows fall into chunks.

Numbers are keyed on their decimal text. An id read as an `int` from one database, as a `Decimal` from another, or as `'42'` from a text column therefore masks the same way under `hash`. `key` is stricter, since its output keeps the input's type: pick one type per domain.


## Every column must be covered

**Every column `sourceQuery` returns must appear in `columns`.** Otherwise the job fails before it writes anything, and the error names the columns:

```
MaskingError: column(s) returned by sourceQuery but not in the masking policy: ssn.
Add each one -- `keep` if it needs no masking -- or set defaultStrategy
```

This guards against the most common masking failure: someone adds a column to production, nobody updates the policy, and the column's real values flow into a non-production copy. With `select *`, a new column stops the job instead.

A column named in the policy that the query doesn't return is also an error, since it's almost always a typo that leaves the real column uncovered.

These errors are never retried. `bauta run --dry-run` finds them without loading anything. It runs each masked job's query, reads one row and discards it unexamined.

`defaultStrategy` turns the check off for unlisted columns. Only `'null'` or `constant` keep the safety property, since they discard whatever a new column holds.


## The key

The key is what stops someone who knows this scheme from hashing likely values, such as common names or every phone number in an area code, and matching them against the masked output.

- **Read it from the environment**: `key: ${MASKING_KEY}`. Never give it a `${NAME:-default}`, and never commit it.
- It must be at least 16 characters. Use a random one: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- **Rotating it changes every mask.** A copy masked under the old key won't join to one masked under the new key. So each masked job's key fingerprint is recorded when it completes, and an **upsert** job whose key has changed since stops the run: its target still holds rows masked under the old key. A `swap` job replaces its whole target, so it just carries on. What to do next depends on whether the policy masks the target's primary key: see [rotating the masking key](operations.md#rotating-the-masking-key).
- The key never appears in logs, errors or the manifest, and pydantic hides it from the configuration's repr. Runs log a **fingerprint** instead: a short, non-reversible identifier. Two runs with the same fingerprint used the same key.

Whoever holds the key can confirm a guess (for example "is this row Alice?") by masking the guess and comparing, so give it the same care as production credentials. [security.md](security.md) sets out what masking does and doesn't protect, for a security review.


## Masking in place

To mask a table where it stands, make the source and target the same and load through a stage table:

```yaml
maskCustomersInPlace:
  sourceDatabase: staging
  sourceQuery: select * from customers
  targetDatabase: staging
  targetTableStage: customers_masked_stage   # same shape, created beforehand
  targetTableFinal: customers
  insertStrategy: swap
  chunkSize: 5000
  masking: ...
```

Masked rows stream into the stage table, and the stage is then swapped with the original. If a run fails before the swap, the original is untouched.

Use `swap` rather than `upsert` for this if any key column is masked. An upsert matches rows by primary key, and a masked key would add new rows instead of replacing the old ones.

`swap` renames tables. Views follow on every database, but foreign keys from other tables, and PostgreSQL's materialized views, don't; see [how a swap works](design.md#how-a-swap-works). For a table that other tables reference, copy into a separate database instead. `audit --connect` reports a swap of a table the target's foreign keys reference as an error.


## The manifest

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
      "sourceDatabase": "prod",
      "targetDatabase": "staging",
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
  database: warehouse           # an alias in database.yaml
  table: audit.bauta_manifest   # optional; bauta_manifest by default
```

or `--manifest-database ALIAS` for one run. Each manifest is stored exactly as it would be written to a file, in 2000-character pieces so one table definition fits every database, under a new run id that the log line names. The table must exist first: [operations.md](operations.md#tables) has its definition.

`bauta verify-manifest` with no file reads the latest manifest from there, and `--run RUN_ID` picks an earlier one.

**The table is not what makes a manifest trustworthy.** Whoever can write to it can replace a manifest, and recompute its digest to match. Only a [signature](#sealing-and-verifying) shows who wrote one, wherever it's stored.

### Sealing and verifying

Every manifest carries a SHA-256 digest of its own content, which shows it hasn't been edited since it was written. Anyone can recompute a digest, though, so it doesn't show who wrote it. For that, set a signing key and the manifest is also signed with HMAC-SHA256:

```
export BAUTA_MANIFEST_KEY=...      # at least 16 characters; not the masking key
bauta run

bauta verify-manifest       # the manifest jobs.yaml names; or a FILE, or --manifest-database ALIAS
```

`verify-manifest` exits 0 for an intact manifest (saying whether it was signed), and 1 if it was altered or its signature doesn't match. With `BAUTA_MANIFEST_KEY` set, an unsigned manifest exits 1 too: otherwise an edited manifest could pass by dropping its signature and recomputing its digest. A signed manifest records its key's fingerprint; verifying it without that key exits 2 rather than half-answering. `--manifest-key-variable` reads the key from another variable, on both commands.


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
| warning | A column is kept unmasked although its name suggests personal data (`email`, `ssn`, `phone`, ...), by the built-in rules or [your own](#your-own-rules-discoveryyaml). |
| warning | `defaultStrategy` is `keep`, so any column added to the source later is copied unmasked. |
| warning | A job copies from a database without masking while other jobs mask what they read from it. |
| warning | A masked job reads over a connection that isn't encrypted, as the server reports it (`--connect`). |
| warning | `shuffle` on an incremental job, whose small chunks leave values near their own rows. |
| warning | A domain is masked two ways, or under two keys, in one target database, so its masks won't match across the columns that share it. Copies in different target databases may use different keys. |
| warning | A foreign-key column isn't masked exactly like the key it references (strategy, options, domain and key), so the copied references won't match (`--connect`). |
| warning | A foreign key and the key it references are both masked with `shuffle`, which moves values between rows, so the references point at other rows (`--connect`). |
| error | `watermarkColumn` falls to a `defaultStrategy` that masks it: the watermark is read before masking and kept in run state and logs (`--connect`). A column the policy masks by name is refused by `validate`. |
| warning | A job copies only part of a table that another job's table references (it has a `watermarkColumn`, or its `sourceQuery` has a `WHERE`), and the referencing job isn't limited to match, so the copy can reference rows it lacks (`--connect`). A query counts as partial when it has a `WHERE`, `LIMIT`, `TOP` or `FETCH FIRST`, or joins another table. |
| warning | A job's table references another job's table, and the job can load before the other: it doesn't wait for it through `predecessors`, the other is inactive, or a job on the way has a longer `refresh` (`--connect`). |
| note | Columns that fall to `defaultStrategy`, by name (`--connect`). |

Without `--connect`, columns are shown as declared. With it, each masked query is run for a single row, discarded unexamined, to list the columns it really returns and the policy each one gets.

The foreign-key check reads the foreign keys of each target database and of the sources copied into it, since a copy often declares none. A key's tables are matched to jobs by `targetTableFinal`'s name, without its schema, and a masked job's target columns to its query's columns by position, as the load matches them. A job that doesn't mask copies every column as it is, and so does `keep`; a reference masked with `null` points at nothing, so it can't break.

The check for a parent copied in part uses the same keys and the same matching by table name. It doesn't parse SQL. A pair of jobs counts as matched when either query names the other's table: a child limited by `EXISTS` over its parent, as [`subset`](#copying-a-subset-subset) generates, or a parent that also selects what its children reference, as in [tables that reference each other](design.md#tables-that-reference-each-other). A table that references itself is left to `subset`, which reports it as a cycle. Whether a job waits for another is decided as a run decides it: through active predecessors only, and in the cycles each runs in (see [refresh and predecessors](design.md#refresh-and-predecessors)).

The check on `swap` jobs reads only the keys the target declares, since a key only the source has constrains nothing in the copy. A job whose `postTargetAdhocQueries` name the referencing table is taken to recreate its keys there. See [how a swap works](design.md#how-a-swap-works).

`audit` exits 1 on an error, and with `--strict` on a warning too, so it can gate a CI pipeline. `--format json` writes the same report for other tools, and `--output FILE` writes it to a file. `--job` narrows it.


## `coverage`: what the jobs do not cover

`audit` checks the jobs that exist. It cannot see a table nobody wrote a job for — a table with no job has nothing to audit — and that is exactly what a new release adds to production.

`coverage` starts from the database instead of from the configuration. It lists every table in a source database and says what the jobs do with each:

```
bauta coverage
bauta coverage --database prod --schema sales
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
| not copied, declared | No job reads it, and [`acknowledged`](configuration.md#acknowledged) records why. |
| NOT COVERED | No job reads it, and nothing says that was intended. |

A table counts as covered when a job reading that database names it in its `sourceQuery`. That doesn't parse SQL — it is the same approximation [`audit`](#reviewing-policies-audit) makes for tables that reference each other — so a job whose query reaches a table only through a view covers it in fact but not in this report, and has to be declared.

For a table nothing covers, `coverage` reads its column names and marks the ones that look like personal data, by the same rules as [`discover`](#proposing-a-policy-discover), including your own from [`discovery.yaml`](#your-own-rules-discoveryyaml). Columns of covered tables aren't read.

**`coverage` exits 1 when any table is NOT COVERED**, so it can follow `run` in CI and fail the build when production grows a table the copy doesn't account for. `--database` picks the alias when the jobs read from more than one; `--job` narrows which jobs count as covering.


## Proposing a policy: `discover`

```
bauta discover --database prod --table customers --table orders --target staging --output proposal.yaml
```

For each table, `discover` reads the schema and samples rows (`--sample`, default 1000), then writes a `jobs.yaml` with a proposed policy for every column. Each proposal carries a comment saying what it was based on:

```yaml
    masking:
      key: ${MASKING_KEY}
      columns:
        id: {strategy: keep}  # numeric key (domain customers); use key if the ids themselves are meaningful
        email: {strategy: email}  # name suggests an email address
        phone: {strategy: digits}  # name suggests a phone number
        notes: {strategy: 'null'}  # name suggests free text, which can hold PII anywhere
        status: {strategy: keep}  # no sign of personal data -- review
```

- **Names first, then values.** Column names are matched against [rules](#your-own-rules-discoveryyaml) for common patterns (email, phone, SSN, card, name, address, birth date and so on). A name-based suggestion is dropped if it doesn't fit the column's type, so `place_of_birth` isn't treated as a date. Sampled values are then checked for emails, national identifiers, card numbers (with a Luhn check), IP addresses, UUIDs, dates, phone numbers and long free text.
- **Keys are decided together.** Primary keys, the columns that foreign keys reference, and the foreign-key columns themselves get matching domains, so both ends of a relationship agree. Numeric keys are proposed as `keep`, since surrogate ids reveal little, and text keys as `key`. **`--mask-keys` masks the numeric ones too**, in the domain each relationship shares:

  ```yaml
  id: {strategy: key, domain: customers}          # --mask-keys
  customer_id: {strategy: key, domain: customers} # the other end, same domain
  ```

  Both ends move together, so the copy's references still match. Use it where the ids themselves are meaningful — sequential ids leak how many customers there are, and when each was created — or where the copy's ids must not be production's. Note that masking a key the target uses as its primary key means a later key rotation has to go through `bauta clear`; see [rotating the masking key](operations.md#rotating-the-masking-key).
- **Sampled values stay in memory.** None of them is printed, logged or written.
- **Load settings.** With a separate `--target`, jobs upsert and load parent tables before child tables. Without one, the proposal masks in place through a `<table>_masked_stage` swap.
- **A whole schema at once.** `--all-tables` proposes for every table in the database, instead of naming each with a repeated `--table`; `--schema NAME` lists another schema. Pair it with [`bauta coverage`](#coverage-what-the-jobs-do-not-cover), which starts from the same list and fails on anything the generated jobs then leave out.
- `--output` refuses to overwrite an existing file, so it can't replace a policy that has already been reviewed.

Treat the result as a starting point for review. It isn't a finished policy.

### Your own rules: `discovery.yaml`

The built-in rules are in [`bauta/builtinDiscovery.py`](../bauta/builtinDiscovery.py), and they recognise English column names and US-shaped identifiers. For anything else, such as a national identifier or column names in another language, put rules of your own in `configuration/discovery.yaml` (or name a file with `--rules FILE`):

```yaml
names:                                  # words in column names
- words: [nif, numero_contribuinte]
  policy: {strategy: key, charset: digits}
  reason: a Portuguese tax number
- words: [nome, apelido]
  policy: fakeName
- words: [office_phone]
  policy: keep                          # a switchboard, not a person
  reason: switchboard numbers
values:                                 # regular expressions sampled values must match
- pattern: '[125689]\d{8}'
  policy: {strategy: key, charset: digits}
  reason: looks like a NIF
personalTables: [clientes, utentes]     # a bare `name` column in these is a person's
exclude: [ip]                           # built-in rules to leave out
```

- **Yours come first.** Your rules are tried before the built-in ones, in the order written, and the first that matches wins. So a rule of yours also overrides a built-in one, and `keep` says a column isn't personal after all: above, `office_phone` stays as it is while `home_phone` is still masked.
- **`names`.** A rule matches when any of its `words` is in the column name. Names are split on underscores and camelCase, and also matched run together, so `numero_contribuinte` matches `NumeroContribuinte` and `numero_contribuinte_cliente`. As with the built-in rules, a rule is skipped for a column whose type its policy doesn't fit.
- **`values`.** A rule matches when at least 80% of a column's sampled values match its `pattern` in full: `\d{9}` matches `501234567`, not `NIF 501234567`. Unlike the built-in value rules, which read text only, yours also read integer columns as their digits, since a tax number is often stored as one.
- **`policy`** is a column policy as in `jobs.yaml`: a strategy name, or a mapping with its options. `reason` is the comment written beside the proposal; without one, it says the rule came from `discovery.yaml`.
- **Leaving built-in rules out.** `exclude` names built-in rules to drop, and one name covers a rule's name and value forms both: leaving out `phone` means nothing is taken for a phone number. `builtins: false` drops them all, `personalTables` included, leaving only yours.

| Field | Required or default | Meaning |
| --- | --- | --- |
| `names` | optional | Rules on column names, each with `words`, a `policy` and an optional `reason`. |
| `values` | optional | Rules on sampled values, each with a `pattern`, a `policy` and an optional `reason`. |
| `personalTables` | optional | Words in a table's name that make a bare `name` column in it a person's. |
| `exclude` | optional | Built-in rules to leave out, by the names below. |
| `builtins` | optional, `true` | `false` leaves out every built-in rule. |

| Built-in rule | Recognises |
| --- | --- |
| `email` | email addresses, by name and value |
| `credential` | passwords, secrets, tokens and API keys |
| `nationalId` | SSNs, tax ids, passports and licence numbers, by name; `123-45-6789` by value |
| `card` | card numbers, by name, and by value with a Luhn check |
| `bankAccount` | IBANs, account, routing and sort codes |
| `phone` | phone, mobile and fax numbers, by name and value |
| `firstName`, `lastName`, `fullName` | people's names |
| `userName` | user names and logins |
| `company` | companies and employers |
| `ip` | IP addresses, by name, and IPv4 by value |
| `streetAddress`, `city`, `postalCode` | addresses |
| `birthDate` | dates of birth |
| `compensation` | salaries, income and bonuses |
| `coordinate` | latitudes and longitudes |
| `sensitiveAttribute` | gender, race, ethnicity, religion and nationality |
| `freeText` | notes, comments and descriptions |
| `uuid`, `date` | UUIDs and ISO dates, by value, proposed as `keep` for review |

The same rules, yours included, decide which unmasked columns [`audit`](#reviewing-policies-audit) questions and which columns [`synthesize`](#generating-data-instead-synthesize) fills with realistic values. `bauta validate` checks the file: every pattern must compile, every policy must be valid, and every name in `exclude` must be a built-in rule.


## Copying a subset: `subset`

```
bauta subset --database prod --target staging \
    --root customers --where "created_at >= '2026-01-01'" --mask --output subset/jobs.yaml
```

`subset` generates one data job per table so that the copy is **referentially complete**: every foreign key in a copied row points at a row that was also copied. It reads the foreign keys from the source database's catalog, including composite keys, and follows them in both directions:

- **Down** (skip this with `--no-children`): rows that reference the selected rows. A customer's orders, and those orders' line items.
- **Up** (always): rows that anything selected references. The products those line items point at, and whatever those products point at in turn.

Each job's `sourceQuery` is plain SQL: a `WITH` clause defining each table's selection once, joined by `EXISTS`. It runs unchanged on all six databases (MySQL from 8.0, MariaDB from 10.2). On PostgreSQL and SQLite the selections are marked `MATERIALIZED`, so each is computed once. Jobs load parents before children, so the target can keep its foreign keys enabled. `--mask` adds a proposed policy for each table, as `discover` does.

**Cycles** can't be followed in SQL that works on every database. This includes a table that references itself, like `employees.manager_id`. `subset` reports the cycle and stops. Break it with `--ignore-foreign-key employees.manager_id`.

An ignored key is a key the subset stops following, so the rows it copies may point at rows it didn't. A nullable column doesn't help by itself — the value is still there, still pointing at nothing. Either mask the column to `'null'`, which needs it to be nullable, or leave that foreign key out of the target. `verify-references` counts what is left pointing at nothing either way. Naming any one column of a composite key ignores the whole key.

**Depth is limited.** A subset may follow a chain of up to 16 tables (`customers` → `orders` → `order_items` → ... is a chain of three). MySQL refuses deeper ones, and SQL Server takes seconds to plan them and fails past about 24, so `subset` refuses them up front rather than generating queries that fail. For a deeper schema, root the subset lower down, use `--no-children`, or split it into subsets rooted at different tables.

**Each table is read at a different moment**, by its own job. Rows written to the source between two of those reads can reference rows that weren't copied, and the target's foreign keys will then reject them. Subset from a replica or a snapshot that isn't being written to, or make `--where` exclude recent rows (`created_at < '2026-09-01'`) so late writes fall outside the subset.

The target's tables must already exist. `subset` generates jobs; it doesn't create tables. See the next section for creating them.


## Creating and refreshing the copy

### `schema`: creating the target's tables

```
bauta schema --database prod --target staging --table customers --related --apply
```

`schema` reads the source's tables and creates matching tables in the target, **in the target's own dialect**: an Oracle `NUMBER(12,2)` becomes `NUMERIC(12,2)` on PostgreSQL, and `NVARCHAR(MAX)` on SQL Server becomes `CLOB` on Oracle.

- **What it copies:** columns, nullability, the primary key, foreign keys between the tables being created, and the `UNIQUE` constraints those keys need — a key may reference a unique column that isn't the primary key, and every dialect refuses one with nothing unique behind it. Not indexes, defaults, check constraints, triggers or permissions. A non-production copy rarely needs them, and translating them between databases is where schema tools go wrong.
- **Which tables:** `--table` names them. `--related` adds every table a subset rooted there would copy, which is what the headers of generated subset jobs suggest. `--no-children` narrows that to the tables `--table` references. Each is created under the name you asked for, not the case its catalog happens to hold (Oracle's is upper case), so the jobs that follow find every one of them.
- **Constraint names:** a foreign key keeps its source name where that fits the target's length limit and no other key in the run has taken it; otherwise it gets a numbered suffix (`fk_parent_2`). PostgreSQL and SQLite name constraints per table, while MySQL, MariaDB, Oracle and SQL Server need them unique across the schema.
- **Without `--apply`,** it prints the SQL, or writes it to `--output`, for you to review or hand to a DBA. Each lossy choice is a comment above its table.
- **With `--apply`,** it creates the tables in dependency order and **skips any that already exist**. It never alters or drops anything, so it's safe to re-run.
- **`--stage-suffix _stage`** also creates `<table>_stage` tables for `swap` jobs, with the same columns and key but **no foreign keys**, and none of the unique constraints those keys need: a key follows the table it was declared on, so once the parent is swapped it would check the emptied old table and refuse every row. The consequence is that a swapped table's keys alternate — see [how a swap works](design.md#how-a-swap-works). When `--target` is the source database itself, as for [masking in place](#masking-in-place), only the stage tables are created.
- **`--no-foreign-keys`** leaves foreign keys out. Use it when tables reference each other in a cycle; add those keys yourself once both tables exist.

A few conversions change what a column can hold, and the generated SQL notes each one:

| Source | Target | Becomes |
| --- | --- | --- |
| Oracle `DATE` | anything else | a timestamp, since Oracle's `DATE` includes a time of day |
| MySQL `TIME` | anything | it is a duration, not a time of day — `-838:59:59` is a legal value — and it is written as `[-]HH:MM:SS[.ffffff]` text, which every database parses back. A `TIME` column elsewhere holds only a time of day, so a value outside one is refused as it loads rather than stored as something else |
| any `TIME` | Oracle | `VARCHAR2(32 CHAR)`, since Oracle has no time-of-day type; wide enough for the day-long values MySQL's `TIME` allows |
| a time-zone-aware timestamp | Oracle | `TIMESTAMP WITH TIME ZONE`, which keeps the offset of the session that loads the row, not the source's, so the instant moves unless that session is UTC |
| SQLite `INTEGER` | anything else | that target's `INT`, which is narrower: SQLite stores an integer in up to 8 bytes whatever the column is called |
| MySQL `INT UNSIGNED` | anything | a signed 32-bit integer; values above 2147483647 are refused as they load |
| MySQL `BIGINT UNSIGNED` | anything | a signed 64-bit integer; values above 9223372036854775807 are refused as they load |
| any decimal | sqlite | `TEXT`, which keeps every digit. SQLite has no exact decimal type, and a column declared `DECIMAL(p,s)` holds a float: it would keep about 15 digits and round the rest away as the row is written |
| a decimal declaring no precision | mysql, mariadb, mssql | `DECIMAL(65,30)` or `DECIMAL(38,10)`, which round anything longer |
| a decimal wider than the target allows | mysql, mariadb, oracle, mssql | the widest that target has, so whole digits or decimal places are lost |
| a time-zone-aware timestamp | MySQL, MariaDB | `DATETIME(6)`, and the offset is lost |
| unbounded text in a key | MySQL, SQL Server, Oracle | 255 characters, since those can't index unbounded text |
| a boolean stored as an integer (SQLite, MySQL, Oracle `NUMBER(1)`) | anything | a small integer, since PostgreSQL won't load an integer into `BOOLEAN` |
| a type it doesn't recognize | anything | text |

Both are noted on the table `schema` creates, since MySQL and MariaDB report `unsigned` as part of the column's type. No target has an unsigned integer, so the values above a signed one's range are refused as they load rather than wrapping.

Every combination of the six databases is tested: tables are created on the target and a copy then loads into them.

### `verify-references`: checking the copy's references

```
bauta run && bauta verify-references
```

`verify-references` counts, for each foreign key on a table the jobs load, the rows whose key matches no row of the table it references. It's the check that proves a copy intact, where [`audit`](#reviewing-policies-audit) can only predict from the configuration.

- **Which keys:** those the target declares, and those of the sources copied into it, matched to the copy by table name as `audit` matches them. A declared key doesn't rule orphans out: MySQL loads can turn checks off, and SQL Server, Oracle and PostgreSQL constraints can be disabled or never validated. A key only a source declares is matched to the target's own spelling of each column.
- **Which tables:** each active job's `targetTableFinal`, with every key it declares, and the tables those reference. `--job` narrows it, and names inactive jobs too.
- **What it costs:** one `NOT EXISTS` query per key, which scans the child table once and looks each key up in the parent's primary key. The source is read for its catalog only, never its rows.
- **What it reports:** a count per key, never a value, since some keys are copied as they are. A key whose columns include a NULL points at nothing and isn't counted, as databases don't enforce it. A key that can't be checked, because the target lacks its table or a column, or refused the query, is reported with the reason.
- **Exit status:** 1 if any key has orphaned rows or couldn't be checked, so it can follow `run` in CI. `--format json` and `--output FILE` work as for `audit`.

```
OK       staging: orders (customer_id) -> customers (id): 0 orphaned row(s)
ORPHANS  staging: order_items (product_id) -> products (id) [not declared]: 12 orphaned row(s)
Checked 2 foreign key(s): 1 with orphaned rows, 0 not checked
```

### `clear`: emptying the copy before a refresh

A subset's jobs upsert, so rows from an earlier subset stay unless the copy is emptied first:

```
bauta clear --config subset --dry-run     # which tables, in what order
bauta clear --config subset --yes
bauta run --config subset --force
```

- **What it empties:** `clear` deletes every row from each active job's `targetTableFinal`, child tables before their parents, so the target's foreign keys don't block it. `--job` narrows it to particular jobs.
- **All or nothing:** each database is emptied in one transaction. If one table can't be emptied, for example because a table outside the set still references its rows, nothing is deleted.
- **`DELETE`, not `TRUNCATE`:** PostgreSQL, SQL Server and Oracle refuse to truncate a table that a foreign key references.
- **Nothing happens without `--yes`.** Without it, `clear` exits with status 2.
- **Incremental jobs are refused.** Their stored watermark would survive, so the next run would load only new rows into the empty table.
- **Run with `--force` afterwards**, so a `refresh` window can't leave a cleared table empty.

Between `clear` and the end of the run, the copy is empty or partly loaded. For a copy people use while it refreshes, load into stage tables and `swap` instead.


## Generating data instead: `synthesize`

Some tables can't be copied at all, even masked, and a new system may have no production data yet. `synthesize` fills existing tables with generated rows, using nothing but the target's own catalog:

```
bauta synthesize --database staging --table customers:1000 --table orders:5000 --dry-run
bauta synthesize --database staging --table customers:1000 --table orders:5000 --yes
```

```
customers: 1000 row(s)
  id                           primary key  sequential, from 1
  email                        name         an email address at example.test
  first_name                   name         a first name
  phone                        name         digits shaped like +1 555 010 0000
  birth_date                   name         a birth date between 1940 and 2004
  balance                      type         a decimal with 2 places, sometimes NULL
  sample: {'id': 1, 'email': 'u5e9bbb2e91df@example.test', 'first_name': 'Hugo', ...}
```

- **Keys are unique.** Integer keys continue after the table's current maximum; text keys run `S1`, `S2`, ... after the current row count (`S0001`, `S0002`, ... in a fixed-width column, so each stays distinct at full width); UUID keys are generated.
- **Values continue between runs.** Every generator is indexed by the row's number, counted from the rows already in the table, so a second run neither repeats the first's values nor collides with them. Generated text ends in that number, so a `UNIQUE` column of any reasonable width keeps taking rows.
- **Foreign keys resolve.** Values are drawn from the parent's existing rows, so parents are filled first; `--table` order doesn't matter. A table whose key is made only of foreign keys gets as many rows as its parents allow, which may be fewer than asked.
- **Names drive realism.** Columns whose names suggest personal data (email, names, phone, postal code, birth date, city, company, address...) get realistic values, from the same rules `discover` uses, [your own](#your-own-rules-discoveryyaml) included. Everything else is random within its type: numbers within their precision, text within its length, dates since 2015. Nullable columns are NULL about one time in ten.
- **Reproducible.** The same `--seed` on the same starting tables makes the same rows.
- `--rows` sets the count for any `--table` given without one. Nothing is written without `--yes`.

**Limits:** a table that references itself through a NOT NULL column can't be filled, since its first row would have nothing to point at; a nullable self-reference is left NULL. A `UNIQUE` constraint on a column that isn't the primary key is satisfied only where the column's type has room for one value per row: generated text ends in the row's number, but a name, a short code or a number can repeat. Where the database refuses such a row, `synthesize` says which table refused it and how many rows were inserted before it, since each chunk is already committed. Values are plausible, not statistically like production: there are no correlations between columns, and no skew. Tables must exist first; `schema` creates them from a source's definitions.


## Limits

- **Free text** can hold personal data anywhere in it. `null` or `constant` remove it all. `redact` keeps the text and removes identifiers with a recognisable shape, but not names. `hash` would only replace the text with an opaque token, and `keep` would copy it as it is.
- **Scripts other than Latin.** `key` and `fpe` refuse letters and digits outside ASCII rather than copy them; `digits` and `redact` handle digits in any script. `redact` finds only email addresses written in ASCII.
- **Unique columns** need enough bits to avoid collisions. `hash` enforces a minimum length for that reason. The `fake*` strategies are never unique. For a unique column, use `key`, which never collides.
- **`number` with `variance`** keeps magnitudes realistic, which also reveals them roughly. Use `min`/`max` if the magnitude itself is sensitive.
- **`dateShift`** is keyed on the date, so everyone born on the same day still shares a birthday after masking, and a day's events stay a day's events whether the column holds a date or a timestamp. That's what keeps the data consistent, and it means dates are shifted, not randomized.
- **`shuffle` needs large chunks.** Values only move within a chunk, so a row keeps its own value with probability 1/chunk size, and a chunk of one row isn't shuffled at all. The last chunk of a load and a small incremental run are both small. Don't use `shuffle` on incremental jobs.
- **Masking hides values, not patterns.** Row counts, NULL rates and relationships are all preserved, which is the point, and a combination of kept columns (zip code, birth year and gender) can still identify someone. Review what you `keep`.
- **Hard deletes** aren't propagated by incremental loads, masked or not. See [design.md](design.md#deletes).

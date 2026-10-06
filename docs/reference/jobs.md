# `jobs.yaml`


```yaml
workers: 2
jobs:
  loadOrders:
    active: true
    sourceConnection: app
    sourceQuery: select id, customerId, amount from orders
    targetConnection: warehouse
    targetTableFinal: orders
    insertStrategy: upsert
    chunkSize: 5000
```

## File level

| Field | Required or default | Meaning |
| --- | --- | --- |
| `workers` | required | Worker processes to run jobs concurrently, at least 1. |
| `cycleSleepSeconds` | optional, `0.5` | Pause between cycles under `--forever`. |
| `memory` | optional, `memory.yaml` | Where `run` keeps run state: last runs, watermarks and key fingerprints. A file, or a [table](#tables). `--memory FILE` or `--memory-connection ALIAS` overrides it. See [run state](../guides/run-on-a-schedule.md#run-state). |
| `history` | optional | Where `run` records each job's outcome after every cycle, for `bauta history`. A JSON-lines file, or a [table](#tables). Not recorded when unset. `--history FILE` or `--history-connection ALIAS` overrides it. See [run history](../guides/watch-what-ran.md#run-history). |
| `manifest` | optional | Where `run` writes its [masking manifest](../guides/keep-a-manifest.md#what-it-records), for `bauta verify-manifest`. A file, replaced each run, or a [table](#tables), which keeps every run's. Not written when unset. `--manifest FILE` or `--manifest-connection ALIAS` overrides it. |
| `requireNative` | optional, `false` | Whether a run with masked jobs stops before it starts when the [native masker](../guides/make-it-faster.md#the-native-masker) isn't in use -- not installed, another version, or `BAUTA_NATIVE=0` -- rather than masking in Python, about ten times slower. For a scheduled run with a window to keep. `validate` checks it too. `BAUTA_REQUIRE_NATIVE=1` sets it. |
| `maskingThreads` | optional, `1` | Threads the [native masker](../guides/make-it-faster.md#the-native-masker) masks each job with: `1`, a number up to the cores available, or `auto` to divide half the cores between the jobs running. Results are the same for any count. `BAUTA_MASKING_THREADS` overrides it. See [masking threads](../guides/make-it-faster.md#masking-threads). |
| `defaults` | optional | Settings every job takes unless it names its own. See [defaults](#defaults). |
| `include` | optional | More files of `jobs` and `acknowledged`, as paths or glob patterns relative to this one. See [splitting the jobs across files](configuration.md#splitting-the-jobs-across-files). |
| `acknowledged` | optional | Tables no job copies, on purpose: connection alias, then table, then why. What [`bauta coverage`](../guides/prove-the-copy-is-safe.md#coverage-what-the-jobs-do-not-cover) reads. |
| `jobs` | required | A map of job name to job definition. |

`validate` prints where all three resolve, and how many masking threads a run would use.

Any other field is an error, so a misspelled setting stops `validate` rather than being ignored. The one exception is a key beginning with `x-`, which is left alone for YAML anchors, as docker-compose uses them.

### `acknowledged`

A table nobody wrote a job for is invisible to `audit`, which checks the jobs that exist. `bauta coverage` lists a source database's tables instead and fails on any that no job covers — so leaving one out has to be said out loud, with the reason:

```yaml
acknowledged:
  prod:
    audit_log: internal audit trail, never leaves production
    employees: HR data, out of scope for this copy
```

A reason is required, since the point is the recorded decision rather than the silence. `coverage` also reports a table declared here that the database no longer has, so a stale declaration doesn't quietly cover a table that was dropped and recreated under another name.

### `defaults`

What every job would otherwise repeat. A job that names any of these itself keeps its own value.

| Field | Meaning |
| --- | --- |
| `active`, `refresh` | As in [scheduling](#scheduling). |
| `sourceConnection`, `targetConnection` | As in [extract](#extract) and [load](#load). |
| `insertStrategy`, `chunkSize` | As in [load](#load) and [extract](#extract). |
| `retries`, `retryDelaySeconds`, `timeoutSeconds` | As in [scheduling](#scheduling). |
| `partitions` | As in [load](#load). `auto` is the one to set here: it decides per job, and reads as one stream any job it can't slice. |
| `masking.key` | The key a masked job uses when it gives none of its own. |

`defaults` may set nothing else. A **masking policy stays with its job**: `columns` names what happens to each column, and a reviewer should be able to read that in one place without holding the whole file in their head. `masking.key` is a reference to a secret, not a policy, so it may be shared.

A job with no `masking` block of its own does not grow one from `defaults`. An unmasked job stays visibly unmasked.

```yaml
defaults:
  active: true
  sourceConnection: sourceDb
  targetConnection: targetDb
  insertStrategy: upsert
  chunkSize: 5000
  masking:
    key: ${MASKING_KEY}

jobs:
  maskCustomers:
    sourceQuery: select id, email from customers
    targetTableFinal: customers
    masking:
      columns:
        id: keep
        email: email
```

### Tables

In place of a file path, `memory`, `history` and `manifest` take a table in one of `connections.yaml`'s aliases:

```yaml
memory:
  connection: warehouse
history:
  connection: warehouse
  table: etl.run_history
```

`table` defaults to `bauta_memory`, `bauta_history` or `bauta_manifest`, and `--memory-table`, `--history-table` or `--manifest-table` overrides it. The alias must be in `connections.yaml`, and the table must exist first: [operations.md](tables.md) has their definitions.

## Scheduling

| Field | Required or default | Meaning |
| --- | --- | --- |
| `active` | required | Whether the job runs at all. |
| `refresh` | optional | Minimum minutes between runs. Applies across separate invocations too. A predecessor inside its own refresh window is **not** waited for — see [refresh and predecessors](../concepts/how-it-works.md#refresh-and-predecessors). |
| `predecessors` | optional | Jobs that must complete first. A job whose predecessor fails is **skipped**. Predecessors that form a cycle are a validation error. |
| `retries` | optional, `0` | Extra attempts after a failure, with exponential backoff. See [retries](../concepts/how-it-works.md#retries). |
| `retryDelaySeconds` | optional, `5.0` | The first backoff delay; each subsequent one doubles, up to five minutes. |
| `timeoutSeconds` | optional | The most the job may take, retries included. Past it, the job's process is stopped, the job fails, and its dependents are skipped. See [workers](../concepts/how-it-works.md#workers). |

## Extract

| Field | Required or default | Meaning |
| --- | --- | --- |
| `sourceConnection` | required | An alias from `connections.yaml`. |
| `sourceQuery` | required | The query to extract with. |
| `chunkSize` | required, at least 1 | Rows per batch. Extracts stream, so this is the **memory dial**: peak memory is about `chunkSize` × row width however large the source is — four times that where the [native masker](../guides/make-it-faster.md#the-native-masker) overlaps reading, masking and writing. |
| `watermarkColumn` | optional | Makes the job incremental. See [incremental loads](../concepts/how-it-works.md#incremental-loads). Refused by `validate` on a column the masking policy masks, by name or through `defaultStrategy`: the watermark is read before masking and kept in run state, logs and `bauta jobs`, so it would leak the unmasked value. |
| `watermarkInitial` | required with `watermarkColumn` | The value bound on the first run, before anything is stored. Bound as the type YAML read: write a timestamp unquoted, or PostgreSQL and Oracle refuse the [lookback](../concepts/how-it-works.md#why-the-lookback-window) arithmetic around it. |

A job with `watermarkColumn` must also put a `{{ watermark }}` placeholder in `sourceQuery` and use `insertStrategy: upsert`, or `append` into a files connection. Validation enforces all three.

## Transform

| Field | Required or default | Meaning |
| --- | --- | --- |
| `sourceQueryColumnTransforms` | optional | A map of column name to a list of transformer references, applied in order. |

A reference is `module.path:function_name` — any importable function taking the column value and returning the new one. Further arguments go in parentheses after the name, as Python literals (numbers, quoted strings, `True`, `False`, `None`):

```yaml
sourceQueryColumnTransforms:
  amount:
  - bauta.transform.builtinTransforms:currency
  name:
  - bauta.transform.builtinTransforms:collapseWhitespace
  - bauta.transform.builtinTransforms:truncate(50)
  signup_date:
  - "bauta.transform.builtinTransforms:parseDate('%d/%m/%Y')"
```

Quote a reference whose arguments contain `: `, `#` or a leading quote, as YAML would otherwise read them. `validate` checks that each reference imports and that its arguments fit the function, so a missing or misspelled argument fails there rather than on the first row. Only literals are accepted, so a reference can't run code.

These ship with the package, in `bauta.transform.builtinTransforms`. Every one passes NULL through unchanged, except `defaultIfNull`, and raises on a value it can't convert rather than guessing.

| Transform | Result |
| --- | --- |
| `upper`, `lower`, `title` | Case changed: `title` gives `Ann-Marie O'Neil`. |
| `strip` | Leading and trailing whitespace removed. |
| `collapseWhitespace` | Stripped, with every run of whitespace inside turned into one space. |
| `removeAccents` | `Zoë Müller` → `Zoe Muller`, so accented and plain spellings match. Letters like `ß` and `ø` are kept. |
| `truncate(maxLength=255)` | At most `maxLength` characters: `truncate(50)`. |
| `padLeft(width, fill='0')` | Filled on the left to `width` characters: `padLeft(5)` turns `42` into `00042`. |
| `replace(old, new='')` | Every `old` replaced: `replace('-')` removes hyphens. |
| `regexReplace(pattern, replacement='')` | A regular-expression replacement; `\1` refers to a group. |
| `digitsOnly` | Only the digits: `+1 (555) 010-9999` → `15550109999`. |
| `nullIfBlank` | NULL for empty or whitespace-only text. |
| `nullIf(*values)` | NULL for any of the listed values: `nullIf('N/A', -1)`. |
| `defaultIfNull(default)` | `default` in place of NULL: `defaultIfNull('unknown')`. |
| `currency(symbol='$', decimals=2)` | `1234.5` → `$1,234.50`, `-5` → `-$5.00`; `currency('€')`, `currency('¥', 0)`. |
| `roundNumber(digits=0)` | Rounded half away from zero, keeping the value's type; a negative `digits` rounds to tens, hundreds and so on. |
| `toInteger` | An integer from text or a whole number. Blank text is NULL; `1.5` raises rather than being cut short. |
| `toDecimal` | An exact decimal from text or a number; `0.1` stays exactly `0.1`. Blank text is NULL. |
| `toBoolean` | True or false from `Y`/`N`, `yes`/`no`, `true`/`false`, `t`/`f`, `on`/`off`, `1`/`0`. Blank text is NULL; anything else raises. |
| `booleanToYN` | `Y` or `N`, for single-character flag columns. |
| `parseDate(format='%Y-%m-%d')` | A date from text, by a [strptime format](https://docs.python.org/3/library/datetime.html#format-codes). Dates pass through; datetimes lose their time. |
| `parseDateTime(format='%Y-%m-%d %H:%M:%S')` | A datetime from text; `%z` in the format keeps the UTC offset. |
| `formatDate(format='%Y-%m-%d')` | A date, datetime or time as text. |
| `epochSecondsToDate` | A date from Unix seconds, in UTC whatever the server's timezone. |
| `epochSecondsToDateTime`, `epochMillisecondsToDateTime` | A timezone-aware UTC datetime from Unix seconds or milliseconds. |
| `toString` | Text: dates as ISO 8601, bytes decoded as UTF-8. |
| `toJson` | A document or list as JSON text, with sorted keys; text passes through as it is. |

Transforms apply to **`sourceQuery`'s own result columns**, not the target's. Naming a column the query doesn't return fails before anything is written. A transformer that raises fails the job; the error names the column and the value's type, never the value.

## Load

| Field | Required or default | Meaning |
| --- | --- | --- |
| `targetConnection` | required | An alias from `connections.yaml`. |
| `targetTableFinal` | required | The table to load: `table`, or `schema.table` for one outside the connection's current schema. For a files connection, the table's directory under `root`, such as `crm/customers`: no absolute path, no `..`, and no part beginning with `_` or `.`, which engines reading a directory tree skip, and bauta keeps for its staging. |
| `insertStrategy` | required | `swap` or `upsert` into a database; `append` or `overwrite` into a files connection; `append`, `overwrite` or `upsert` into Iceberg — below. |
| `targetTableStage` | required for `swap` | A staging table with the same shape, emptied before each load, so it must be a different table from `targetTableFinal` (compared ignoring case). For `swap`, it must be in the same schema as `targetTableFinal`. |
| `targetColumns` | optional | Target column names matching `sourceQuery`'s SELECT list **by position**. |
| `preTargetAdhocQueries` | optional | SQL run on the target before any write, the stage load included. |
| `postTargetAdhocQueries` | optional | SQL run on the target after the load. |
| `targetColumnTypes` | optional; files and Iceberg only | Column → type, for a column whose values don't settle its type, or settle it as something else: `string`, `binary`, `bool`, `int8` to `int64`, `uint8` to `uint64`, `float32`, `float64`, `decimal(precision,scale)` up to 38 digits, `date`, `time`, `timestamp` (without a time zone) or `timestamptz` (an instant, written in UTC). See [column types](../concepts/how-it-works.md#column-types). |
| `targetKey` | optional; Iceberg only | The columns an `upsert` matches rows by, which a table bauta creates is given as its identifier fields. An existing table's own identifier fields serve without it; given both, they must agree. |
| `singleFile` | optional, `false`; files only | Write an `overwrite` job's table as one file, `<targetTableFinal>.parquet`, replaced whole each run, rather than a directory of parts. For small tables and handoffs to something that wants one file. |
| `partitions` | optional; a database only, but `auto` anywhere | `{column: id, count: 8}`: read, mask and write the job as `count` slices at once, each a range of the numeric `column` the query returns, each with connections of its own. At least 2, and no more than the `maxConcurrentJobs` of either connection; refused for DuckDB. The job succeeds only if every slice does. **`count: auto`** chooses the count as the job starts, and **`partitions: auto`** the column too: the target's primary key. Either reads as one stream a job it can't slice, and is never refused. See [partitions](../concepts/how-it-works.md#partitions) and [choosing automatically](../guides/make-it-faster.md#partitions-auto). |

`targetTableStage`, the adhoc queries and `partitions` are for a table in a database; `targetColumnTypes` for files and Iceberg; `singleFile` for files; `targetKey` for Iceberg. Each given to another kind of target is an error.

- **`swap`** loads `targetTableStage`, then swaps it with `targetTableFinal` by renaming the two. The target is replaced wholesale. See [how the swap works](../concepts/how-it-works.md#how-a-swap-works) for what renaming means for views and on Oracle.
- **`append`** (files) adds the run's parts to the table's directory, beside those already there. With a watermark it is an incremental export, and like any append-only table it can hold a row twice: see [duplicates](../concepts/how-it-works.md#appends-and-duplicate-rows).
- **`overwrite`** (files) publishes the whole table as a new snapshot, `snapshot=<run>/` in the table's directory, and keeps the newest `keepSnapshots`. Readers pick the newest complete one; see [reading a snapshot](../concepts/how-it-works.md#reading-an-overwrite-jobs-table).
- **Into Iceberg**, `append` adds the run's rows and `overwrite` replaces the table's, each in one commit, and `upsert` merges them by `targetKey` or the table's identifier fields, a commit per row group. See [Iceberg tables](../concepts/how-it-works.md#iceberg-tables).
- **`upsert`** inserts or updates by the target's declared primary key — from `targetTableStage` if set, otherwise straight from the extract. UNIQUE constraints aren't part of the match. A target without a primary key fails the job before anything is written; `bauta run --dry-run` checks for one too.

## Mask

| Field | Required or default | Meaning |
| --- | --- | --- |
| `masking` | optional | Masks rows between the extract and the load. Its fields are `key`, `columns` and `defaultStrategy`, all documented in [masking.md](../guides/mask-a-table.md#a-masked-job). |
| `unmasked` | optional, `false` | Says this job copies its rows as they stand, and that somebody decided so. Cannot be set beside `masking`. |

```yaml
masking:
  key: ${MASKING_KEY}
  columns:
    id: keep
    email: email
    customerId: { strategy: key, domain: customer }
```

Masking runs after transforms, on `sourceQuery`'s result columns. **Every column the query returns must be listed**, or the job fails before writing anything. See [masking.md](../guides/mask-a-table.md) for the strategies, domains and the key.

### Copying without masking

A job with no `masking` block copies every column as it stands, which `bauta audit` reports, since it is a choice a reviewer has to see:

- a column whose name suggests personal data is an **error**, naming the columns and why;
- otherwise it is a **warning**.

`unmasked: true` says the job was reviewed and copies as it stands, the way `keep` says it of a single column. The warning then goes, the audit report shows the job as `not masked, declared with \`unmasked\``, and a column that still looks like personal data is reported as a warning rather than an error.

## Load details

**Into a files connection, `targetColumns` names the columns** the query's result is written as, in its order. Without it they are the query's own names. Two names differing only in case are refused, since several engines can't tell them apart.

**`targetColumns` is purely positional.** Left unset, `sourceQuery` must select every column of `targetTableFinal` in that table's own order. Real column names in the wrong order load data into the wrong columns *without any error*, since both sides are valid; a wrong count fails at the database.

**Column names are quoted** in the statements a load writes, so a reserved word such as `rank` or `order` works as a column. Each name is first matched to the target's own spelling, ignoring case, so `targetColumns: [job]` still finds Oracle's `JOB`; a name the table doesn't have fails the job before anything is written, and so does one that matches two columns differing only in case, until it's spelled exactly.

**Table names are quoted too**, so `targetTableFinal: group` loads into a table named for a reserved word. A name written plainly means what it means without quotes, so `orders` finds Oracle's `ORDERS` and PostgreSQL's `orders`; to name a table those two would fold differently — Oracle's lower-case `"orders"`, PostgreSQL's `"Orders"` — write it in quotes, in that database's own style (`"..."`, or `` `...` `` on MySQL and MariaDB, `[...]` on SQL Server), and that spelling is kept as it is. A schema is quoted apart from the table, so `sales.group` stays two names. The same holds for `targetTableStage`, for the tables `clear` empties, and for the names `schema`, `subset` and `discover` write into what they generate. `sourceQuery` and the adhoc queries are yours, and are written into SQL as given.

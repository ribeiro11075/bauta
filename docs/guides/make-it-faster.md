# Make it faster


Three things decide how fast a job moves rows, in this order.

**The masking policy**, by about fivefold. `key` is expensive because it must be a permutation; `hash` hides as much for a thirtieth of the work wherever a column needn't stay one-to-one. See [speed](#what-each-strategy-costs).

**The [native masker](#the-native-masker)**, about ten times faster on the same policy, with identical results. On a wide table, where masking sets the pace, it can also spread each chunk over several cores ([`maskingThreads`](#masking-threads)).

**`chunkSize`, for latency rather than throughput.** Locally, chunks from 500 to 200,000 rows finish the same job within 7% of each other. What a chunk costs is a round trip: 25 ms away, a million rows take 123 seconds at `chunkSize: 500` and 8.8 at `10000`. Latency stops mattering once a chunk's masking outlasts its round trips:

```
chunkSize  >  2 x latency / per-row masking cost
```

That is a few thousand rows at 5 ms, about six thousand at 25 ms. Cheap policies need *larger* chunks, having less work to hide the wait behind.

To see which of these sets a job's pace, [`bauta history`](watch-what-ran.md#where-the-time-went) shows the seconds each run spent reading, masking and writing, and [`bauta bench`](#measure-a-job) measures reading and masking against your own source before anything is copied.

If a job is still slow, look at the database: the target's indexes and constraints during a bulk load, and a stage table (`targetTableStage`) so the final table is written once. And for one very large table, where one connection is the limit, read it as several [partitions](#partitions) at once.


## What each strategy costs

A million rows of six columns plus a kept id (a reference, an integer, an email, a hex token, a session string, a phone number), SQLite to SQLite, with the [native masker](#the-native-masker) on one thread, on a ten-core M1 Pro. `python benchmarks/masking.py` measures it again (see [benchmarks](../project/development.md#benchmarks)):

| Policy | Rows a second |
| --- | --- |
| `email`, two `hash`, three `keep` | 333,000 |
| two `key` columns, `email`, two `hash`, `digits` | 131,000 |
| two `fpe` columns, `email`, two `hash`, `digits` | 268,000 |
| five `key` columns, one `hash` | 74,000 |

`key` costs the most because it has to be a *permutation*: a Feistel network per value, about thirty times the work of `hash`'s one digest. Where nothing joins on a column, `hash` hides as much far more cheaply. `fpe` is a permutation too, but FF1's ten AES rounds cost less natively than `key`'s forty-odd digests.

Repeated values are remembered per column (up to 16,384 in Python, and 65,536 across chunks in the native masker for `key`, `fpe` and `fake*`), so foreign keys and low-cardinality columns mask many times faster.


## The native masker

`bauta-rs` is an optional extension that masks in Rust. It changes no result, and everything works without it. Install it as an extra:

```
pip install "bauta[native]"
```

Wheels are published for Linux (x86-64 and ARM, glibc 2.17 or newer) and macOS (Apple silicon and Intel), each covering every supported Python. Elsewhere pip compiles it, which needs Rust 1.83 or newer.

**Only the matching version is used.** `bauta-rs` is released with every `bauta`, and the extra pins the match. Another version is ignored, since two versions aren't certain to mask identically, and a difference would surface as joins that quietly stop matching. The manifest records which implementation masked.

**Upgrading `bauta` alone leaves the extension behind**, so a run with masked jobs stops before it starts, naming the version to install; [`requireNative: false`](../reference/jobs.md#file-level) or `BAUTA_REQUIRE_NATIVE=0` masks in Python instead. Where the extension was never installed, or `BAUTA_NATIVE=0`, a run masks in Python and says so; `requireNative: true` stops it there too.

From a clone, `pip install ./mask-rs/py` builds the extension at the checkout's version.

It covers `key`, `fpe`, `hash`, `email`, `digits`, `number`, `redact` (but its custom `patterns`) and the `fake*` strategies, which is where the time goes. Everything else stays in Python: the already-cheap strategies, [custom strategies](../reference/strategies.md#your-own-strategies), and values the extension doesn't handle (dates, UUIDs, non-ASCII text where characters matter), which are handed back one by one, so a value Python would refuse still refuses with the same message.

The second policy above, a million rows, SQLite to SQLite:

| Masker | Rows a second |
| --- | --- |
| Python | 13,600 |
| Rust, one thread, reading, masking and writing in turn | 100,000 |
| Rust, one thread, overlapped with the database (the default) | 132,000 |
| Rust, `maskingThreads: auto` (5 threads) | 329,000 |

**The two implementations compute identical masks**, a release requirement; see [two implementations](../concepts/security.md#two-implementations).


## Masking threads

The native masker can spread each chunk over several cores. Every mask depends on its value alone, so the result is identical for any number of threads; only the time changes. `jobs.yaml`'s [`maskingThreads`](../reference/jobs.md#file-level) sets how many threads each job masks with:

| `maskingThreads` | Each job masks with | Use it when |
| --- | --- | --- |
| `1` (default) | One thread. | You haven't measured a need, or the database shares the machine. |
| A number, such as `4` | That many threads, every job. | You want a fixed share. It can't exceed the cores available: `validate` and `run` refuse it. |
| `auto` | Half the cores available, divided between the jobs running when it starts. | Bauta has the machine to itself, and you want masking as fast as the job can use it. |

**Why half.** Masking only has to keep up with reading and writing, which need cores of their own, as may the database. On a wide table on ten cores, four masking threads copied as fast as six or eight, and ten were slower than any.

**How `auto` divides the cores.** When a job starts, it gets `half the cores ÷ jobs running`, counting the jobs already running and those starting with it, and at least one. A running job keeps its share; cores freed later go to the next job to start. With `workers: 2` on 8 cores, where `a` and `b` run together and `c` waits for both:

| Job | Starts | Jobs running | Threads |
| --- | --- | --- | --- |
| `a` | first | 2 | 2 |
| `b` | with `a` | 2 | 2 |
| `c` | once `a` and `b` finish | 1 | 4 |

**Cores available** are the ones the process may use: on Linux, a container's CPU limit and CPU affinity, not the host's total; on macOS, the core count.

**With partitions**, slices may take over `auto`'s threads: see [`partitions: auto`](#partitions-auto).

**A number applies to every job**, not shared: `workers: 4` with `maskingThreads: 4` on 8 cores runs 16 masking threads. Only `auto` divides the cores.

**`BAUTA_MASKING_THREADS`** overrides the setting for one environment, as a number or `auto`, and is checked the same way.

**What never uses more than one thread:** masking without the extension (not installed, or `BAUTA_NATIVE=0`), and strategies the extension doesn't cover, such as `shuffle`, `dateShift`, `redact` with `patterns`, and [your own](../reference/strategies.md#your-own-strategies).

**Seeing what it chose.** `bauta validate` prints the plan:

```
masking: bauta-rs 0.2.4, 2 to 5 thread(s) per job (maskingThreads: auto; 10 core(s)): 2 with 2 jobs running, 5 for a job running alone
```

and each masked job logs what it got as it starts:

```
maskCustomers: masking with 4 thread(s) (2 job(s) running, 8 core(s))
```

**When it helps.** Threads help where masking, not the database, sets the pace: wide tables with many masked columns. A million rows of 25 masked columns, SQLite to SQLite on ten cores, from [the native-masking demo](../../example/README.md#native-masking):

| `maskingThreads` | Rows a second |
| --- | --- |
| `1` | 31,000 |
| `auto` (5 threads) | 106,000 |

A narrow table gains little, since its time goes to writing.


## Partitions

One job reads, masks and writes on one connection to each database, however large its table. [`partitions`](../concepts/how-it-works.md#partitions) divides it into slices of a numeric column, each with connections and threads of its own, all loading the same table:

```yaml
partitions:
  column: id
  count: 4
```

A masked `swap` copy, PostgreSQL to PostgreSQL in Docker on the job's ten-core machine, the second policy above, one masking thread per slice; every run wrote the same table, byte for byte:

| Masker | Partitions | Time | Rows a second |
| --- | --- | --- | --- |
| native, 2,000,000 rows | none | 15.4 s | 130,000 |
| | 2 | 8.0 s | 250,000 (1.9 times) |
| | 4 | 5.0 s | 403,000 (3.1 times) |
| | 8 | 4.8 s | 415,000 (3.2 times) |
| Python, 300,000 rows | none | 25.4 s | 11,800 |
| | 4 | 25.0 s | 12,000 (1.0 times) |

Past four slices the machine had no cores left: the database server and the job shared the same ten. With the source, the target and the job on machines of their own, the ceiling is whichever of them runs out first.

**It pays where the database waits, not where Python masks.** Slices overlap their reading and writing, mostly waits on a socket. They are threads, and Python masking holds the interpreter lock, so without the [native masker](#the-native-masker) a masked job gains nothing, as the table shows.

**Size it to the databases.** Each slice is another connection to each, counted against `maxConcurrentJobs`; beyond what the servers serve at once, more slices only queue.

**Partition on an indexed column that doesn't change**, such as the primary key: each slice then reads only its range, and no row moves between slices while the job runs. Slices are as even as the column's values are spread.

### `partitions: auto`

Rather than choosing a column and a count for each job, let each job choose as it starts:

```yaml
defaults:
  partitions: auto              # every job; or on one job, or {column: id, count: auto} to name the column
```

**The column** is the target table's primary key, where it is one numeric column the query returns: what a slice reads by range from an index, and what doesn't change while it does.

**The count** is the fewest of:

| Limit | Why |
| --- | --- |
| The job's share of the cores: half of them, divided between the jobs running when it starts | The same share `maskingThreads: auto` reckons, spent once -- see below |
| The places its source and target connections have free (`maxConcurrentJobs`) | Each slice opens a connection to each |
| One slice per 250,000 rows, reckoned from the span of the key | A small table gains less than another pair of connections costs |
| One, for a masked job without the native masker | Its slices would take turns at Python's interpreter lock, and gain nothing (the table above) |

A job it can't slice is read as one stream, with a log line saying why: no single numeric primary key the query returns, a query SQL Server won't wrap (one holding `WITH` or `ORDER BY`), a SQLite target, or files and Iceberg. So `auto` never stops a job, and can sit under `defaults`.

**With `maskingThreads`.** Where `maskingThreads: auto` gave a job no more threads than it has slices, each slice masks on its own thread, so the job's share of the cores is spent once; with fewer slices than threads, the slices share the pool. A number set for `maskingThreads` is kept, and the slices share it.

The log says what each job chose and why:

```
partitions: reading events as 4 slices of id at once, set by its share of the cores (4)
partitions: reading customers as one stream: its target has no primary key to slice by
```


## Measure a job

`bauta bench` reads each job's query from its real source, transforms and masks it as `run` would, and writes nothing. It says how fast each stage went, and which one would set a run's pace:

```
bauta bench --job maskCustomers --rows 200000
```

```
masker: bauta-rs 0.2.4, 1 thread(s)
Reading and masking were measured in turn, and nothing was written. A run overlaps them, so whichever is slower sets its pace,
unless writing to the target is slower still.

JOB                                ROWS  CONNECT  READ ROWS/S  MASK ROWS/S  WAITED  SETS THE PACE
maskCustomers                  200,000+     0.04      412,000      134,000     0.0  masking

+ the query returns more rows than were read; --rows reads more.
```

- **`--rows`** (default 100,000) is how many rows of each query to read, to the next whole chunk. A query that returns fewer is read whole, and its row count has no `+`.
- **`--job`** names a job to measure, and repeats; without it, every active job is.
- **`READ ROWS/S`** covers running the query and fetching its rows; **`MASK ROWS/S`** transforms and masking, on the threads a job running alone would get; **`CONNECT`** opening the connection.
- **It doesn't measure the write.** Where reading and masking are fast but a run is slow, the target is the limit; `bauta history` shows its `WRITE` seconds.
- **An incremental job** is read from its `watermarkInitial`, as its first run or a full refresh would read it. **A job with partitions** is read as one stream.
- **It reads production**, as a run does, and the source's [read limit](#limiting-what-a-job-reads) applies: `WAITED` is seconds it held `bench` back. `--format json` gives the same as JSON, with the seconds of each stage.

It exits 1 if a job couldn't be measured, naming why, and measures the others.


## Limiting what a job reads

`maxRowsReadPerSecond` on a connection caps how many rows a second jobs read from it, all of them together:

```yaml
production:
  type: postgresql
  ...
  maxRowsReadPerSecond: 50000
```

- **One budget for the connection**, shared by every job and [partition](#partitions) reading it, across processes: two jobs at once get about half each.
- **A chunk at a time.** A job waits after each chunk for as long as its rows owe, so the rate holds over a few chunks; a pause earns at most a second's worth.
- **It holds back reading only.** With the native masker, masking and writing go on during the wait, recorded as `WAITED` in [history](watch-what-ran.md#where-the-time-went).
- **Sampling commands aren't limited** (`discover`, `audit --connect`, `coverage`), nor are writes; `bauta bench` is.

Choose it from what the source can spare: a limit below what a job could read makes it take longer, by exactly `WAITED`.


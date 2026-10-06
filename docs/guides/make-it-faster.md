# Make it faster


Three things decide how fast a job moves rows, in this order.

**The masking policy**, by about fivefold. `key` is expensive because it must be a permutation; `hash` hides as much for a thirtieth of the work wherever a column needn't stay one-to-one. See [speed](#what-each-strategy-costs).

**The [native masker](#the-native-masker)**, about ten times faster on the same policy, with identical results: a million rows of six masked columns, two of them `key`, take under 8 seconds with it and 74 without. See [speed](#what-each-strategy-costs) for the conditions. On a wide table, where masking rather than the database sets the pace, it can also spread each chunk over several cores ([`maskingThreads`](#masking-threads)): 25 masked columns went from 31,000 rows a second on one thread to 106,000 on the five `auto` gives ten cores.

**`chunkSize` — for latency, not throughput.** On a local database, chunks from 500 rows to 200,000 finish the same job in 8.6 to 9.2 seconds. What a chunk costs is a round trip: against a database 25 ms away, a million rows take 123 seconds at `chunkSize: 500` and 8.8 at `10000`. Latency stops mattering once a chunk's masking outlasts its round trips:

```
chunkSize  >  2 x latency / per-row masking cost
```

— a few thousand rows at 5 ms, about six thousand at 25 ms. Cheap policies need *larger* chunks, having less work to hide the wait behind. With the native masker, reading, masking and writing also overlap; a job then holds four chunks.

If a job is still slow, look at the database: the target's indexes and constraints during a bulk load, and a stage table (`targetTableStage`) so the final table is written once. And for one very large table, where one connection is the limit, read it as several [partitions](#partitions) at once.


## What each strategy costs

**The policy decides throughput, by about fivefold.** A million rows of six columns plus an id, SQLite to SQLite, with the [native masker](#the-native-masker) on one thread (`maskingThreads: 1`), reading, masking and writing overlapped. The columns are a ten-character reference (`C` and nine digits), a ten-digit integer, an email address, a twelve-character hex token, a session string and a phone number; the id is kept. Ten cores, an M1 Pro; `python benchmarks/masking.py` measures it again (see [benchmarks](../project/development.md#benchmarks)):

| Policy | Rows a second |
| --- | --- |
| `email`, two `hash`, three `keep` | 333,000 |
| two `key` columns, `email`, two `hash`, `digits` | 131,000 |
| two `fpe` columns, `email`, two `hash`, `digits` | 268,000 |
| five `key` columns, one `hash` | 74,000 |

`key` costs the most because it has to be a *permutation*: a Feistel network per value, about thirty times the work of `hash`'s one digest. Where nothing joins on a column, `hash` hides as much far more cheaply. `fpe` is a permutation too, but FF1's ten AES rounds cost less natively than `key`'s forty-odd digests.

`key`, `fpe`, `hash`, `email`, `digits` and the `fake*` strategies remember up to 16,384 masked values per column (text up to 256 characters, integers and UUIDs), so foreign keys and low-cardinality columns mask many times faster. The native masker masks each distinct value in a chunk once, and for `key`, `fpe` and the `fake*` strategies remembers up to 65,536 values per column across chunks (text up to 64 bytes, and integers). For how `chunkSize` and latency interact, see [throughput](make-it-faster.md).


## The native masker

`bauta-rs` is an optional extension that masks in Rust. It changes no result, and everything works without it. Install it as an extra:

```
pip install "bauta[native]"
```

Wheels are published for Linux (x86-64 and ARM, glibc 2.17 or newer) and macOS (Apple silicon and Intel), each covering every supported Python. Elsewhere pip compiles it, which needs Rust 1.83 or newer.

**Only the matching version is used.** `bauta-rs` is released with every version of `bauta`, and the extra pins the one that matches. Any other version is ignored with a warning and masking runs in Python, since two versions aren't certain to mask identically, and a difference would reach a deployment as joins that quietly stop matching. `maskingImplementation`, recorded with each job's key fingerprint and in the manifest, says which one masked.

**Upgrading `bauta` alone leaves the extension behind**, and every masked job about ten times slower, with only a warning in the log to say so. Where a run has a window to keep, set [`requireNative: true`](../reference/jobs.md#file-level) in `jobs.yaml` (or `BAUTA_REQUIRE_NATIVE=1`): a run with masked jobs then stops before it starts, saying why the extension isn't in use, and `bauta validate` fails the same way.

From a clone, `pip install ./mask-rs/py` builds the extension at the checkout's version.

It covers `key`, `fpe`, `hash`, `email`, `digits`, `number` and the `fake*` strategies, which is where the time goes; the `fake*` ones pick from the lists Python hands it, so there is one copy of those. `number` repeats Python's decimal arithmetic digit for digit, integers, floats and `Decimal`s alike, and hands back what it would have to guess at: a mask that comes out as zero, whose sign Python keeps, and values too long or too far from 1. Everything else stays in Python: the strategies that are already cheap, `redact`, and [custom strategies](../reference/strategies.md#your-own-strategies), which are Python by definition and mask on one thread whatever `maskingThreads` says. So do values the extension doesn't handle (`Decimal` outside `number`, `UUID`, dates, and non-ASCII text for the strategies that read characters), so a value Python would refuse still refuses with the same message.

The second policy above, a million rows, SQLite to SQLite:

| Masker | Rows a second |
| --- | --- |
| Python | 13,600 |
| Rust, one thread, reading, masking and writing in turn | 100,000 |
| Rust, one thread, overlapped with the database (the default) | 132,000 |
| Rust, `maskingThreads: auto` (5 threads) | 329,000 |

**The two implementations compute identical masks**, a release requirement: a difference would silently break joins between old and new copies. See [two implementations](../concepts/security.md#two-implementations). `BAUTA_NATIVE=0` masks in Python even with the extension installed, and the manifest records which one ran as `maskedBy`.


## Masking threads

The native masker can spread each chunk over several cores. Every mask depends on its value alone, so the result is identical for any number of threads; only the time changes. `jobs.yaml`'s [`maskingThreads`](../reference/jobs.md#file-level) sets how many threads each job masks with:

| `maskingThreads` | Each job masks with | Use it when |
| --- | --- | --- |
| `1` (default) | One thread. | You haven't measured a need, or the database shares the machine. |
| A number, such as `4` | That many threads, every job. | You want a fixed share. It can't exceed the cores available: `validate` and `run` refuse it. |
| `auto` | Half the cores available, divided between the jobs running when it starts. | Bauta has the machine to itself, and you want masking as fast as the job can use it. |

**Why half.** Masking only has to keep up with the job's reading and writing, which need cores of their own, and so may the database. On a wide table on ten cores, four masking threads copied as fast as six or eight, and all ten were slower than any of them: the extra threads took cores the reading and writing needed. The other half is left to them.

**How `auto` divides the cores.** When a job starts, it gets `half the cores ÷ jobs running`, counting the jobs already running and those starting with it, and at least one. A running job keeps its share; cores freed later go to the next job to start. With `workers: 2` on 8 cores, where `a` and `b` run together and `c` waits for both:

| Job | Starts | Jobs running | Threads |
| --- | --- | --- | --- |
| `a` | first | 2 | 2 |
| `b` | with `a` | 2 | 2 |
| `c` | once `a` and `b` finish | 1 | 4 |

**Cores available** are the ones the process may use: on Linux, a container's CPU limit and CPU affinity, not the host's total; on macOS, the core count.

**With partitions**, slices may take over `auto`'s threads: see [`partitions: auto`](#partitions-auto).

**A number applies to every job.** It's checked against the cores, not multiplied by `workers`: `workers: 4` with `maskingThreads: 4` on 8 cores runs 16 masking threads at once. Only `auto` shares the cores between jobs. A number can still use every core, where you have measured that it pays.

**`BAUTA_MASKING_THREADS`** overrides the setting for one environment, as a number or `auto`, and is checked the same way.

**What never uses more than one thread:** masking without the extension (not installed, or `BAUTA_NATIVE=0`), and strategies the extension doesn't cover, such as `redact`, `shuffle`, `dateShift` and [your own](../reference/strategies.md#your-own-strategies).

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

A masked `swap` copy, PostgreSQL to PostgreSQL in Docker on the same ten-core machine as the job: an id and six columns, two masked with `key`, one with `email`, two with `hash` and one with `digits`; chunks of 5,000, one masking thread per slice; best of two runs, interleaved. Every run wrote the same masked table, byte for byte:

| Masker | Partitions | Time | Rows a second |
| --- | --- | --- | --- |
| native, 2,000,000 rows | none | 15.4 s | 130,000 |
| | 2 | 8.0 s | 250,000 (1.9 times) |
| | 4 | 5.0 s | 403,000 (3.1 times) |
| | 8 | 4.8 s | 415,000 (3.2 times) |
| Python, 300,000 rows | none | 25.4 s | 11,800 |
| | 4 | 25.0 s | 12,000 (1.0 times) |

Past four slices the machine had no cores left: the database server and the job shared the same ten. With the source, the target and the job on machines of their own, the ceiling is whichever of them runs out first.

**It pays where the database waits, not where Python masks.** Slices overlap their reading and writing, which spend most of their time waiting on a socket, and the slower the target writes, the more there is to overlap. Slices are threads of the job's process, and masking in Python holds the interpreter lock, so they take turns at it: a masked job without the native masker gains nothing, as the table shows. The [native masker](#the-native-masker) releases the lock, so slices mask at once too.

**Size it to the databases.** Each slice is another connection reading from the source and another writing to the target, and the job holds a place in each connection's `maxConcurrentJobs` for every one. Beyond what the servers can serve at once, more slices only queue there.

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

A job it can't slice is read as one stream, the query as it is, with a line in the log saying why: no primary key, a key of several columns, a key the query doesn't return or that isn't a number, a query the database won't read as a derived table (SQL Server refuses one holding `WITH` or `ORDER BY`), a target in SQLite, which commits one write at a time, or files and Iceberg, which one writer publishes. `auto` never stops a job that runs without it, so it can sit under `defaults` -- the one setting for partitions that can.

**With `maskingThreads`.** One masking thread masks on the calling thread, so each slice masks on its own, all at once; more than one share one pool, which a job's slices queue for. So where `maskingThreads: auto` gave a job no more threads than it has slices, each slice masks on its own thread instead: as many maskers, without the queue, and the job's share of the cores spent on slices once rather than on slices and threads both. With fewer slices than threads, the pool masks for all of them. A number set for `maskingThreads` is kept as it is, and the slices share that many threads.

A job holds the places its connections had free, up to its share, from the moment it starts, and may use fewer once it has looked at the key, so jobs started after it don't take places it then needs. The log says what it chose and what set the count:

```
partitions: reading events as 4 slices of id at once, set by its share of the cores (4)
partitions: reading customers as one stream: its target has no primary key to slice by
```

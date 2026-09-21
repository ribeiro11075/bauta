# bauta-rs

The optional native masker for [Bauta](https://github.com/ribeiro11075/bauta).

Bauta masks data on its way from production to a copy. Masking `key` and
`fpe` columns costs tens of microseconds a value in Python, most of it spent in
the interpreter rather than in cryptography. This computes the same masks in
Rust, about ten times faster on a whole job on one core, and can use
several.

It is optional. Bauta works without it, and produces identical output
either way.

## Installing

```
pip install "bauta[native]"
```

The extra installs the `bauta-rs` released with your version of `bauta`, which
is the only one Bauta uses; any other is ignored with a warning. Wheels cover
Linux (x86-64 and ARM) and macOS (Apple silicon and Intel) on every supported
Python. Elsewhere pip compiles it, which needs Rust 1.83 or newer.

## Layout

| Path | What it is |
| --- | --- |
| `core/` | The constructions. No Python dependency, so they are testable without an interpreter. |
| `py/` | The PyO3 layer: conversions in, results out, and every unsafe boundary. |
| `vectors/reference.json` | What the Python implementation produces, recorded. The contract between the two. Its `pythonOnly` section records the strategies with no port, so Python can't change those masks unnoticed either; nothing here reads it. |
| `generate_vectors.py` | Regenerates that file from the Python implementation. |

## Building

From a clone, with Rust 1.83 or newer.

```
cargo test --release
cd py && maturin build --release
pip install ../target/wheels/bauta_rs-*.whl
```

`--release` matters for the tests: two of them measure SHA-256 and AES
throughput to catch a backend that has silently fallen back to software, and a
debug build is indistinguishable from one. `cargo test` covers `core/`; `py/`
needs a Python interpreter to link, so it is tested from Python, by
`tests/masking/test_nativeMasking.py`.

## Threads, and remembering masks

A chunk's distinct values are masked across a thread pool, whose size the
Python layer sets per process (`setThreads`) from `jobs.yaml`'s
`maskingThreads`; each mask depends on its value alone, so the count changes no
result. `availableCores()` reads a container's CPU quota, which Python's
`os.cpu_count()` doesn't. `key`, `fpe` and the `fake*` strategies also remember
masks across chunks. The extension allocates through mimalloc: the system
allocators serialise masking's many small allocations across threads.

## Machine integers where they fit

`key`'s Feistel network and `fpe`'s FF1 rounds each have two paths: one in
`u64` halves, taken wherever the values fit -- any `key` domain up to 2\*\*128,
and FF1 halves of up to 19 decimal digits, which is every identifier in
practice -- and the original in `BigUint` for anything wider. Both hash and
encrypt exactly the same bytes; the first only skips allocating a big integer
every round, which was 40% of a `key` mask and three quarters of an `fpe` one.
Unit tests run both paths over every width the fast one takes and require the
same result, and the vectors cover each side of both boundaries.

Integers cross the Python boundary the same way: as an `i64` through the C API
where they fit, and as a `BigInt` otherwise. The extension is built against
the stable ABI (`abi3`), where PyO3 converts a `BigInt` by calling
`int.to_bytes` and `int.from_bytes` -- a Python call per value, made while
every masking thread waits for the results.

## The rule

**Python is the reference.** This crate exists to be faster, not to be
different. Where the two disagree, Python is right.

That is not a style preference. Bauta's masks are deterministic and keyed,
so a difference between the two implementations would not surface as a wrong
answer — it would surface as a changed key, months later, as joins between an
old copy and a new one quietly stopping matching. The key fingerprint would not
change, because the key did not.

So:

- Every covered strategy is checked against `vectors/reference.json`, over a
  corpus chosen for boundaries rather than volume: the lengths where a Feistel
  half stops fitting a machine word, domains of exactly 2\*\*128, the maximum
  identifier length, single-character alphabets, mixed-case hex, and every
  refusal with its exact message.
- Anything whose behaviour depends on Python's own Unicode rules is not
  reimplemented. Non-ASCII text, `str.isspace()` when an address is stripped,
  digits normalised across scripts — those values are handed back, and Python
  masks them.
- FF1 is checked against NIST SP 800-38G's sample vectors, and the keyed hash
  against RFC 4231.

## What it covers

`key`, `fpe`, `hash`, `email`, `digits`, `number`, and the `fake*` strategies,
which pick from the lists Python passes in when a masker is built -- so the
lists are defined once, in Python, and the vectors record them. `number`
(core/src/number.rs) reproduces Python's `decimal` arithmetic at 60 digits for
the operations it uses, and hands back zeros, non-finite values and anything
that would take Python's rounding past what it reproduces. Everything else
stays in Python: `redact` needs lookbehind that Rust's regex engine doesn't
offer, `shuffle`, `dateShift`, `keep`, `null` and `constant` are already
cheap, and custom strategies are Python by definition.

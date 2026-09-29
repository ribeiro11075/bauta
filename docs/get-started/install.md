# Install


Python 3.10 or newer. Choose the drivers you need as extras; each is loaded only when a connection uses it.

```
pip install "bauta[postgresql,oracle]"
```

| Extra | Installs | Needs besides pip |
| --- | --- | --- |
| `mysql`, `mariadb` | mysql-connector-python | nothing |
| `postgresql` | psycopg 3, with its own libpq | nothing |
| `oracle` | oracledb, in thin mode | nothing — no Oracle client |
| `mssql` | pymssql | nothing |
| `sqlite` | Python's own `sqlite3` | nothing |
| `duckdb` | duckdb, with pyarrow for fast loads | nothing; one process at a time per file, see [DuckDB](../reference/connections.md#duckdb) |
| `files` | pyarrow, to write Parquet, CSV and JSON Lines, locally or to S3, GCS or Azure | nothing; see [files](../reference/connections.md#files) |
| `iceberg` | pyiceberg, with its Glue and SQL catalogs | nothing; see [Iceberg](../reference/connections.md#iceberg) |
| `fpe` | cryptography, for the `fpe` masking strategy | nothing; `oracle` already brings it |
| `native` | `bauta-rs`, the native masker (below) | nothing on Linux (x86-64, ARM) or macOS; elsewhere, [Rust](https://rustup.rs) 1.83 or newer |
| `all` | every driver above | nothing |

**The native masker (optional).** `bauta-rs` masks in Rust: about ten times the throughput on one core, with identical masks. It can also mask on several cores: `jobs.yaml`'s `maskingThreads` is `1` by default, a number up to the cores available, or `auto` to divide half the cores between the jobs running (see [masking threads](../guides/make-it-faster.md#masking-threads)). `pip install "bauta[postgresql,native]"` installs the version that matches, which is the only one Bauta uses. Without it, everything works, only slower. See [the native masker](../guides/make-it-faster.md#the-native-masker).

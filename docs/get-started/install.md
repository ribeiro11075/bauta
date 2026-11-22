# Install


Python 3.10 or newer. Choose the drivers you need as extras; each is loaded only when a connection uses it.

```
pip install "bauta[postgresql,oracle]"
```

Nothing besides pip is needed, except where the table says so.

| Extra | Installs |
| --- | --- |
| `mysql`, `mariadb` | mysql-connector-python; the server must be MySQL 8.0.19 or later |
| `postgresql` | psycopg 3, with its own libpq |
| `oracle` | oracledb, in thin mode |
| `mssql` | pymssql |
| `sqlite` | nothing: Python's own `sqlite3` |
| `duckdb` | duckdb, with pyarrow for fast loads |
| `files` | pyarrow, to read and write Parquet, CSV and JSON Lines, locally or in S3, GCS or Azure |
| `iceberg` | pyiceberg, with its Glue and SQL catalogs |
| `fpe` | cryptography, for the `fpe` masking strategy |
| `native` | `bauta-rs`, the native masker (below); prebuilt for Linux (x86-64, ARM) and macOS, built elsewhere with [Rust](https://rustup.rs) 1.83 or newer |
| `all` | every driver above |
| `studio` | FastAPI, Uvicorn, SQLAlchemy and argon2, for [Studio](../guides/studio.md), the web console, whose web app ships built in the package |

**The native masker (optional).** `bauta-rs` masks in Rust: about ten times the throughput on one core, with identical masks, and it can use several cores ([masking threads](../guides/make-it-faster.md#masking-threads)). `pip install "bauta[postgresql,native]"` installs the matching version, the only one Bauta uses. Without it everything works, only slower. See [the native masker](../guides/make-it-faster.md#the-native-masker).

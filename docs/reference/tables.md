# State tables


Run state, history and manifests each need their table to exist before a run uses it. These definitions are `DATABASE_MEMORY_SCHEMA`, `DATABASE_HISTORY_SCHEMA` and `DATABASE_MANIFEST_SCHEMA` in the package, and their types work on all seven databases. Run state can't be kept in DuckDB, which lets one process at a time open a file; history and manifests can. See [DuckDB](connections.md#duckdb). Times are seconds since 1970.

```sql
CREATE TABLE bauta_memory (
    job VARCHAR(255) PRIMARY KEY,
    last_run DOUBLE PRECISION,
    watermark_value VARCHAR(255),
    watermark_type VARCHAR(32)
    )

CREATE TABLE bauta_history (
    run_id VARCHAR(36) NOT NULL,
    job VARCHAR(255) NOT NULL,
    status VARCHAR(16) NOT NULL,
    row_count NUMERIC(19),
    attempts INT,
    started_at DOUBLE PRECISION,
    finished_at DOUBLE PRECISION,
    error VARCHAR(2000),
    read_seconds DOUBLE PRECISION,
    mask_seconds DOUBLE PRECISION,
    write_seconds DOUBLE PRECISION,
    throttled_seconds DOUBLE PRECISION,
    PRIMARY KEY (run_id, job)
    )

CREATE TABLE bauta_manifest (
    run_id VARCHAR(36) NOT NULL,
    part INT NOT NULL,
    written_at DOUBLE PRECISION NOT NULL,
    content VARCHAR(2000) NOT NULL,
    PRIMARY KEY (run_id, part)
    )
```

The four `_seconds` columns of `bauta_history` hold how long each completed job was busy [reading, masking and writing](../guides/watch-what-ran.md#where-the-time-went), and waiting on a read limit. A table made by 0.2.4 or earlier works without them, and records the rest; add all four to keep them too:

```sql
ALTER TABLE bauta_history ADD read_seconds DOUBLE PRECISION
ALTER TABLE bauta_history ADD mask_seconds DOUBLE PRECISION
ALTER TABLE bauta_history ADD write_seconds DOUBLE PRECISION
ALTER TABLE bauta_history ADD throttled_seconds DOUBLE PRECISION
```

A manifest is stored in pieces of `content`, in `part` order, because its JSON can be longer than any one text type every database shares. See [manifests in a table](../guides/keep-a-manifest.md#in-a-table).

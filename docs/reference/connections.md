# `connections.yaml`


One entry per connection alias. Jobs refer to connections by these aliases, as `sourceConnection` and `targetConnection`.

```yaml
warehouse:
  type: postgresql
  database: analytics
  host: ${WAREHOUSE_HOST}
  user: etl
  password: ${WAREHOUSE_PASSWORD}
```

Each type takes only the settings that apply to it. A setting that belongs to another type -- a `serviceName` on a PostgreSQL connection, a `host` on a SQLite one -- is an error naming the types it belongs to, rather than accepted and ignored.

| Setting | Types | Required or default | Meaning |
| --- | --- | --- | --- |
| `type` | all | required | `postgresql`, `mysql`, `mariadb`, `mssql`, `oracle`, `sqlite` or `duckdb`, `files` for tables of files in a directory or a cloud bucket, or `iceberg` for Iceberg tables; see [files](#files) and [Iceberg](#iceberg) for their settings |
| `database` | postgresql, mysql, mariadb, mssql | required | The database name. |
| `path` | sqlite, duckdb | required | The database file, or `:memory:`, relative to `connections.yaml`. It must exist, except for a job's target and `schema --apply`, which create it, so a misspelled source path is an error rather than an empty database. SQLite connections enforce declared foreign keys. See [DuckDB](#duckdb) for what differs there. |
| `host`, `user` | the five server types | required | |
| `password` | the five server types | required, unless `passwordCommand` is set | Held as a secret, so it never appears in a log line or a traceback. |
| `passwordCommand` | the five server types | optional | A command whose output is the password, run at every connection, for credentials that expire; see [passwords that expire](#passwords-that-expire). |
| `port` | the five server types | optional | The driver's standard port when omitted. |
| `serviceName` / `sid` | oracle | exactly one | Oracle is reached by service name or SID; it has no `database`. |
| `currentSchema` | postgresql, oracle, duckdb | optional | The schema unqualified table names resolve in (PostgreSQL's `search_path`, alone; Oracle's `CURRENT_SCHEMA`; DuckDB's `schema`). Elsewhere, qualify names as `schema.table`. |
| `maxConcurrentJobs` | all | optional, no limit | The most jobs that may use this connection at once, whatever `workers` allows; a job past it waits while jobs on other connections start. Always 1 for `duckdb`. |
| `maxRowsReadPerSecond` | the seven databases | optional, no limit | The most rows a second all jobs together may read from this database, every partition included. See [limiting what a job reads](../guides/make-it-faster.md#limiting-what-a-job-reads). |
| `requireMasking` | all | optional, `false` | No job may read from or write to this connection without a masking policy. See [requiring masking](#requiring-masking). |
| `readOnly` | databases | optional, `false` | Nothing is written here: a job loading into it, or run state, history or a manifest kept in it, is refused, as is every write a command would make. See [read-only connections](#read-only-connections). |
| `statementTimeoutSeconds` | server databases | optional | The longest one statement may run before the server stops it; measured differently by each database ([statement timeout](#statement-timeout)). |
| `requireEncryption` | server databases | optional, `false` | Every session must be one the server reports as encrypted, or it is closed before a statement runs. See [requiring encryption](#requiring-encryption). |
| `options` | the seven databases | optional | Extra keyword arguments for the driver's `connect()`, for anything the settings above don't cover; for `duckdb`, DuckDB's own settings, such as `memory_limit` and `threads`. See below. |

Any other setting is an error, so a misspelled one stops `validate`. (Before 0.1.9 this file was `database.yaml`; its old setting names are refused as unknown.)

## DuckDB

```yaml
analytics:
  type: duckdb
  path: /srv/data/analytics.duckdb
  options: {memory_limit: 4GB}
```

Install it with `pip install "bauta[duckdb]"`. DuckDB works like any other database, with four differences, each refused with an error rather than left to fail part-way:

- **One job at a time per file**, since DuckDB lets one process open a file and every job runs in its own (`maxConcurrentJobs` is always 1; jobs on other connections still run alongside). Two aliases for one file are refused. Anything else holding the file open makes a job wait up to a minute, then fail saying why.
- **No run state in DuckDB**: the run holds that connection open throughout. `history` and `manifest` can be DuckDB tables.
- **No files outside the database.** DuckDB's SQL could otherwise read or write any file the process can, attach databases or load extensions, so a query in the jobs files could reach the machine as bauta's account. That is turned off on every connection; set `options: {enable_external_access: true}` where queries are meant to read files. A [files connection](#files) reads tables of files without it.
- **No swap of a table with an index or in a foreign key** (a primary key is fine): such a `swap` job fails before renaming anything; use `upsert`. `clear` empties such tables one transaction each, so a clear stopped part-way is finished by running it again.

## Files

```yaml
lake:
  type: files
  root: s3://acme-lake/masked      # or gs://..., az://..., abfss://..., or a directory
  format: parquet
  requireMasking: true
```

A directory of tables of files, on this machine or in S3, Google Cloud Storage or Azure, for a data lake or a handoff. Install it with `pip install "bauta[files]"`. A job writing to it takes `insertStrategy: append` or `overwrite` ([load](jobs.md#load); [what a run leaves where](../concepts/how-it-works.md#files-as-a-target)); a job reading it queries its tables with SQL ([files as a source](../concepts/how-it-works.md#files-as-a-source)). Commands that need a catalog (`discover`, `subset`, `schema`, `synthesize`, `coverage`, `clear`) refuse it, and run state, history and manifests can't be kept in it.

| Setting | Required or default | Meaning |
| --- | --- | --- |
| `root` | required | A directory (created if missing, relative to `connections.yaml`), `s3://bucket[/prefix]`, `gs://bucket[/prefix]`, or on Azure `az://container[/prefix]` with `accountName` or `abfss://container@account.dfs.core.windows.net[/prefix]`. A bucket or container must exist. Each job's `targetTableFinal` is a path under it. |
| `format` | `parquet` | `parquet`, `csv` or `ndjson` (JSON Lines: one JSON object per line). See [formats](../concepts/how-it-works.md#formats) for how each type is spelled in the two text formats. |
| `compression` | `zstd` for Parquet, `gzip` for text | Parquet: `zstd`, `snappy` (for older readers), `gzip` or `none`. CSV and JSON Lines: `gzip` or `none`, the whole file compressed, and named `.gz` so readers know. |
| `delimiter` | `,` | CSV's alone: one character, not a quote or a line break. |
| `fileSize` | `256MB` | Where a part is closed and the next begun, compressed: bytes, or `256MB`, `256MiB`. Snowflake loads best from 100 to 250 MB files. At most `5GiB` on S3, and `256MiB` on Azure with pyarrow before 19. |
| `rowGroupSize` | `128MB` | How much of a table is held in memory, before compression, and written at once: one Parquet row group. Larger compresses better and lets engines skip more; smaller holds less. |
| `keepSnapshots` | `2` | How many complete snapshots of an `overwrite` job's table stay, the newest included. |
| `readOnly` | `false` | Jobs may read it, and any job writing to it is refused. Mark a production bucket so. |
| `endpoint` | any cloud | Another service speaking the same API -- MinIO or Cloudflare R2 for S3, an emulator for the others -- as a URL, `https://...` or `http://...`. |

`requireMasking` and `maxConcurrentJobs` apply as to any connection. Two connections may share a root, to write some tables in one format and some in another.

**Credentials.** Without settings, bauta finds credentials as each cloud's command-line tool would. Prefer that: nothing to rotate, and the machine's or pod's own identity where it has one.

| Cloud | Found without settings | Settings, each optional |
| --- | --- | --- |
| S3 | The AWS default chain: `AWS_ACCESS_KEY_ID` and friends, a profile, SSO, an EC2 instance role, an ECS task role, IRSA on Kubernetes. | `region` (else the bucket's, asked of S3); `accessKeyId` with `secretAccessKey`, and `sessionToken` for temporary keys; `roleArn`, a role to assume with whichever credentials the rest find, for a bucket in another account. |
| Google Cloud Storage | Application Default Credentials: the key file `GOOGLE_APPLICATION_CREDENTIALS` names, `gcloud auth application-default login`, a GCE or GKE service account, workload identity. | `serviceAccount`, an account to impersonate with them; `anonymous` set to `true`, for a public bucket or an emulator. |
| Azure | The Azure default chain: `AZURE_CLIENT_ID` and friends in the environment, workload identity on Kubernetes, a managed identity, `az login`. | One of: `accountKey`; `sasToken` (pyarrow 20 or newer); a service principal, `clientId` with `clientSecret` and `tenantId` (pyarrow 21 or newer). `accountName` names the storage account for an `az://` root. |

Secrets -- `secretAccessKey`, `sessionToken`, `accountKey`, `sasToken`, `clientSecret` -- are held as `password` is, never in a log line; give them as `${VARIABLES}`.

**What the credentials need** on the root's prefix: write, read, delete and list (on S3 `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`, `s3:ListBucket`, `s3:AbortMultipartUpload`; on GCS `roles/storage.objectAdmin`; on Azure `Storage Blob Data Contributor`). `bauta run --dry-run` checks by writing, listing and deleting an object. On S3, add a lifecycle rule aborting incomplete multipart uploads after a day: a process killed mid-upload leaves parts S3 bills for.

## Iceberg

```yaml
lake:
  type: iceberg
  catalog: glue
  warehouse: s3://acme-lake/warehouse
  namespace: masked
  requireMasking: true
```

Iceberg tables, found through a catalog, which Athena, Snowflake, Databricks, BigQuery, Trino and Spark all read. A run lands as one commit, and `upsert` works as against a database. Install it with `pip install "bauta[iceberg]"`. It is a target only: a job names `namespace.table` (or `table` in the connection's `namespace`) as its `targetTableFinal`, with `insertStrategy: append`, `overwrite` or `upsert` ([load](jobs.md#load); [Iceberg tables](../concepts/how-it-works.md#iceberg-tables)).

| Setting | Required or default | Meaning |
| --- | --- | --- |
| `catalog` | required | `glue` (AWS), `rest` -- the Iceberg REST protocol: BigLake on GCP, Databricks Unity Catalog, Snowflake Open Catalog (Polaris), S3 Tables, Lakekeeper, Nessie -- or `sql`, a catalog kept in a database of your own, SQLite or PostgreSQL, for a setup with no catalog service. |
| `uri` | required for `rest` and `sql` | The REST catalog's URL, or the SQL catalog's database as SQLAlchemy names it: `sqlite:////srv/lake/catalog.db`, `postgresql+psycopg://user:password@host/catalog`. |
| `warehouse` | required for `glue` and `sql` | Where a table the catalog creates keeps its files: a directory (relative to `connections.yaml` if the path is), `s3://`, `gs://`, `az://` (with `accountName`) or `abfss://`. For a REST catalog, what that catalog calls its warehouse, often a name. |
| `namespace` | optional | The namespace -- Glue's database -- a table named without one is in. Created if missing. |
| `credential`, `token` | `rest` only | The REST catalog's sign-in: `credential` as `clientId:clientSecret` for its OAuth2, or a bearer `token`. Held as secrets. |
| `properties` | optional | Anything more for pyiceberg's catalog, as it names it -- `glue.id` for another account's Glue, `header.X-Iceberg-Access-Delegation: vended-credentials` for a REST catalog that hands out storage credentials -- over what bauta sets. |
| `keepSnapshots` | `5` | How many of a table's snapshots stay after a run, the newest included. The files only older ones referenced are deleted with them. |
| `evolveSchema` | `false` | Lets a run add a column the table lacks, rather than refusing the job. |
| `compression`, `fileSize`, `rowGroupSize` | `zstd`, `256MB`, `128MB` | As for a [files connection](#files), for the table's Parquet data files. |
| `endpoint` and each cloud's credentials | optional | As for a [files connection](#files), for the warehouse's files; a REST catalog that vends credentials needs none. On Google Cloud Storage, pyiceberg takes Application Default Credentials only, so `anonymous` and `serviceAccount` are refused. |

`requireMasking` and `maxConcurrentJobs` apply as to any connection.

## Requiring masking

`requireMasking: true` on an alias makes a job that names it as `sourceConnection` or `targetConnection` without a `masking` policy fail `bauta validate`, before anything connects:

```yaml
staging:
  type: postgresql
  database: staging
  host: ${STAGING_HOST}
  user: etl
  password: ${STAGING_PASSWORD}
  requireMasking: true
```

```
copyCountries: targetConnection "staging" is configured with requireMasking, and this job has no masking
policy. Add one naming every column sourceQuery returns -- `keep` for the ones that need no masking
```

A `defaultStrategy` that copies values as they are (`keep`, or a custom passthrough) is refused the same way, since it would cover columns production adds later without anyone deciding. Name each column instead, or give `defaultStrategy` one that masks.

Set it on a target to say the copy can only ever hold masked data, and on a source to say nothing reads it unmasked. Nothing overrides it, not even a job's own [`unmasked: true`](jobs.md#copying-without-masking).

## Passwords that expire

Cloud databases can take short-lived tokens instead of passwords. `passwordCommand` runs a command each time a connection opens and uses what it prints, so a token is never older than the connection that uses it:

```yaml
orders:
  type: postgresql
  host: orders.abc123.eu-west-1.rds.amazonaws.com
  port: 5432
  database: orders
  user: etl
  passwordCommand: [aws, rds, generate-db-auth-token, --hostname, orders.abc123.eu-west-1.rds.amazonaws.com,
                    --port, "5432", --username, etl, --region, eu-west-1]
  options:
    sslmode: verify-full
    sslrootcert: /etc/ssl/rds-global-bundle.pem
```

| Service | Command |
| --- | --- |
| AWS RDS / Aurora IAM | `aws rds generate-db-auth-token ...` as above. MySQL also needs `options: {auth_plugin: mysql_clear_password, ssl_ca: ...}`. |
| Azure Database for PostgreSQL / MySQL | `[az, account, get-access-token, --resource-type, oss-rdbms, --query, accessToken, -o, tsv]` |
| Google Cloud SQL IAM | `[gcloud, sql, generate-login-token]` |
| Oracle with OCI IAM | Pass the token as `options: {access_token: ...}` instead. |
| Any secret manager | Its CLI, e.g. `[vault, kv, get, -field=password, secret/etl/orders]` |

A list runs as written; a single string is split the way a shell would split it, but no shell runs it. The command has 60 seconds. Its output is never logged, and a failure is retried like any other connection error. `validate` never runs it. SQL Server's Azure AD tokens need a driver that pymssql isn't, so they aren't supported.

## Driver options and TLS

`options` is handed to the driver as it is, so it accepts whatever that driver does: `psycopg` (any libpq parameter), `mysql.connector`, `oracledb` and `pymssql`. An option that repeats a field above (`host`, say) is refused by `validate`; set the field instead. Values are read from the environment like any other, and are left out of logs.

Encrypting the connection is the common reason to use it:

| Database | TLS |
| --- | --- |
| postgresql | `sslmode: verify-full` and `sslrootcert: /path/ca.pem`. `require` encrypts without checking the certificate. |
| mysql, mariadb | Encrypted by default when the server supports it. Add `ssl_ca: /path/ca.pem` and `ssl_verify_identity: true` to check the certificate; `ssl_disabled: true` turns TLS off. |
| oracle | `protocol: tcps`, plus `wallet_location` (and `wallet_password`) or `ssl_server_cert_dn` as your server requires. |
| mssql | Configure it in FreeTDS, not in `options`: point `FREETDSCONF` at a `freetds.conf` whose `[global]` section says `encryption = require`. pymssql's own `encryption` argument had no effect in testing with pymssql 2.4.0. |

```yaml
warehouse:
  type: postgresql
  database: analytics
  host: ${WAREHOUSE_HOST}
  user: etl
  password: ${WAREHOUSE_PASSWORD}
  currentSchema: reporting
  options:
    sslmode: verify-full
    sslrootcert: /etc/ssl/warehouse-ca.pem
    application_name: bauta
```

Settings describe what was asked for; the server decides what happened. `bauta run --dry-run` and `bauta audit --connect` report whether each connection is actually encrypted, as the server sees it.

### Read-only connections

```yaml
production:
  type: postgresql
  # ...
  readOnly: true
```

Mark production `readOnly`, so nothing pointed the wrong way can write to it. `validate` and `run` refuse a job loading into it, and every write bauta makes (a load, swap, adhoc query, `clear`, `schema --apply`, `synthesize`, or run state, history or a manifest kept there) is refused. Reading is unaffected.

Where the database can, it refuses writes too: PostgreSQL, MySQL, MariaDB and SQLite make the session read-only and DuckDB opens the file read-only, so even a `sourceQuery` can't write. Oracle and SQL Server can't, so give the login no write privileges as well.

### Statement timeout

`maxRowsReadPerSecond` paces the rows jobs fetch, not what their queries cost the server: a join gone wrong runs on production for as long as it takes. `statementTimeoutSeconds` has the server stop it:

```yaml
production:
  type: postgresql
  # ...
  maxRowsReadPerSecond: 50000
  statementTimeoutSeconds: 600
```

Every session on the connection gets it. A job stopped by it fails without a retry, since the query would run as long again. What is limited differs by database:

| Database | What runs past it is stopped |
| --- | --- |
| postgresql | Each statement, which for a streamed query is each fetch of a chunk: a long extract runs on, but a sort or join that takes too long to produce rows is stopped (`statement_timeout`). |
| mysql | Each `SELECT`, for its whole run, rows sent included (`max_execution_time`). Set it above your longest extract. |
| mariadb | Each statement, for its whole run (`max_statement_time`). Set it above your longest extract. |
| oracle | Each round trip to the server, which for a streamed query is each fetch (oracledb's call timeout). Oracle has no session setting for it. |
| mssql | **By estimate, not by the clock**: a query the optimizer estimates to cost more than this many seconds is refused before it starts, and one estimated below runs to its end (`QUERY_GOVERNOR_COST_LIMIT`). pymssql's own timeout left the session hung rather than ending the query. |

### Requiring encryption

```yaml
production:
  type: postgresql
  # ...
  requireEncryption: true
  options: { sslmode: verify-full, sslrootcert: /etc/ssl/prod-ca.pem }
```

With `requireEncryption`, every session bauta opens asks the server, as it connects, whether it is encrypted, and one that isn't, or whose server can't say (SQL Server answers only a login with `VIEW SERVER STATE`), is closed before anything is read or written. It checks what the server reports, not what `options` asked, so it also catches a server that stopped offering TLS. Whether the certificate is checked is still up to `options`: `sslmode: require` passes without checking it.

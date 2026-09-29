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
| `path` | sqlite, duckdb | required | The database file, or `:memory:`. SQLite connections enforce declared foreign keys, as every other database does; SQLite itself leaves them off unless asked. See [DuckDB](#duckdb) for what differs there. |
| `host`, `user` | the five server types | required | |
| `password` | the five server types | required, unless `passwordCommand` is set | Held as a secret, so it never appears in a log line or a traceback. |
| `passwordCommand` | the five server types | optional | A command whose output is the password, run at every connection. A string is split as a shell would split it, without a shell; `validate` rejects one that names no program or has an unterminated quote. For credentials that expire; see [passwords that expire](#passwords-that-expire). |
| `port` | the five server types | optional | The driver's standard port when omitted. |
| `serviceName` / `sid` | oracle | exactly one | Oracle is reached by service name or SID; it has no `database`. |
| `currentSchema` | postgresql, oracle, duckdb | optional | The schema unqualified table names, and every key and column lookup, resolve in. PostgreSQL sets `search_path` to this schema alone; Oracle sets `CURRENT_SCHEMA`; DuckDB sets `schema`. On the other databases, qualify names as `schema.table` instead. |
| `maxConcurrentJobs` | all | optional, no limit | The most jobs that may use this connection at once, however many `workers` there are. A job that would pass it waits for one on this connection to finish, while jobs on other connections start. For a server that can take only so many loads at a time. Always 1 for `duckdb`; a higher value is refused. |
| `requireMasking` | all | optional, `false` | No job may read from or write to this connection without a masking policy. See [requiring masking](#requiring-masking). |
| `options` | the seven databases | optional | Extra keyword arguments for the driver's `connect()`, for anything the settings above don't cover; for `duckdb`, DuckDB's own settings, such as `memory_limit` and `threads`. See below. |

Any other setting is an error, so a misspelled one stops `validate` rather than leaving the connection to behave in some way nobody configured.

Before 0.1.9 this file was `database.yaml`, a job named its connections with `sourceDatabase` and `targetDatabase`, SQLite named its file with `database`, and Oracle required a `database` it never used. The old names are refused as unknown settings.

## DuckDB

```yaml
analytics:
  type: duckdb
  path: /srv/data/analytics.duckdb
  options: {memory_limit: 4GB}
```

Install it with `pip install "bauta[duckdb]"`, which brings pyarrow for fast loads. DuckDB is a source or target like any other database, with three differences, each stopped with an error that says so rather than left to fail part-way:

- **One job at a time per file.** DuckDB lets one process open a file, and every job runs in a process of its own, so the run starts one job at a time on a DuckDB connection (its `maxConcurrentJobs` is always 1). `workers` still runs jobs on other connections alongside. Two aliases for one file are refused, since each would count its jobs apart. Anything else holding the file open -- another run, a program reading it -- makes a job wait up to a minute, then fail saying why.
- **No run state in DuckDB.** The run holds its run-state connection for as long as it lasts, so `memory` can't be a DuckDB table. `history` and `manifest` can, since each is written once a cycle ends.
- **No swap of a table in a foreign key, or with an index.** DuckDB won't rename a table with an index or one another references, and renaming one that references another corrupts its catalog. A `swap` job whose target or stage is either fails before renaming anything; use `upsert` for it. A primary key is fine. `clear` empties such tables one transaction per table, since DuckDB checks a foreign key against what is committed: a clear stopped part-way leaves the tables it reached empty, and running it again finishes it.

## Files

```yaml
lake:
  type: files
  root: s3://acme-lake/masked      # or gs://..., az://..., abfss://..., or a directory
  format: parquet
  requireMasking: true
```

A directory -- on this machine, or in Amazon S3, Google Cloud Storage or Azure Blob Storage -- that jobs write tables of files into, for a data lake or a handoff. Install it with `pip install "bauta[files]"`, which brings pyarrow; the clouds need nothing more. It is a target only: a job naming it as `sourceConnection`, and `discover`, `subset`, `schema`, `synthesize`, `coverage` and `clear`, are refused, and so is keeping run state, history or a manifest in it. A job writing to it takes `insertStrategy: append` or `overwrite` ([load](jobs.md#load)); [files as a target](../concepts/how-it-works.md#files-as-a-target) describes what a run leaves where.

| Setting | Required or default | Meaning |
| --- | --- | --- |
| `root` | required | Where the tables go: a directory, created if missing; `s3://bucket[/prefix]`; `gs://bucket[/prefix]`; or on Azure `az://container[/prefix]` with `accountName`, or `abfss://container@account.dfs.core.windows.net[/prefix]` as Databricks and Synapse write it. A bucket or container must exist. Each job's `targetTableFinal` is a path under the root. Any other URL is refused rather than read as a directory. |
| `format` | `parquet` | `parquet`, `csv` or `ndjson` (JSON Lines: one JSON object per line). See [formats](../concepts/how-it-works.md#formats) for how each type is spelled in the two text formats. |
| `compression` | `zstd` for Parquet, `gzip` for text | Parquet: `zstd`, `snappy` (for older readers), `gzip` or `none`. CSV and JSON Lines: `gzip` or `none`, the whole file compressed, and named `.gz` so readers know. |
| `delimiter` | `,` | CSV's alone: one character, not a quote or a line break. |
| `fileSize` | `256MB` | Where a part is closed and the next begun, measured as written, compressed. Bytes, or text such as `256MB` or `256MiB`. Snowflake loads best from files of 100 to 250 MB compressed, and Athena and Spark read one file per worker. At most `5GiB` on S3, and `256MiB` on Azure with pyarrow before 19, which publish a part by copying it in one request. |
| `rowGroupSize` | `128MB` | How much of a table is held in memory, before compression, and written at once: one Parquet row group. Larger compresses better and lets engines skip more; smaller holds less. |
| `keepSnapshots` | `2` | How many complete snapshots of an `overwrite` job's table stay, the newest included. |
| `endpoint` | any cloud | Another service speaking the same API -- MinIO or Cloudflare R2 for S3, an emulator for the others -- as a URL, `https://...` or `http://...`. |

`requireMasking` and `maxConcurrentJobs` apply as to any connection. Two connections may share a root, to write some tables as Parquet and others as CSV. A setting of one cloud given with another's root is an error naming the cloud it belongs to.

**Credentials.** In each cloud, without settings that give them, bauta finds credentials as that cloud's command-line tool would. Prefer that: nothing to rotate in a file, and the machine's or the pod's own identity where it has one.

| Cloud | Found without settings | Settings, each optional |
| --- | --- | --- |
| S3 | The AWS default chain: `AWS_ACCESS_KEY_ID` and friends, a profile, SSO, an EC2 instance role, an ECS task role, IRSA on Kubernetes. | `region` (else the bucket's, asked of S3); `accessKeyId` with `secretAccessKey`, and `sessionToken` for temporary keys; `roleArn`, a role to assume with whichever credentials the rest find, for a bucket in another account. |
| Google Cloud Storage | Application Default Credentials: the key file `GOOGLE_APPLICATION_CREDENTIALS` names, `gcloud auth application-default login`, a GCE or GKE service account, workload identity. | `serviceAccount`, an account to impersonate with them; `anonymous` set to `true`, for a public bucket or an emulator. |
| Azure | The Azure default chain: `AZURE_CLIENT_ID` and friends in the environment, workload identity on Kubernetes, a managed identity, `az login`. | One of: `accountKey`; `sasToken` (pyarrow 20 or newer); a service principal, `clientId` with `clientSecret` and `tenantId` (pyarrow 21 or newer). `accountName` names the storage account for an `az://` root. |

Secrets -- `secretAccessKey`, `sessionToken`, `accountKey`, `sasToken`, `clientSecret` -- are held as `password` is, never in a log line; give them as `${VARIABLES}`.

**What the credentials need**, on the root's prefix: to write, read (a part is published by copying it), delete and list objects -- on S3 `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`, `s3:ListBucket` and `s3:AbortMultipartUpload`; on GCS the role `roles/storage.objectAdmin`; on Azure `Storage Blob Data Contributor`. `bauta run --dry-run` writes, lists and deletes an object under the root to check. On S3, give the bucket a lifecycle rule aborting incomplete multipart uploads after a day or so: a process killed mid-upload leaves parts that S3 bills for and nothing lists.

## Iceberg

```yaml
lake:
  type: iceberg
  catalog: glue
  warehouse: s3://acme-lake/warehouse
  namespace: masked
  requireMasking: true
```

Iceberg tables, found through a catalog, which Athena, Snowflake, Databricks, BigQuery, Trino and Spark all read: a run's rows land as one commit, so a reader sees all of a run or none of it, and `upsert` works as it does against a database. Install it with `pip install "bauta[iceberg]"`, which brings pyiceberg and pyarrow. Like a files connection it is a target only. A job writing to it names `namespace.table`, or `table` in the connection's `namespace`, as its `targetTableFinal`, and takes `insertStrategy: append`, `overwrite` or `upsert` ([load](jobs.md#load)); [Iceberg tables](../concepts/how-it-works.md#iceberg-tables) says what a run does.

| Setting | Required or default | Meaning |
| --- | --- | --- |
| `catalog` | required | `glue` (AWS), `rest` -- the Iceberg REST protocol: BigLake on GCP, Databricks Unity Catalog, Snowflake Open Catalog (Polaris), S3 Tables, Lakekeeper, Nessie -- or `sql`, a catalog kept in a database of your own, SQLite or PostgreSQL, for a setup with no catalog service. |
| `uri` | required for `rest` and `sql` | The REST catalog's URL, or the SQL catalog's database as SQLAlchemy names it: `sqlite:////srv/lake/catalog.db`, `postgresql+psycopg://user:password@host/catalog`. |
| `warehouse` | required for `glue` and `sql` | Where a table the catalog creates keeps its files: a directory, `s3://`, `gs://`, `az://` (with `accountName`) or `abfss://`. For a REST catalog, what that catalog calls its warehouse, often a name. |
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

Set it on a target to say that the copy can only ever hold masked data, and on a source to say that nothing reads from it unmasked. Nothing overrides it: a job's own [`unmasked: true`](jobs.md#copying-without-masking) records a decision about that job, but `requireMasking` is the database's, and it wins.

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

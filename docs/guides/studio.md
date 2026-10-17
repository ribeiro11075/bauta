# Studio

!!! note "Preview"
    Studio is new. Its screens, settings and stored data may change between releases without a deprecation period, and it has been tested on SQLite more than on server databases. It runs as one process, and sees live runs only on its own machine. The CLI is unaffected either way.

bauta Studio is a web console for a team, run on your own infrastructure. People sign in to watch runs as they go, start and rerun them, prove the copy is safe, and change jobs, without a shell on the machine that holds production's credentials. Everything it shows, `bauta` itself can: the CLI is complete without it.

Studio reads the same configuration, run state, history and manifests `run` keeps, and starts runs as the same `bauta run` cron does. Your schedule stays where it is.


## Running it

```
pip install "bauta[studio]"
bauta studio create-user --config /etc/bauta/configuration --email you@example.com
bauta studio serve --config /etc/bauta/configuration
```

`create-user` asks for a password, at least 12 characters, and makes an admin unless `--role` says otherwise; it is how the first account is made, since a sign-up page open to whoever reaches it first would be a hole. `serve` listens on `127.0.0.1:8710`, this machine only.

To share it, put it behind a proxy that terminates TLS -- nginx, Caddy, a cloud load balancer -- and pass `--host 0.0.0.0` if the proxy is on another machine, with `--forwarded-allow-ips` naming the proxy. Run it as one process: it keeps track of the runs it starts.

Give Studio the environment your cron job has. It loads the same configuration, so the variables `${MASKING_KEY}` and passwords name must be set, and `BAUTA_MANIFEST_KEY` lets it verify signed manifests.

| Setting | Default | |
| --- | --- | --- |
| `--config`, `$BAUTA_STUDIO_CONFIG` | `$BAUTA_CONFIG`, else `./configuration` | bauta's `jobs.yaml` and `connections.yaml`. |
| `--data`, `$BAUTA_STUDIO_DATA` | `./studio-data` | Studio's own: accounts, the activity log, the logs of runs it starts, and the jobs files as they were before each edit. Keep it on a persistent volume. |
| `$BAUTA_STUDIO_DATABASE_URL` | SQLite in the data directory | Any SQLAlchemy URL, PostgreSQL say, for Studio's own tables. |
| `$BAUTA_STUDIO_INSECURE_COOKIES=1` | off | Only for plain HTTP on your own machine. |


## Who can do what

| Role | Can |
| --- | --- |
| viewer | read every screen |
| operator | also start, rerun and cancel runs, and run the checks that connect to the databases: dry runs, a connected audit, coverage, discovery, connection tests |
| admin | also edit the jobs files and manage users, and read the activity log |


## What it shows

- **Overview:** the last run, failing jobs, stale jobs, what the latest checks found that the ones before didn't -- a table no job covers, a column no policy names, a reference that broke -- the audit's findings, whether the last manifest verifies, and where recent runs spent their time.
- **Runs:** every run, from the schedule or from Studio. A running one updates as it goes: each job waiting, running with the rows it has written so far, or done; see [what's running now](watch-what-ran.md#whats-running-now).
- **Cancelling** a run asks it to stop as Ctrl-C would: jobs not yet started are skipped, and jobs already loading finish first, so no target is left half loaded. A run on another machine is cancelled there.
- **Start a run** offers a full refresh only when every incremental job can take one, and says why not when one can't.
- **Jobs:** each job's policy by strategy and key fingerprint, how it loads, its trend of rows and duration, its recent runs, and its speed against the source: reading up to the rows asked for and masking them as a run would, writing nothing, as [`bauta bench`](make-it-faster.md#measure-a-job) does.
- **Comparing runs:** two runs side by side, job by job -- status, rows, duration, the stage that slowed it, and, where both manifests are kept, the columns masked differently.
- **Schedule:** where each job stands -- due, inside its `refresh` window, inactive -- when it last ran, and whether it has gone stale.
- **Proof:** coverage of every source, the references in each copy, as [`verify-references`](copy-a-subset.md#verify-references-checking-the-copys-references) counts them, the audit, every manifest and whether it verifies, and an evidence bundle per run to hand to a reviewer: what ran, the manifest it sealed and its verification, the audit and coverage, whether the jobs files are still the ones it used, and a SHA-256 of every file in it. Studio keeps each manifest by run as it appears, since a manifest file is replaced by every run; runs from cron in quick succession can replace one before Studio sees it, so to be sure of every run's, keep manifests [in a table](keep-a-manifest.md#in-a-table).
- **Discovery:** columns that appeared in a source since its policy was written, with the policy [`discover`](propose-a-policy.md) proposes for each, to accept into the jobs file.
- **Subset builder:** a [subset](copy-a-subset.md) rooted at a table and a filter, previewed in load order with each table's rows counted, which tables the target lacks, and which another job already loads; then added to a jobs file. The filter is SQL run against the source as written, so only an admin builds one.
- **Connections:** each alias, where it points, and its rules, with a connection test. Never a password.
- **Configuration:** the jobs files, edited in place as text or job by job in a form.


## Nightly checks

Coverage, the audit against the databases, discovery and references read the sources' catalogs and a sample of rows, so they run when an operator asks -- or every day at a time an admin sets on the Proof page, in UTC. Each result is kept with who or what ran it; a Studio that was down at the time runs them when it starts. What a check finds that the one before it didn't is listed on the Overview: that is how a table added to production last night, or a column a source gained, comes to someone's attention before a job copies it.


## What it protects

- **Accounts:** argon2 password hashes; five failed sign-ins for an email in five minutes, then it is refused for a while. Sessions are random tokens kept only as their SHA-256, in an HttpOnly, SameSite=Strict cookie, marked Secure unless turned off. Changing a password or deactivating a user signs them out everywhere, and the last admin can't be removed.
- **Requests from other sites:** every change needs the `X-Bauta-Studio` header, which another site can't send without a CORS preflight Studio never answers. Pages carry a strict Content-Security-Policy and can't be framed.
- **What leaves the server:** what `bauta audit` and `bauta history` already show -- names, strategies, key fingerprints, counts, times and errors as history keeps them. Never a masking key, a password, `connections.yaml`, or a value from a table.
- **Editing:** only `jobs.yaml` and the files it includes. A change is written only if the whole configuration still validates with it and the file hasn't changed since it was opened, in one atomic step; the file as it was is kept, and the change is recorded with its diff and who made it.
- **Runs:** each a `bauta run` process in a session of its own, under bauta's own lock, so a run from Studio and one from cron never overlap on the same jobs, and restarting Studio doesn't stop one.

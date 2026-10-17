<!-- Generated from the parser by website/commands.py. Change the flag, then run `python website/commands.py`. -->

# `bauta studio`

Run bauta Studio, a web console for a team, on your own infrastructure.

See [Studio](../../guides/studio.md) for how to use it.

## `bauta studio serve`

Run Studio.

```
bauta studio serve [options]
```

| Flag | Default | Effect |
| --- | --- | --- |
| `--host HOST` | `127.0.0.1` | Address to listen on. |
| `--port PORT` | `8710` |  |
| `--forwarded-allow-ips FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies trusted to say the client's address and scheme. |
| `--data DATA` | $BAUTA_STUDIO_DATA or ./studio-data | Studio's own data: accounts, activity, run logs, backups. |

It also takes these [common flags](index.md#common-flags): `--config`, `--log`, `--log-format`, `--log-level`, `--quiet`.

## `bauta studio create-user`

Add an account; the way the first admin is made.

```
bauta studio create-user --email EMAIL [options]
```

| Flag | Default | Effect |
| --- | --- | --- |
| `--email EMAIL` | required | The account's email, which is its sign-in. |
| `--name NAME` |  | Shown beside what they do. |
| `--role {viewer,operator,admin}` | `admin` | One of `viewer`, `operator`, `admin`. |
| `--data DATA` | $BAUTA_STUDIO_DATA or ./studio-data | Studio's own data: accounts, activity, run logs, backups. |

It also takes these [common flags](index.md#common-flags): `--config`, `--log`, `--log-format`, `--log-level`, `--quiet`.

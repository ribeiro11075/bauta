# Studio

!!! note "Preview"
    Studio is new. Its screens, settings and stored data may change between releases without a deprecation period, and it has been tested on SQLite more than on server databases. It runs as one process, and sees live runs only on its own machine. The CLI is unaffected either way.

bauta Studio is a web console for a team, run on your own infrastructure. People sign in to watch runs, start and rerun them, prove the copy is safe, and change jobs, without a shell on the machine holding production's credentials. Everything it shows, the `bauta` command can too.

Studio reads the configuration, run state, history and manifests `run` keeps, and starts runs as the same `bauta run` cron does, so your schedule stays where it is.


## Running it

```
pip install "bauta[studio]"
bauta studio create-user --config /etc/bauta/configuration --email you@example.com
bauta studio serve --config /etc/bauta/configuration
```

`create-user` makes an admin (unless `--role` says otherwise), asking for a password of 12 characters or more, or none with `--sso-only`. It is the only way to make the first account, since an open sign-up page would be a hole. People change their own password on their Account page; an admin sets a forgotten one on the Users page, as Studio sends no email.

Locked out, say with the only admin's password forgotten or their authenticator lost with its recovery codes? Reset it from the server's shell, which signs the account out everywhere; `--reset-2fa` also turns two-factor sign-in off, to set up again:

```
bauta studio set-password --config /etc/bauta/configuration --email you@example.com --reset-2fa
```

`serve` listens on `127.0.0.1:8710`, this machine only. To share it, put it behind a proxy that terminates TLS (nginx, Caddy, a cloud load balancer), with `--host 0.0.0.0` and `--forwarded-allow-ips` naming the proxy if it is on another machine. Run one process: it keeps track of the runs it starts.

Give Studio your cron job's environment: the variables the configuration names (`${MASKING_KEY}`, passwords) must be set, and `BAUTA_MANIFEST_KEY` lets it verify signed manifests.

| Setting | Default | |
| --- | --- | --- |
| `--config`, `$BAUTA_STUDIO_CONFIG` | `$BAUTA_CONFIG`, else `./configuration` | bauta's `jobs.yaml` and `connections.yaml`. |
| `--data`, `$BAUTA_STUDIO_DATA` | `./studio-data` | Studio's own: accounts, the activity log, run logs, and the jobs files as they were before each edit. Keep it on a persistent volume. |
| `$BAUTA_STUDIO_DATABASE_URL` | SQLite in the data directory | Any SQLAlchemy URL, PostgreSQL say, for Studio's tables. |
| `$BAUTA_STUDIO_INSECURE_COOKIES=1` | off | Only for plain HTTP on your own machine. |
| `$BAUTA_STUDIO_TRANSFORM_MODULES` | none | Comma-separated modules an edit may start naming in a transform or custom masking strategy. See [what it protects](#what-it-protects). |
| `$BAUTA_STUDIO_PASSWORD_LOGIN` | `all` | Who may sign in with a password: `all`, `admins` or `off`. Anything but `all` needs [single sign-on](#single-sign-on). |
| `$BAUTA_STUDIO_SESSION_HOURS` | `12` | How long a password session lasts. |
| `$BAUTA_STUDIO_REQUIRE_2FA` | `off`, or `admins` with single sign-on | Whose password also needs an authenticator code; see [two-factor sign-in](#two-factor-sign-in). |
| `$BAUTA_STUDIO_SECRET_KEY_FILE` | `secret.key` in the data directory | The key two-factor secrets are encrypted with. |


## Single sign-on

Studio signs people in through your identity provider with OpenID Connect: Okta, Entra ID, Google Workspace, Auth0, Keycloak, or any provider with a discovery document.

1. At the provider, register a web application (a confidential client) with the redirect URI `https://<studio's address>/api/auth/sso/callback`, and note its issuer, client ID and client secret.
2. Give Studio these, and restart it:

    ```
    BAUTA_STUDIO_PUBLIC_URL=https://studio.example.com
    BAUTA_STUDIO_OIDC_ISSUER=https://example.okta.com
    BAUTA_STUDIO_OIDC_CLIENT_ID=0oa...
    BAUTA_STUDIO_OIDC_CLIENT_SECRET_FILE=/run/secrets/studio-oidc
    ```

3. Add people on the Users page with no password, or let them [make their own accounts](#who-gets-an-account). The sign-in page then offers the provider.

Then make the provider the way in:

- **`BAUTA_STUDIO_PASSWORD_LOGIN=admins`**, leaving passwords only to break-glass admins, the way in if the provider is down or misconfigured. Make one with `create-user` under an email the provider doesn't have, such as `break-glass@example.com`; `serve` warns when no active admin has a password. Under `all`, someone linked keeps their password, so disabling them at the provider doesn't keep them out: remove it on the Users page.
- **[Two-factor sign-in](#two-factor-sign-in)** for those passwords, on for admins by default once single sign-on is.

| Setting | Default | |
| --- | --- | --- |
| `$BAUTA_STUDIO_PUBLIC_URL` | required | Where browsers reach Studio, with no path. The redirect URI is built from it, never from the request. |
| `$BAUTA_STUDIO_OIDC_ISSUER` | off | The provider's issuer, exactly as its discovery document names it, trailing slash and all. Setting it turns single sign-on on. |
| `$BAUTA_STUDIO_OIDC_CLIENT_ID` | required | |
| `$BAUTA_STUDIO_OIDC_CLIENT_SECRET`, `..._CLIENT_SECRET_FILE` | none | The secret, or a file holding it. Sent with HTTP Basic unless the provider takes it only in the request body. |
| `$BAUTA_STUDIO_OIDC_LABEL` | `Sign in with SSO` | The sign-in button's words, such as `Sign in with Okta`. |
| `$BAUTA_STUDIO_OIDC_SCOPES` | `openid email profile` | Space-separated. Add `groups`, or your provider's name for it, if groups set roles. |
| `$BAUTA_STUDIO_OIDC_SESSION_HOURS` | `8` | How long a session through the provider lasts; someone disabled there keeps theirs until it ends. |
| `$BAUTA_STUDIO_OIDC_TRUST_EMAIL=1` | off | Link by email even when the provider doesn't vouch for it. See [who gets an account](#who-gets-an-account). |
| `$BAUTA_STUDIO_OIDC_AUTO_CREATE=1` | off | Make an account at a person's first sign-in. Needs `..._ALLOWED_DOMAINS`. |
| `$BAUTA_STUDIO_OIDC_ALLOWED_DOMAINS` | none | Comma-separated email domains whose people get an account at first sign-in. A subdomain is another domain. |
| `$BAUTA_STUDIO_OIDC_HOSTED_DOMAINS` | none | For Google Workspace: the workspaces whose accounts may sign in, by the token's `hd` claim. |
| `$BAUTA_STUDIO_OIDC_ROLES` | `studio` | `studio`: admins set roles on the Users page. `provider`: groups at the provider set them. |
| `$BAUTA_STUDIO_OIDC_ROLE_CLAIM` | `groups` | The claim listing a person's groups; a dotted name reaches inside an object, such as `realm_access.roles`. |
| `$BAUTA_STUDIO_OIDC_ADMIN_GROUPS`, `..._OPERATOR_GROUPS`, `..._VIEWER_GROUPS` | none | For `ROLES=provider`: comma-separated groups giving each role, as the claim names them. Someone in several gets the highest. |

Studio refuses to start with a setting missing or wrong, naming it. `serve` reads the provider's discovery document as it starts and warns if it can't, then starts anyway and says so at sign-in. Its log says why any sign-in was refused.

### Who gets an account

Once linked, a person is matched by the provider's issuer and subject, which the provider gives no one else. The subject is kept as its SHA-256, so two differing only in case stay two people on databases that ignore case.

The first time, a person is linked by email, but only if the provider vouches for it (`email_verified`, or Entra ID's `xms_edov`): a provider that let people set their own email would otherwise let anyone take over an account. Set `TRUST_EMAIL=1` only for a provider whose emails only its admins set.

- **Made at first sign-in:** under `AUTO_CREATE`, someone with no account gets one if their email is in an allowed domain, as a viewer or with the role their groups give. Otherwise they are told to ask an admin.
- **Passwords:** linking removes a password the policy wouldn't let the account use. A break-glass admin under `PASSWORD_LOGIN=admins` is never linked; its sign-in through the provider is refused.
- **Emails:** an email changed at the provider follows to Studio if the provider vouches for it and no other account has it.
- **Linked twice:** an account linked to one identity refuses another from the same issuer. When a person's identity there was remade, an admin unlinks the account on the Users page and the next sign-in links it anew. One linked under another issuer is relinked by itself.
- **Deactivated** accounts are refused through the provider too.

### Roles from the provider

With `BAUTA_STUDIO_OIDC_ROLES=provider`, a person's role is set from their groups at each sign-in, and the Users page shows it but can't change it. Someone with no group giving a role is refused, with no account made. A change takes effect at the next sign-in, so a session keeps its role until it ends. Accounts not linked to the provider, the break-glass admin among them, keep the roles set in Studio. The provider can't demote the last active admin: that sign-in is refused, saying why.

Groups are read from the ID token, or from the userinfo endpoint when the token leaves them out.

### Signing out, and logs

Signing out ends Studio's session, not the provider's, so the next sign-in through it won't ask for a password while the provider's session lasts.

The callback's address holds a one-time authorization code, which Studio keeps out of its access log. A proxy in front logs it too; it is useless without what only the browser that began the sign-in held, but you may want the proxy to leave out the query for `/api/auth/sso/callback`.

### Notes by provider

- **Okta:** the issuer is your org's (`https://example.okta.com`) or an authorization server's (`.../oauth2/default`). For roles, add a groups claim to the ID token, filtered to Studio's groups, and add `groups` to `SCOPES` for the org authorization server.
- **Entra ID:** the issuer is `https://login.microsoftonline.com/<tenant id>/v2.0`. Entra sends no `email_verified`, so add the `xms_edov` optional claim, which Studio accepts instead. `TRUST_EMAIL=1` is safe only in a tenant with no guest users. For roles, define app roles and set `ROLE_CLAIM=roles`: a groups claim names groups by object ID and is cut off past 200.
- **Google Workspace:** the issuer is `https://accounts.google.com`. Set `HOSTED_DOMAINS` to your workspace, or a personal Google account made with a work email, whose email is also verified, could sign in. Google has no groups claim, so set roles in Studio.
- **Keycloak:** the issuer is `https://<host>/realms/<realm>`. For roles, add a group membership mapper with full group path off, or read realm roles with `ROLE_CLAIM=realm_access.roles` after turning on "Add to ID token" for the realm roles mapper.


## Two-factor sign-in

A password can also ask for a code from an authenticator app (1Password, Google Authenticator, Microsoft Authenticator, any that does TOTP). Signing in through the identity provider never does: the provider asks for its own second factor.

Anyone with a password sets it up on their Account page: scan a QR code, type the code the app shows, and keep the ten one-time recovery codes for a lost app. `BAUTA_STUDIO_REQUIRE_2FA` makes it required:

| `$BAUTA_STUDIO_REQUIRE_2FA` | |
| --- | --- |
| `off` | Anyone may set it up. The default without single sign-on. |
| `admins` | Admins set it up at their next sign-in, before they are signed in, and can't turn it off. The default with single sign-on, when an admin's password is the break-glass way in. |
| `all` | Everyone with a password. |

An admin resets it on the Users page for someone who lost both app and recovery codes.

- **Codes** are taken once each; one a step either side of now is accepted too, for a drifting phone clock. Ten wrong codes for an account in five minutes, five from one address, or thirty from one address across accounts, and more are refused for a while.
- **Secrets** are encrypted with a key Studio makes in its data directory, `secret.key`, readable by its account alone, so the database or a backup of it can't make codes without the key. Keep the key apart with `$BAUTA_STUDIO_SECRET_KEY_FILE`, and back it up: lose it, and everyone's two-factor sign-in must be reset.
- **Recovery codes** are kept only as their SHA-256, and each signs in once.

## Who can do what

| Role | Can |
| --- | --- |
| viewer | read every screen |
| operator | also start, rerun and cancel runs, and run the checks that connect to the databases: dry runs, a connected audit, coverage, discovery, connection tests |
| admin | also edit the jobs files, manage users, and read the activity log |


## What it shows

- **Overview:** the last run, failing and stale jobs, what the latest checks found new (a table no job covers, a column no policy names, a broken reference), the audit's findings, whether the last manifest verifies, and where recent runs spent their time.
- **Runs:** every run, from the schedule or from Studio. A running one updates as it goes, each job waiting, running with its rows so far, or done; see [what's running now](watch-what-ran.md#whats-running-now).
- **Cancelling** a run stops it as Ctrl-C would: jobs not started are skipped and loading ones finish, so no target is left half loaded. A run on another machine is cancelled there.
- **Start a run** offers a full refresh only when every incremental job can take one, and says why not when one can't.
- **Jobs:** each job's policy by strategy and key fingerprint, how it loads, its trend of rows and duration, its recent runs, and its speed against the source, measured as [`bauta bench`](make-it-faster.md#measure-a-job) measures it.
- **Comparing runs:** two runs job by job: status, rows, duration, the stage that slowed it and, where both manifests are kept, the columns masked differently.
- **Schedule:** each job's place (due, inside its `refresh` window, inactive), when it last ran, and whether it is stale.
- **Proof:** coverage of every source, each copy's references as [`verify-references`](copy-a-subset.md#verify-references-checking-the-copys-references) counts them, the audit, every manifest and whether it verifies, and an evidence bundle per run for a reviewer (what ran, its manifest and verification, the audit and coverage, whether the jobs files are still those it used, and a SHA-256 of every file). Studio keeps each manifest by run as it appears; cron runs in quick succession can replace one before Studio sees it, so to be sure of every run's, keep manifests [in a table](keep-a-manifest.md#in-a-table).
- **Discovery:** columns a source gained since its policy was written, with the policy [`discover`](propose-a-policy.md) proposes, to accept into the jobs file.
- **Subset builder:** a [subset](copy-a-subset.md) from a root table and a filter, previewed in load order with row counts, which tables the target lacks and which another job already loads, then added to a jobs file. The filter is SQL run against the source as written, so only an admin builds one.
- **Connections:** each alias, where it points and its rules, with a connection test. Never a password.
- **Configuration:** the jobs files, edited in place as text or job by job in a form.


## Nightly checks

Coverage, the connected audit, discovery and references read the sources, so they run when an operator asks, or daily at a time an admin sets on the Proof page (in UTC); a Studio that was down then runs them as it starts. Each result is kept with who ran it, and what a check finds new is listed on the Overview, so a table or column production gained last night reaches someone before a job copies it.


## What it protects

- **Single sign-on:** the authorization code flow with PKCE. The state tying a sign-in to its browser is in a signed cookie sent only to the callback, lasting ten minutes. The ID token must be signed by a key the provider publishes, asymmetrically, for this client, by this issuer, unexpired, with this sign-in's nonce. The provider's keys are re-read within the hour, so a withdrawn key stops being trusted. Each sign-in is taken once, failed ones are counted by address, only HTTPS reaches the provider and Studio (but for local addresses), and a new sign-in ends the browser's old session.
- **Accounts:** argon2 password hashes. Five failed sign-ins for an email from one address in five minutes, thirty from one address across emails, or a hundred for one email across addresses, and more are refused for a while, so failing from one address doesn't lock the owner out from another (give `--forwarded-allow-ips` your proxy, or every sign-in counts as its address). At most four passwords are checked at once. Sessions are random tokens kept only as their SHA-256, in an HttpOnly, SameSite=Strict, Secure cookie. Changing a password or deactivating a user signs them out everywhere, and the last admin can't be removed.
- **Requests from other sites:** every change needs the `X-Bauta-Studio` header, which another site can't send without a CORS preflight Studio never answers. Pages carry a strict Content-Security-Policy and can't be framed.
- **What leaves the server:** what `bauta audit` and `bauta history` show: names, strategies, key fingerprints, counts, times and scrubbed errors. Never a masking key, a password, `connections.yaml`, or a table's value.
- **Editing:** only `jobs.yaml` and the files it includes. A change is written, atomically, only if the whole configuration still validates with it and the file is unchanged since it was opened; the old file is kept, and the change recorded with its diff and author.
- **Admins are trusted with the server.** Editing a job changes what SQL runs against your databases, as Studio's account. An edit can't start naming a transform or custom strategy from a module the jobs files don't already use (beyond bauta's own and `$BAUTA_STUDIO_TRANSFORM_MODULES`), since `os:system` is a callable too, and the edit is refused before anything is imported; DuckDB queries can't reach the machine's files ([DuckDB](../reference/connections.md#duckdb)). That closes the shortest path from an edit to a command, not every path: give the role accordingly.
- **Its own files:** the data directory, run logs and backups are created readable by Studio's account alone. An existing directory keeps its permissions; `chmod 700` it.
- **Upgrades:** Studio migrates its own tables as it starts, keeping what they hold. Back up its database first, as you would any other.
- **Runs:** each a `bauta run` process in a session of its own, under bauta's lock, so a run from Studio and one from cron never overlap on the same jobs, and restarting Studio doesn't stop one.

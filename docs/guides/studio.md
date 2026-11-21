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

`create-user` asks for a password, at least 12 characters, unless `--sso-only` says they sign in only through [single sign-on](#single-sign-on), and makes an admin unless `--role` says otherwise. It is how the first account is made, since a sign-up page open to whoever reaches it first would be a hole. Anyone with a password changes it on their Account page, under their name in the sidebar, giving the one they have; an admin sets one for someone who forgot theirs on the Users page, since Studio sends no emails.

If you are locked out -- the only admin forgot their password, or lost both their authenticator app and their recovery codes -- set it from the server's shell, which signs the account out everywhere:

```
bauta studio set-password --config /etc/bauta/configuration --email you@example.com --reset-2fa
```

`--reset-2fa` also turns its two-factor sign-in off, to set up again at the next sign-in. `serve` listens on `127.0.0.1:8710`, this machine only.

To share it, put it behind a proxy that terminates TLS -- nginx, Caddy, a cloud load balancer -- and pass `--host 0.0.0.0` if the proxy is on another machine, with `--forwarded-allow-ips` naming the proxy. Run it as one process: it keeps track of the runs it starts.

Give Studio the environment your cron job has. It loads the same configuration, so the variables `${MASKING_KEY}` and passwords name must be set, and `BAUTA_MANIFEST_KEY` lets it verify signed manifests.

| Setting | Default | |
| --- | --- | --- |
| `--config`, `$BAUTA_STUDIO_CONFIG` | `$BAUTA_CONFIG`, else `./configuration` | bauta's `jobs.yaml` and `connections.yaml`. |
| `--data`, `$BAUTA_STUDIO_DATA` | `./studio-data` | Studio's own: accounts, the activity log, the logs of runs it starts, and the jobs files as they were before each edit. Keep it on a persistent volume. |
| `$BAUTA_STUDIO_DATABASE_URL` | SQLite in the data directory | Any SQLAlchemy URL, PostgreSQL say, for Studio's own tables. |
| `$BAUTA_STUDIO_INSECURE_COOKIES=1` | off | Only for plain HTTP on your own machine. |
| `$BAUTA_STUDIO_TRANSFORM_MODULES` | none | Comma-separated modules, with what is under them, that an edit in Studio may start naming in a transform or a masking strategy of your own. See [what it protects](#what-it-protects). |
| `$BAUTA_STUDIO_PASSWORD_LOGIN` | `all` | Who may sign in with a password: `all`, `admins` or `off`. Anything but `all` needs [single sign-on](#single-sign-on). |
| `$BAUTA_STUDIO_SESSION_HOURS` | `12` | How long a session signed in with a password lasts. |
| `$BAUTA_STUDIO_REQUIRE_2FA` | `off`, or `admins` with single sign-on | Whose password also needs a code from an authenticator app: see [two-factor sign-in](#two-factor-sign-in). |
| `$BAUTA_STUDIO_SECRET_KEY_FILE` | `secret.key` in the data directory | The key two-factor secrets are encrypted with. |


## Single sign-on

Studio signs people in through your identity provider with OpenID Connect: Okta, Entra ID, Google Workspace, Auth0, Keycloak, or any other that publishes a discovery document.

1. At the provider, register a web application, a confidential client, with the redirect URI `https://<studio's address>/api/auth/sso/callback`, and note its issuer, client ID and client secret.
2. Give Studio these, and restart it:

    ```
    BAUTA_STUDIO_PUBLIC_URL=https://studio.example.com
    BAUTA_STUDIO_OIDC_ISSUER=https://example.okta.com
    BAUTA_STUDIO_OIDC_CLIENT_ID=0oa...
    BAUTA_STUDIO_OIDC_CLIENT_SECRET_FILE=/run/secrets/studio-oidc
    ```

3. Add the people who will use it on the Users page, with no password, or let the provider's people [make their own accounts](#who-gets-an-account). The sign-in page then offers to sign in with the provider.

Then keep people out through the provider alone:

- **`BAUTA_STUDIO_PASSWORD_LOGIN=admins`**, so the only passwords left are break-glass admins'. If the provider is down or misconfigured, such an account is how anyone gets in to fix it. Make it with `create-user` under an email the provider doesn't have, such as `break-glass@example.com`; `serve` warns when no active admin has a password. With `all`, someone linked to the provider keeps their password, and disabling them there doesn't keep them out: remove it on the Users page.
- **[Two-factor sign-in](#two-factor-sign-in)** for those passwords, which is on for admins by default once single sign-on is.

| Setting | Default | |
| --- | --- | --- |
| `$BAUTA_STUDIO_PUBLIC_URL` | required | Where browsers reach Studio, with no path. The redirect URI is built from it, never from the request. |
| `$BAUTA_STUDIO_OIDC_ISSUER` | off | The provider's issuer, exactly as its discovery document names it, trailing slash and all. Single sign-on is on once it is set. |
| `$BAUTA_STUDIO_OIDC_CLIENT_ID` | required | |
| `$BAUTA_STUDIO_OIDC_CLIENT_SECRET`, `..._CLIENT_SECRET_FILE` | none | The secret, or a file holding it, as a mounted secret is. Sent with HTTP Basic unless the provider takes it only in the request body. |
| `$BAUTA_STUDIO_OIDC_LABEL` | `Sign in with SSO` | The sign-in button's words: `Sign in with Okta`, say. |
| `$BAUTA_STUDIO_OIDC_SCOPES` | `openid email profile` | Space-separated. Add `groups`, or what your provider calls it, if groups set roles. |
| `$BAUTA_STUDIO_OIDC_SESSION_HOURS` | `8` | How long a session through the provider lasts. Someone the provider disables keeps one they have until it ends. |
| `$BAUTA_STUDIO_OIDC_TRUST_EMAIL=1` | off | Link by email even when the provider doesn't say it is verified. See [who gets an account](#who-gets-an-account). |
| `$BAUTA_STUDIO_OIDC_AUTO_CREATE=1` | off | Make an account at a person's first sign-in. Needs `..._ALLOWED_DOMAINS`. |
| `$BAUTA_STUDIO_OIDC_ALLOWED_DOMAINS` | none | Comma-separated email domains whose people get an account at their first sign-in. A subdomain is another domain. |
| `$BAUTA_STUDIO_OIDC_HOSTED_DOMAINS` | none | For Google Workspace: the workspaces whose accounts may sign in, by the token's `hd` claim. |
| `$BAUTA_STUDIO_OIDC_ROLES` | `studio` | `studio`: admins set roles on the Users page. `provider`: groups at the provider set them. |
| `$BAUTA_STUDIO_OIDC_ROLE_CLAIM` | `groups` | The claim listing a person's groups. A dotted name reaches inside an object: `realm_access.roles`. |
| `$BAUTA_STUDIO_OIDC_ADMIN_GROUPS`, `..._OPERATOR_GROUPS`, `..._VIEWER_GROUPS` | none | Comma-separated groups giving each role, for `ROLES=provider`, as the claim names them. Someone in several gets the highest. |

Studio refuses to start with a setting missing or wrong, and says which. `serve` reads the provider's discovery document as it starts and warns if it can't; Studio starts anyway, and says so at sign-in. Its log says why any sign-in was refused.

### Who gets an account

Once linked, a person is matched by the provider's issuer and subject, which the provider never gives anyone else. The subject is kept as its SHA-256, so two that differ only in case stay two people on databases that compare text without case, such as MySQL and SQL Server.

The first time, Studio links a person to the account with their email, if the provider vouches for the email: with `email_verified`, or Entra ID's `xms_edov`. A provider that lets people set their own email would otherwise let anyone take an account over by setting its email. Set `BAUTA_STUDIO_OIDC_TRUST_EMAIL=1` only for a provider whose emails only its admins set.

- **Made at first sign-in:** with `AUTO_CREATE`, someone with no account gets one, as a viewer, or with the role their groups give, if their email is in an allowed domain. Without it, they are told to ask an admin.
- **Passwords:** linking removes a password the policy wouldn't let the account use. A break-glass admin, under `PASSWORD_LOGIN=admins`, is never linked: its sign-in through the provider is refused.
- **Emails:** an email changed at the provider follows to Studio if the provider vouches for the new one and no other account has it.
- **Linked twice:** an account linked to one identity refuses another from the same issuer with its email. When a person's identity at the provider was made again, an admin unlinks the account on the Users page, and their next sign-in links it anew. One linked under another issuer, as after moving to another of Okta's authorization servers, is linked anew by itself.
- **Deactivated** accounts are refused through the provider too.

### Roles from the provider

With `BAUTA_STUDIO_OIDC_ROLES=provider`, a person's role is set from their groups at each sign-in, and the Users page shows it but doesn't change it. Someone none of whose groups gives a role is refused, and no account is made for them. A change at the provider takes effect at the person's next sign-in, so a session they already have keeps its role until it ends. Accounts that aren't linked to the provider, the break-glass admin among them, keep the roles set here.

The provider can't take the admin role from Studio's last active admin: that sign-in is refused, and says why.

Groups are read from the ID token, or from the userinfo endpoint when the token leaves them out.

### Signing out, and logs

Signing out of Studio ends its session, not the one at the provider: the next sign-in through it doesn't ask for a password while the provider's session lasts.

The callback's address holds a one-time authorization code. Studio keeps it out of its own access log, but a proxy in front of it logs it too: it is useless without the secret only the browser that began the sign-in held, but you may want to leave the query out of the proxy's log for `/api/auth/sso/callback`.

### Notes by provider

- **Okta:** the issuer is your org's (`https://example.okta.com`) or an authorization server's (`.../oauth2/default`). For roles, add a groups claim to the ID token, filtered to Studio's groups, and add `groups` to `SCOPES` for the org authorization server.
- **Entra ID:** the issuer is `https://login.microsoftonline.com/<tenant id>/v2.0`. Entra sends no `email_verified`: add the `xms_edov` optional claim, which Studio takes as vouching for the email. `TRUST_EMAIL=1` is safe only for a tenant with no guest users, whose emails aren't the tenant's to vouch for. For roles, define app roles and set `ROLE_CLAIM=roles`: a groups claim names groups by object ID, not name, and is cut off past 200.
- **Google Workspace:** the issuer is `https://accounts.google.com`. Set `HOSTED_DOMAINS` to your workspace's domain: a personal Google account made with a work email has a verified email too, and would otherwise sign in. Google has no groups claim: set roles in Studio.
- **Keycloak:** the issuer is `https://<host>/realms/<realm>`. For roles, add a group membership mapper, with full group path off, or read realm roles with `ROLE_CLAIM=realm_access.roles`, after turning on "Add to ID token" for the realm roles mapper, which adds them only to the access token by default.


## Two-factor sign-in

A password can also ask for a code from an authenticator app -- 1Password, Google Authenticator, Microsoft Authenticator, any that does TOTP -- at each sign-in. Signing in through the identity provider never does: the provider asks for its own second factor.

Anyone with a password sets it up on their Account page, under their name in the sidebar: they scan a QR code, type the code their app shows, and get ten one-time recovery codes for when they lose the app. `BAUTA_STUDIO_REQUIRE_2FA` makes it required:

| `$BAUTA_STUDIO_REQUIRE_2FA` | |
| --- | --- |
| `off` | Anyone may set it up. The default without single sign-on. |
| `admins` | Admins set it up at their next sign-in, before they are signed in, and can't turn it off. The default with single sign-on, when an admin's password is the break-glass way in. |
| `all` | Everyone with a password does. |

An admin resets it on the Users page for someone who lost both their app and their recovery codes; they set it up again.

- **Codes:** each is taken once, and one from a step either side of now is taken too, for a phone whose clock drifts. Ten wrong codes for an account in five minutes, five from one address, or thirty from one address across every account, and more are refused for a while.
- **Secrets** are encrypted with a key Studio makes in its data directory, `secret.key`, readable by its account alone. Studio's database, or a backup of it, isn't enough to make codes without that key. Keep the key apart from the database by naming another file with `$BAUTA_STUDIO_SECRET_KEY_FILE`, and back it up with it. Lose it, and everyone's two-factor sign-in must be reset.
- **Recovery codes** are kept only as their SHA-256, and each signs in once.

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

- **Single sign-on:** the authorization code flow with PKCE. The state that ties a sign-in to the browser that began it is kept in a cookie Studio signs, sent only to the callback and lasting ten minutes. The ID token must be signed by a key the provider publishes, with an asymmetric algorithm, for this client, by this issuer, unexpired, and with the nonce this sign-in sent. The provider's keys are read again within the hour, so a key it withdraws stops being trusted. The authorization code is kept out of the access log. Each sign-in through the provider is taken once, and failed ones are counted by address. Only HTTPS reaches the provider and Studio, but for addresses on the machine itself. A new sign-in ends the session the browser had.
- **Accounts:** argon2 password hashes. Five failed sign-ins for an email from one address in five minutes, thirty from one address across every email, or a hundred for one email across every address, and more are refused for a while; failing from one address doesn't lock the account's owner out from another, so give `--forwarded-allow-ips` your proxy, or every sign-in counts as the proxy's. Only four passwords are checked at once. Sessions are random tokens kept only as their SHA-256, in an HttpOnly, SameSite=Strict cookie, marked Secure unless turned off. Changing a password or deactivating a user signs them out everywhere, and the last admin can't be removed.
- **Requests from other sites:** every change needs the `X-Bauta-Studio` header, which another site can't send without a CORS preflight Studio never answers. Pages carry a strict Content-Security-Policy and can't be framed.
- **What leaves the server:** what `bauta audit` and `bauta history` already show -- names, strategies, key fingerprints, counts, times and errors as history keeps them. Never a masking key, a password, `connections.yaml`, or a value from a table.
- **Editing:** only `jobs.yaml` and the files it includes. A change is written only if the whole configuration still validates with it and the file hasn't changed since it was opened, in one atomic step; the file as it was is kept, and the change is recorded with its diff and who made it.
- **Admins are trusted with the server.** Editing a job changes what SQL runs against your databases and where run state is written, as Studio's account. An edit can't start naming a transform or a masking strategy of your own from a module the jobs files don't already use, beyond bauta's own and `$BAUTA_STUDIO_TRANSFORM_MODULES`: a transform is any callable in any installed module, `os:system` among them, and naming a strategy imports its module. The edit is refused before anything it names is imported. A DuckDB connection's queries can't reach the machine's files either; see [DuckDB](../reference/connections.md#duckdb). That closes the shortest way from an edit to a command; it doesn't make an admin less than someone with that account. Give the role accordingly.
- **Its own files:** the data directory, and the run logs and backups in it, are created readable by Studio's account alone. One that already exists keeps its permissions; `chmod 700` it.
- **Upgrades:** Studio brings its own tables up to date as it starts, keeping what they hold. Back up its database before upgrading, as you would any other.
- **Runs:** each a `bauta run` process in a session of its own, under bauta's own lock, so a run from Studio and one from cron never overlap on the same jobs, and restarting Studio doesn't stop one.

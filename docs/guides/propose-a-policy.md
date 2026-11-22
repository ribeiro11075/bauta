# Propose a policy


```
bauta discover --connection prod --table customers --table orders --target staging --output proposal.yaml
```

For each table, `discover` reads the schema and samples rows (`--sample`, default 1000), then writes a `jobs.yaml` with a proposed policy for every column. Each proposal carries a comment saying what it was based on:

```yaml
    masking:
      key: ${MASKING_KEY}
      columns:
        id: {strategy: keep}  # numeric key (domain customers); use key if the ids themselves are meaningful
        email: {strategy: email}  # name suggests an email address
        phone: {strategy: digits}  # name suggests a phone number
        notes: {strategy: 'null'}  # name suggests free text, which can hold PII anywhere
        status: {strategy: keep}  # no sign of personal data -- review
```

- **Names first, then values.** Column names are matched against [rules](#your-own-rules-discoveryyaml) for common patterns (email, phone, SSN, card, name, address, birth date and so on). A name-based suggestion is dropped if it doesn't fit the column's type, so `place_of_birth` isn't treated as a date. Sampled values are then checked for emails, national identifiers, card numbers (with a Luhn check), IPv4 and IPv6 addresses, MAC addresses, UUIDs, dates, phone numbers and long free text.
- **Inside JSON documents.** A JSON column is read key by key and path by path against the same rules, so `{"contact": {"alt_email": ...}}` is found and proposed as a [`json`](../reference/strategies.md#json) policy naming each path: `{strategy: json, fields: {contact.alt_email: {strategy: email}}}`.
- **Keys are decided together.** Primary keys, the columns that foreign keys reference, and the foreign-key columns themselves get matching domains, so both ends of a relationship agree. Numeric keys are proposed as `keep`, since surrogate ids reveal little, and text keys as `key`. **`--mask-keys` masks the numeric ones too**, in the domain each relationship shares:

  ```yaml
  id: {strategy: key, domain: customers}          # --mask-keys
  customer_id: {strategy: key, domain: customers} # the other end, same domain
  ```

  Both ends move together, so references still match. Use it where the ids themselves are meaningful (sequential ids leak how many customers there are, and when each joined) or must not be production's. A masked primary key makes a later key rotation go through `bauta clear` ([rotating the masking key](rotate-the-key.md)).
- **Sampled values stay in memory.** None of them is printed, logged or written.
- **Load settings.** With a separate `--target`, jobs upsert and load parent tables before child tables. Without one, the proposal masks in place through a `<table>_masked_stage` swap.
- **A whole schema at once.** `--all-tables` proposes for every table, and `--schema NAME` for another schema's. Pair it with [`bauta coverage`](prove-the-copy-is-safe.md#coverage-what-the-jobs-do-not-cover), which fails on anything the jobs then leave out.
- `--output` refuses to overwrite an existing file, so it can't replace a policy that has already been reviewed.

Treat the result as a starting point for review. It isn't a finished policy.

**What every job shares is written once**, under [`defaults`](../reference/jobs.md#defaults): the connections, the insert strategy and the masking key, so each job holds only what a reviewer has to read, and no edit can leave one job under another key. To paste the jobs into a file with defaults of its own, pass `--self-contained`, which writes every setting into every job.


## Keeping a policy up to date

A column added to or dropped from production stops every job reading it, since [every column must be covered](cover-every-column.md). `discover --update` runs each masked job's query and reports both, proposing a policy for each new column:

```
$ bauta discover --update
maskCustomers (configuration/jobs.d/crm.yaml):
  + ssn: {strategy: key}  # name suggests a government identifier; key keeps it unique and shaped
  + home_email: {strategy: email}  # name suggests an email address
  - notes  # no longer returned by sourceQuery
1 of 61 masked job(s) have drifted. Review the proposals above, then apply them with --apply
```

It exits 1 when any job has drifted, so it can run in CI beside [`coverage`](prove-the-copy-is-safe.md#coverage-what-the-jobs-do-not-cover): `coverage` catches a new table, and `discover --update` a new column. `--job NAME` narrows it to some jobs.

**`--apply` writes the change** into each job's file, [included](../reference/configuration.md#splitting-the-jobs-across-files) ones too: new columns at the end of `columns:`, marked `proposed by discover --update`, and stale ones removed, with comments, order and quoting kept. A policy in flow style (`columns: {id: keep}`) is left as it is, and the error says so. Review what it wrote as you would any policy change.

A job reading one table whole gets proposals from the table, so a new foreign key shares its parent's domain; other queries' columns are classified by name and value. A job with `defaultStrategy` reports only its stale columns.

### Your own rules: `discovery.yaml`

The built-in rules are in [`bauta/generate/builtinDiscovery.py`](../../bauta/generate/builtinDiscovery.py), and they recognise English column names and US-shaped identifiers. For anything else, such as a national identifier or column names in another language, put rules of your own in `configuration/discovery.yaml` (or name a file with `--rules FILE`):

```yaml
names:                                  # words in column names
- words: [nif, numero_contribuinte]
  policy: {strategy: key, charset: digits}
  reason: a Portuguese tax number
- words: [nome, apelido]
  policy: fakeName
- words: [office_phone]
  policy: keep                          # a switchboard, not a person
  reason: switchboard numbers
values:                                 # regular expressions sampled values must match
- pattern: '[125689]\d{8}'
  policy: {strategy: key, charset: digits}
  reason: looks like a NIF
personalTables: [clientes, utentes]     # a bare `name` column in these is a person's
exclude: [ip]                           # built-in rules to leave out
```

- **Yours come first**, in the order written, and the first match wins, so a rule of yours overrides a built-in one: above, `office_phone` is kept while `home_phone` is still masked.
- **`names`** match when any of `words` is in the column name, split on underscores and camelCase and also run together, so `numero_contribuinte` matches `NumeroContribuinte`. A rule is skipped for a column whose type its policy doesn't fit.
- **`values`** match when 80% of a column's sampled values match `pattern` in full. Unlike the built-in value rules, yours also read integer columns as their digits.
- **`policy`** is a column policy as in `jobs.yaml`: a strategy name, or a mapping with its options. `reason` is the comment written beside the proposal; without one, it says the rule came from `discovery.yaml`.
- **Leaving built-in rules out.** `exclude` drops built-in rules by name, both forms of each; `builtins: false` drops them all.

| Field | Required or default | Meaning |
| --- | --- | --- |
| `names` | optional | Rules on column names, each with `words`, a `policy` and an optional `reason`. |
| `values` | optional | Rules on sampled values, each with a `pattern`, a `policy` and an optional `reason`. |
| `personalTables` | optional | Words in a table's name that make a bare `name` column in it a person's. |
| `exclude` | optional | Built-in rules to leave out, by the names below. |
| `builtins` | optional, `true` | `false` leaves out every built-in rule. |

| Built-in rule | Recognises |
| --- | --- |
| `email` | email addresses, by name and value |
| `credential` | passwords, secrets, tokens and API keys |
| `nationalId` | SSNs, tax ids, passports and licence numbers, by name; `123-45-6789` by value |
| `card` | card numbers, by name, and by value with a Luhn check |
| `cardSecurityCode` | CVV, CVC and CSC codes, by name, proposed as `null`: PCI DSS forbids storing them |
| `bankAccount` | IBANs, account, routing and sort codes |
| `phone` | phone, mobile and fax numbers, by name and value |
| `firstName`, `lastName`, `fullName` | people's names |
| `userName` | user names and logins |
| `company` | companies and employers |
| `ip` | IP addresses, by name, and IPv4 and IPv6 by value, proposed as [`ip`](../reference/strategies.md#ip), which keeps each an address; a column named for one whose values aren't all addresses gets `hash` |
| `mac` | MAC addresses, by name and by value (`08:00:2b:01:02:03`, `08-00-…`, `0800.2b01.0203`), proposed as `key` with `charset: hex`: a device identifier, kept valid and one-to-one |
| `streetAddress`, `city`, `postalCode` | addresses |
| `birthDate` | dates of birth |
| `compensation` | salaries, income and bonuses |
| `coordinate` | latitudes and longitudes, proposed as [`coordinate`](../reference/strategies.md#coordinate), moved up to a kilometre wherever they are; a longitude takes the table's latitude as `latitudeColumn` where there is exactly one |
| `sensitiveAttribute` | gender, race, ethnicity, religion and nationality, proposed as `null`: special categories of personal data under GDPR, which `shuffle` would leave every real value of in the table |
| `freeText` | notes, comments and descriptions |
| `uuid` | UUIDs, by value, proposed as `key` with `charset: hex`: other systems (logs, a CRM) may hold the same ids, and `key` keeps them unique |
| `date` | ISO dates, by value, proposed as `keep` for review |

The same rules, yours included, decide which unmasked columns [`audit`](prove-the-copy-is-safe.md#reviewing-policies-audit) questions and which columns [`synthesize`](generate-data.md) fills with realistic values. `bauta validate` checks the file: every pattern must compile, every policy must be valid, and every name in `exclude` must be a built-in rule.

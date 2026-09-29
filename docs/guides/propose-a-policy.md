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

- **Names first, then values.** Column names are matched against [rules](#your-own-rules-discoveryyaml) for common patterns (email, phone, SSN, card, name, address, birth date and so on). A name-based suggestion is dropped if it doesn't fit the column's type, so `place_of_birth` isn't treated as a date. Sampled values are then checked for emails, national identifiers, card numbers (with a Luhn check), IP addresses, UUIDs, dates, phone numbers and long free text.
- **Keys are decided together.** Primary keys, the columns that foreign keys reference, and the foreign-key columns themselves get matching domains, so both ends of a relationship agree. Numeric keys are proposed as `keep`, since surrogate ids reveal little, and text keys as `key`. **`--mask-keys` masks the numeric ones too**, in the domain each relationship shares:

  ```yaml
  id: {strategy: key, domain: customers}          # --mask-keys
  customer_id: {strategy: key, domain: customers} # the other end, same domain
  ```

  Both ends move together, so the copy's references still match. Use it where the ids themselves are meaningful — sequential ids leak how many customers there are, and when each was created — or where the copy's ids must not be production's. Note that masking a key the target uses as its primary key means a later key rotation has to go through `bauta clear`; see [rotating the masking key](rotate-the-key.md).
- **Sampled values stay in memory.** None of them is printed, logged or written.
- **Load settings.** With a separate `--target`, jobs upsert and load parent tables before child tables. Without one, the proposal masks in place through a `<table>_masked_stage` swap.
- **A whole schema at once.** `--all-tables` proposes for every table in the database, instead of naming each with a repeated `--table`; `--schema NAME` lists another schema. Pair it with [`bauta coverage`](prove-the-copy-is-safe.md#coverage-what-the-jobs-do-not-cover), which starts from the same list and fails on anything the generated jobs then leave out.
- `--output` refuses to overwrite an existing file, so it can't replace a policy that has already been reviewed.

Treat the result as a starting point for review. It isn't a finished policy.

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

- **Yours come first.** Your rules are tried before the built-in ones, in the order written, and the first that matches wins. So a rule of yours also overrides a built-in one, and `keep` says a column isn't personal after all: above, `office_phone` stays as it is while `home_phone` is still masked.
- **`names`.** A rule matches when any of its `words` is in the column name. Names are split on underscores and camelCase, and also matched run together, so `numero_contribuinte` matches `NumeroContribuinte` and `numero_contribuinte_cliente`. As with the built-in rules, a rule is skipped for a column whose type its policy doesn't fit.
- **`values`.** A rule matches when at least 80% of a column's sampled values match its `pattern` in full: `\d{9}` matches `501234567`, not `NIF 501234567`. Unlike the built-in value rules, which read text only, yours also read integer columns as their digits, since a tax number is often stored as one.
- **`policy`** is a column policy as in `jobs.yaml`: a strategy name, or a mapping with its options. `reason` is the comment written beside the proposal; without one, it says the rule came from `discovery.yaml`.
- **Leaving built-in rules out.** `exclude` names built-in rules to drop, and one name covers a rule's name and value forms both: leaving out `phone` means nothing is taken for a phone number. `builtins: false` drops them all, `personalTables` included, leaving only yours.

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
| `bankAccount` | IBANs, account, routing and sort codes |
| `phone` | phone, mobile and fax numbers, by name and value |
| `firstName`, `lastName`, `fullName` | people's names |
| `userName` | user names and logins |
| `company` | companies and employers |
| `ip` | IP addresses, by name, and IPv4 by value |
| `streetAddress`, `city`, `postalCode` | addresses |
| `birthDate` | dates of birth |
| `compensation` | salaries, income and bonuses |
| `coordinate` | latitudes and longitudes |
| `sensitiveAttribute` | gender, race, ethnicity, religion and nationality |
| `freeText` | notes, comments and descriptions |
| `uuid`, `date` | UUIDs and ISO dates, by value, proposed as `keep` for review |

The same rules, yours included, decide which unmasked columns [`audit`](prove-the-copy-is-safe.md#reviewing-policies-audit) questions and which columns [`synthesize`](generate-data.md) fills with realistic values. `bauta validate` checks the file: every pattern must compile, every policy must be valid, and every name in `exclude` must be a built-in rule.

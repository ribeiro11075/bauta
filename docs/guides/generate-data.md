# Generate data


Some tables can't be copied at all, even masked, and a new system may have no production data yet. `synthesize` fills existing tables with generated rows, using nothing but the target's own catalog:

```
bauta synthesize --connection staging --table customers:1000 --table orders:5000 --dry-run
bauta synthesize --connection staging --table customers:1000 --table orders:5000 --yes
```

```
customers: 1000 row(s)
  id                           primary key  sequential, from 1
  email                        name         an email address at example.test
  first_name                   name         a first name
  phone                        name         digits shaped like +1 555 010 0000
  birth_date                   name         a birth date between 1940 and 2004
  balance                      type         a decimal with 2 places, sometimes NULL
  sample: {'id': 1, 'email': 'u5e9bbb2e91df@example.test', 'first_name': 'Hugo', ...}
```

- **Keys are unique.** Integer keys continue after the table's maximum, text keys run `S1`, `S2`, ... (zero-padded in a fixed-width column), and UUID keys are generated.
- **Values continue between runs.** Generators are indexed by row number, counted from the rows already there, so a second run doesn't repeat or collide with the first.
- **Foreign keys resolve.** Values are drawn from the parent's rows, and parents are filled first, whatever the `--table` order. A table keyed only by foreign keys gets as many rows as its parents allow.
- **Names drive realism.** Columns named like personal data get realistic values, by the rules `discover` uses ([your own](propose-a-policy.md#your-own-rules-discoveryyaml) included). Everything else is random within its type, dates from 2015 to the end of this year. Nullable columns are NULL about one time in ten.
- **Reproducible.** The same `--seed` on the same starting tables makes the same rows (within a calendar year, as the date span grows each January).
- **CHECK constraints are kept** where they list values (`status IN (...)`) or bound a number (`amount > 0 AND amount < 1000`). Any other check, such as `start_date <= end_date`, is named as `synthesize` starts, since rows breaking it will be refused.

### Shaped like production: `--profile`

```
bauta synthesize --connection staging --table orders:50000 --profile production --dry-run
```

`--profile ALIAS` shapes the rows like the same table in another connection: each column NULL as often, numbers and dates within the same range, and a column of few values (`status`, `region`) taking each label as often. **Only aggregates are read**, never a row:

- columns named like personal data, and keys, are never read;
- labels come only from columns of at most 20 values, and only those at least 5 rows share, so a value rare enough to point at someone is never copied;
- a number's or date's extremes are read, so leave out a column whose extremes are sensitive, or name it in [your own rules](propose-a-policy.md#your-own-rules-discoveryyaml).

A CHECK takes precedence over the profile, and the profile over names. `--dry-run` shows which each column follows.
- `--rows` sets the count for any `--table` given without one. Nothing is written without `--yes`.

**Limits:** a table referencing itself through a NOT NULL column can't be filled (a nullable self-reference is left NULL). A non-key `UNIQUE` column is satisfied only where its type has room: a name or short code can repeat, and the database's refusal says how many rows were already committed. Even with `--profile`, columns are shaped one by one, with no correlations between them. Tables must exist first; `schema` creates them.

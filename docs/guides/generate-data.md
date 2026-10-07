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

- **Keys are unique.** Integer keys continue after the table's current maximum; text keys run `S1`, `S2`, ... after the current row count (`S0001`, `S0002`, ... in a fixed-width column, so each stays distinct at full width); UUID keys are generated.
- **Values continue between runs.** Every generator is indexed by the row's number, counted from the rows already in the table, so a second run neither repeats the first's values nor collides with them. Generated text ends in that number, so a `UNIQUE` column of any reasonable width keeps taking rows.
- **Foreign keys resolve.** Values are drawn from the parent's existing rows, so parents are filled first; `--table` order doesn't matter. A table whose key is made only of foreign keys gets as many rows as its parents allow, which may be fewer than asked. Only such a table's keys are remembered, to skip a combination drawn twice; any other key has a generated part that can't repeat, so filling it holds a chunk in memory however many rows are asked for.
- **Names drive realism.** Columns whose names suggest personal data (email, names, phone, postal code, birth date, city, company, address...) get realistic values, from the same rules `discover` uses, [your own](propose-a-policy.md#your-own-rules-discoveryyaml) included. Everything else is random within its type: numbers within their precision, text within its length, dates from 2015 to the end of the current year. The span grows each January, so a seed gives the same rows all year and other dates in the next. Nullable columns are NULL about one time in ten.
- **Reproducible.** The same `--seed` on the same starting tables makes the same rows.
- **CHECK constraints are kept.** A check listing a column's values (`status IN ('open', 'closed')`) has the column take only those, and one bounding a number (`amount > 0 AND amount < 1000`, `pct BETWEEN 1 AND 100`) keeps it within the bounds, at the column's own type and scale -- read from each database's catalog, however it spells them. Any other check, such as one comparing two columns (`start_date <= end_date`), is named as `synthesize` starts, since rows breaking it will be refused; such a refusal says which kind of check it was.

### Shaped like production: `--profile`

```
bauta synthesize --connection staging --table orders:50000 --profile production --dry-run
```

`--profile ALIAS` reads the same table in another connection and shapes the rows like it: each column NULL as often as there, a number or a date within the range it spans there, and a column of few values -- `status`, `plan`, `region` -- taking each of its labels as often as there. **Only aggregates are read**, so no row of production reaches the generated ones:

- a column named like personal data (the rules `discover` uses) is never read, nor is a key;
- labels are read only from a column of at most 20 values, and only those at least 5 rows share, so a value rare enough to point at someone -- a one-off status naming a customer -- is never copied;
- the smallest and largest value of a number or date are, so leave a column whose extremes are themselves sensitive out of the tables you profile, or out of `--profile` by giving it a name the rules recognise in [your own rules](propose-a-policy.md#your-own-rules-discoveryyaml).

A CHECK takes precedence over the profile, and the profile over names. `--dry-run` shows which each column follows.
- `--rows` sets the count for any `--table` given without one. Nothing is written without `--yes`.

**Limits:** a table that references itself through a NOT NULL column can't be filled, since its first row would have nothing to point at; a nullable self-reference is left NULL. A `UNIQUE` constraint on a column that isn't the primary key is satisfied only where the column's type has room for one value per row: generated text ends in the row's number, but a name, a short code or a number can repeat. Where the database refuses such a row, `synthesize` says which table refused it and how many rows were inserted before it, since each chunk is already committed. Without `--profile`, values are plausible, not statistically like production; with it, each column is spread as production's is, but each on its own: there are no correlations between columns. Tables must exist first; `schema` creates them from a source's definitions.

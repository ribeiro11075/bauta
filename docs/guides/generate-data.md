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
- **Names drive realism.** Columns whose names suggest personal data (email, names, phone, postal code, birth date, city, company, address...) get realistic values, from the same rules `discover` uses, [your own](propose-a-policy.md#your-own-rules-discoveryyaml) included. Everything else is random within its type: numbers within their precision, text within its length, dates since 2015. Nullable columns are NULL about one time in ten.
- **Reproducible.** The same `--seed` on the same starting tables makes the same rows.
- `--rows` sets the count for any `--table` given without one. Nothing is written without `--yes`.

**Limits:** a table that references itself through a NOT NULL column can't be filled, since its first row would have nothing to point at; a nullable self-reference is left NULL. A `UNIQUE` constraint on a column that isn't the primary key is satisfied only where the column's type has room for one value per row: generated text ends in the row's number, but a name, a short code or a number can repeat. Where the database refuses such a row, `synthesize` says which table refused it and how many rows were inserted before it, since each chunk is already committed. Values are plausible, not statistically like production: there are no correlations between columns, and no skew. Tables must exist first; `schema` creates them from a source's definitions.

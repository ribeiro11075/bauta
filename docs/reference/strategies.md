# Masking strategies


A NULL stays NULL under every strategy except `constant` and `null`.

| Strategy | Result | Options |
| --- | --- | --- |
| `keep` | Unchanged. The explicit way to say a column was reviewed. | none |
| `null` | NULL. The right choice for free text. | none |
| `constant` | `value` in every row, NULLs included. | `value` (required) |
| `hash` | An opaque hex token, e.g. `cust_9f86d081884c7d65`. | `length` (12–64, default 16), `prefix`; `normalize`: `strip`, `lower` ([below](#values-held-more-than-one-way-normalize)) |
| `email` | Still an email address, e.g. `u9f86d081884c@example.test`. Keyed on the lower-cased address. | `length` (8–40, default 12), `mailDomain` (default `example.test`), `keepDomain` |
| `digits` | Each digit replaced, everything else kept: `+1 (555) 010-9999` → `+1 (831) 402-5517`. Keyed on the digits alone, so formatting doesn't matter. Integers keep their digit count. Digits in any script (full-width `１２３`, Arabic-Indic `١٢٣`) are masked too, and written back in their own script. A value the keeps cover entirely fails the job rather than being copied through: `555-0100` under `keepLeading: 3, keepTrailing: 4`. | `keepLeading`, `keepTrailing` (e.g. `4` for a card number) |
| `number` | A number of the same type and precision, either within `variance` of the original (default `0.1`) or within `min`–`max`. A value the variance would round back to itself, such as a small integer, moves one step instead; zero stays zero. | `min` + `max`, or `variance` (0–1); `decimals` |
| `dateShift` | Moved by a keyed number of whole days, never zero. The shift is keyed on the day, so a date, a timestamp and ISO text of that same day all move to the same day — and two days move by unrelated amounts, so **a start and an end can change order**. `shiftBy` moves every date of one person alike instead; see [below](#dateshift-by-a-column-shiftby). Times of day are kept. ISO 8601 text, which is how SQLite stores dates, is written back in the same format. `0001-01-01` and `9999-12-31` are kept, since they mean "no date" or "forever"; a date near either is shifted away from it. | `maxDays` (default 30); `shiftBy`: a column of the row |
| `key` | A one-to-one mapping, safe for primary and foreign keys. See below. | `charset`: `alphanumeric` (default), `digits`, `hex`; `normalize`: `strip`, `lower`, `integer` |
| `fpe` | Like `key`, but using NIST's FF1 format-preserving encryption, for policies that must name a standard. See below. | `charset`: `alphanumeric` (default), `digits`, `hex`; `strict`; `normalize`: `strip`, `lower`, `integer` |
| `fakeName`, `fakeFirstName`, `fakeLastName`, `fakeCity`, `fakeCompany`, `fakeStreetAddress` | Realistic values from bundled lists. Not unique. | `maxLength`; `locale`; `lists` (1 or 2), and `matchGender` for the name strategies; [below](#fake-data-by-country) |
| `coordinate` | A latitude or longitude moved along its axis by a keyed distance on the ground, between half of `meters` and all of it, wherever the point is. See [below](#coordinate). | `axis` (required): `latitude` or `longitude`; `meters` (default 1000); `latitudeColumn`, for a longitude |
| `redact` | Free text with each recognisable identifier replaced: emails, phone numbers, US SSNs, card numbers and IBANs (both checksum-verified), IPv4 addresses. **Names aren't found.** See below. | `replacement`: `label` (default) or `mask`; `detect`: a list of `email`, `phone`, `ssn`, `card`, `iban`, `ip`; `patterns`: extra regular expressions |
| `json` | A JSON document with each path `fields` names masked by its own policy, and every other value by `otherwise`. See below. | `fields` (required): path → column policy; `otherwise`: a policy, default `redact` with `replacement: mask` |
| `shuffle` | The column's values rearranged among rows in the same chunk. **Not anonymization:** every real value is still in the table, and a small chunk barely moves them. It moves values between rows rather than mapping them, so it breaks a key and its references even where both are shuffled alike; `audit` warns. See [limits](#limits). | none |

A value a strategy can't handle fails the job, for example text given to `number`. The error names the column and the value's type, never the value itself.

## `key`

`key` guarantees that different inputs give different outputs. That's what a primary key needs: a hash reduced to a column's width would eventually collide. It is a keyed permutation: a Feistel network with HMAC-SHA256 rounds. That's the same structure as the NIST FF1 and FF3-1 standards, but not a certified implementation of either, and it adds no dependency.

The output has the same shape as the input:

- An integer keeps its sign and number of digits.
- Text keeps its length, and every character that isn't masked stays put. With `alphanumeric`, digits map to digits and letters to letters of the same case. `digits` masks only digits. `hex` masks hex characters the same way — digits to digits, `a-f` to `a-f`, `A-F` to `A-F` — which suits UUIDs and hex tokens, and keeps two spellings of one value apart.
- A UUID object stays a UUID. Its version digit isn't preserved.

`charset` is set once per column rather than detected from each value, because detection could give two different shapes the same output.

**Only ASCII letters and digits are masked**, so a value with letters or digits in another script fails the job rather than being copied through: `Дмитрий`, `王伟`, `José` or `١٢٣` under `alphanumeric`, and digits outside 0-9 under `digits` or `hex`. Other characters (spaces, punctuation, `€`) are kept as they are. Use `hash`, a `fake*` strategy or `null` for names in any script, and the `digits` strategy for numbers written in other digits.

**Values are limited to 256 characters**, or 256 digits for an integer. `key` is for identifiers, and its cost grows with the square of a value's length; a longer value fails the job with a suggestion of `hash`, `redact` or `null`. The same limits apply to `fpe`.

`number` handles ordinary numeric columns. `key` is for identifiers, whose values have to stay distinct. `number` computes to 60 significant digits: an integer of more than 60 digits, or a float held to more `decimals` than 60 digits reach at its size, fails the job with an error naming the column.

**A mask can be wider than the column.** Keeping a value's shape is not the same as fitting where it came from:

- `key` keeps an integer's digit count, and a column's range doesn't stop at a digit boundary. A 10-digit value in an `INT` column masks to another 10-digit value, and most of those are above 2147483647 — the load then fails with the database's own "out of range". A `BIGINT` has the same problem at 19 digits. Mask such a key into a wider column, or with `hash` where it need not stay a number.
- `number` varies a value that may already be at its column's limit: `9999999999.99` in a `DECIMAL(12,2)` with the default variance overflows about half the time. Bound it with `min` and `max`, which is what they are for. SQLite is the exception, and not in your favour: it ignores a column's declared precision and stores the wider value as it is.

The mask is the same for a given value, key and domain wherever it appears, so it cannot be narrowed to fit one column without breaking the joins it exists to keep. A job that fails this way logs which of the two is likely responsible.

## `fpe`

`fpe` is `key`'s alternative for when a security review asks for a published algorithm: FF1 from NIST SP 800-38G Rev. 1, with AES-256. It is checked against NIST's sample vectors, and needs the `cryptography` package (`pip install "bauta[fpe]"`; the `oracle` extra already brings it).

- It keeps shapes the way `key` does: integers keep sign and digit count, text keeps its length and every character outside `charset`. FF1 needs one alphabet for every position, so with `alphanumeric` a letter may become a digit, and with `hex` a masked value may mix cases (`2cAB74Ce`); `key` keeps each character's class and case.
- The masking key is turned into an AES key per domain, and the domain goes into FF1's tweak.
- **FF1 needs at least a million possible values**: six digits, five hex characters or four alphanumerics. Shorter values are masked with `key`'s permutation instead, and still never collide with longer ones, since lengths are kept. **`strict: true`** fails the job on a shorter value instead, for policies that require FF1 for every value; the error gives the minimum length, never the value. `audit` notes each `fpe` column without `strict`.
- In pure Python it is slower than `key`; with the native masker it is faster: with two `fpe` columns among six, a million rows run at 254,000 rows a second, against 129,000 with `key` (see [speed](../guides/make-it-faster.md#what-each-strategy-costs)). Repeated values are remembered, as described under [speed](../guides/make-it-faster.md#what-each-strategy-costs).

Only encryption is implemented. Nothing in the package can reverse a mask.

## Values held more than one way: `normalize`

Masks agree when values are equal byte for byte, and a database's idea of equal is often looser. Three cases break joins in a copy without anything failing:

- **Padding.** A `CHAR(10)` column holds `AB12` as `AB12      `. SQL Server, MySQL and Oracle compare the two as equal, and a `VARCHAR` key elsewhere holds `AB12`; masked, they differ.
- **Case.** A case-insensitive collation joins `AB12` to `ab12`; `hash` masks them differently.
- **Type.** One table holds a customer id as an integer, another as text. `hash` keys both on the same digits, but `key` and `fpe` keep a value's type, so `42` and `'42'` mask differently.

`normalize` lists the steps to apply to text before it is masked, always in this order whatever order they are written in:

| Step | Takes | For |
| --- | --- | --- |
| `strip` | `hash`, `key`, `fpe` | leading and trailing spaces removed |
| `lower` | `hash`, `key`, `fpe` | lower-cased |
| `integer` | `key`, `fpe` | text that spells an integer one way only (`42`, `-7`, not `042` or `+7`) masked as that integer, and written back as text |

```yaml
columns:
  customer_ref: { strategy: key, domain: customers, normalize: [strip, integer] }   # '42  ' masks as customers.id 42 does
  account_code: { strategy: hash, domain: accounts, normalize: [strip, lower] }
```

Normalize every column of a domain the same way, or none: a padded value masks as the unpadded one only where both are stripped. Values that aren't text are masked as they are. [`audit --connect`](../guides/prove-the-copy-is-safe.md#reviewing-policies-audit) samples each query and warns about a domain holding ids as numbers in one column and as text in another, or padded in one and not another. `email` already strips and lower-cases an address.

## `dateShift` by a column: `shiftBy`

Keyed on the day, every date on one day moves by the same amount, which keeps a day's events together, but the next day moves by an unrelated amount. So an admission on the 1st and a discharge on the 2nd can come out a month apart in either order, and a constraint such as `CHECK (discharged >= admitted)` fails in the copy.

`shiftBy` names a column of the row whose value the shift is keyed on instead, usually the person the row is about:

```yaml
columns:
  patient_id: { strategy: key, domain: patients }
  admitted:   { strategy: dateShift, shiftBy: patient_id, maxDays: 180 }
  discharged: { strategy: dateShift, shiftBy: patient_id, maxDays: 180 }
```

- **Every date of one person moves by the same number of days**, in every column and table that shifts by a column of that name, so their order and the time between them are kept. The shift is keyed on the `shiftBy` column's value as read from the source, before it is masked.
- **The domain** is the `shiftBy` column's name, lower-cased, unless the policy names one, so the dates of one person agree across tables whatever each date column is called. Give columns that should move together the same `maxDays`, or they agree in direction only.
- **A row whose `shiftBy` value is NULL** is shifted by the day.
- **What it gives away**: anyone who knows one real date of a person, and finds its mask, knows the shift for every other date of theirs. Keyed on the day, knowing one date reveals only that day's. That is the price of keeping intervals, and why `maxDays` matters more here: a wider window hides more.

The column must be one `sourceQuery` returns, other than the date column itself, or the job fails before writing anything.

## `coordinate`

`number` with a `variance` moves a value by a share of itself, so a coordinate near the equator or the prime meridian barely moves: 1% of London's longitude is about 60 metres. `coordinate` moves a point by a distance on the ground instead, between half of `meters` and all of it, in a keyed direction along its axis, wherever the point is:

```yaml
columns:
  lat: { strategy: coordinate, axis: latitude, meters: 2000 }
  lng: { strategy: coordinate, axis: longitude, meters: 2000, latitudeColumn: lat }
```

- **A degree of longitude spans fewer metres away from the equator.** `latitudeColumn` names the row's latitude, read before it is masked, so a longitude moves by `meters` at that latitude. Without it a longitude moves by `meters` as measured at the equator: half that at 60°.
- The move is keyed on the value, so equal coordinates mask alike. A latitude that would pass a pole moves the other way, and a longitude wraps at 180°.
- Floats come back as floats. A `Decimal` keeps its scale and moves at least one step of it; a column of two decimal places (about a kilometre) can't hold a smaller move.
- [`discover`](../guides/propose-a-policy.md) proposes `coordinate` for columns named like a latitude or a longitude, with `latitudeColumn` where the table has one latitude.

## Fake data by country

The `fake*` strategies draw from an international mix of names and places by default. `locale` picks one country's names, cities and address layout instead: `en_US`, `en_GB`, `de_DE`, `fr_FR`, `es_ES`, `it_IT`, `nl_NL` or `pt_BR`.

```yaml
columns:
  full_name: { strategy: fakeName, locale: de_DE }          # Lukas Schneider
  street:    { strategy: fakeStreetAddress, locale: fr_FR } # 12 rue des Lilas
```

Leaving `locale` out keeps the original lists, so existing masks don't change.

### The larger lists: `lists: 2`

The default lists are short: a few dozen first names and surnames per locale, so a million customers share a few hundred full names, and a search or de-duplication feature tested on them sees an unrealistic world. **`lists: 2`** picks from longer ones: about 250 first names, 160 to 300 surnames and 70 to 90 cities in each locale, and every locale's together without `locale`. Masks made without it are unchanged.

With `lists: 2` the name strategies also agree with one another:

- **A full name masks part by part**: its given names as `fakeFirstName` masks them, its surname as `fakeLastName` does, with a particle (`de Jong`, `van der Berg`, `da Silva`) kept with the surname, and `Surname, Given` kept in that order. So `John Smith` in a `full_name` column agrees with `John` and `Smith` in `first_name` and `last_name`.
- **They mask in one domain**, `fake name`, rather than each column's own, so the agreement holds across tables too. A `domain` on the policy overrides it, as for any strategy.
- **Names are matched as a person writes them**, whatever their case or spacing, and written back in the original's case: `JOHN` becomes, say, `JORGE` where `John` becomes `Jorge`.
- **`matchGender: true`** replaces a first name with one of the same gender, where the lists know the name as a woman's or a man's in any locale; a name they don't know, or know as both, gets one of either. That keeps a gender column and the names beside it plausible together — and so keeps the gender a name suggests, which is a reason to leave it off.

```yaml
columns:
  first_name: { strategy: fakeFirstName, lists: 2, matchGender: true }
  last_name:  { strategy: fakeLastName, lists: 2 }
  full_name:  { strategy: fakeName, lists: 2, matchGender: true }   # agrees with the two above
```

`lists: 2` gives `fakeCity` the longer city lists; `fakeCompany` and `fakeStreetAddress` are the same in both. It is masked in Python: the native masker covers `lists: 1`.

## `redact`

`redact` is for free text worth keeping, like support tickets, where `null` would lose too much:

```yaml
columns:
  ticket_body: { strategy: redact }
  # Called Ann at [PHONE], card [CARD] was declined, reply to [EMAIL]
  agent_notes: { strategy: redact, replacement: mask, patterns: ['ACC-\d{6}'] }
  # Called Ann at +7 (362) 248-5677, card 4095 9565 2378 1111 was declined, account redacted-b934cf4da0a9
```

- `label` writes `[EMAIL]`, `[PHONE]`, `[SSN]`, `[CARD]`, `[IBAN]`, `[IP]`, or `[REDACTED]` for your own `patterns`.
- `mask` writes keyed values of the same shape instead: the same address always becomes the same masked address, a card keeps its last four digits, an IP becomes `10.x.x.x`.
- Phone detection is broad on purpose: any run of 7–15 digits that isn't a date counts, order numbers included. Leave `phone` out of `detect` where that removes too much.
- **It can't recognise names, addresses written in words, or anything else without a fixed shape.** "Call Maria about her divorce" passes through unchanged. Where text may hold that, use `null`. `audit` notes every `redact` column for this reason.

## `json`

`json` masks inside a JSON document -- PostgreSQL's `json` and `jsonb`, Oracle's `JSON`, MySQL's `JSON`, or text holding one -- field by field:

```yaml
columns:
  preferences:
    strategy: json
    fields:
      contact.alt_email: email
      contact.owner_id: { strategy: key, domain: customers }   # matches customers.id masked in domain customers
      family[].first_name: fakeFirstName
      address: 'null'                                           # an object, dropped whole
    otherwise: { strategy: redact, replacement: mask }         # the default
```

- **Paths** join keys with dots, and name every element of an array with `[]`: `orders[].card`, or `[].email` for a document that is an array.
- **A field's policy** is any column policy but `shuffle`, which has only the one value to move, and one naming another column of the row (`shiftBy`, `latitudeColumn`), which a value in a document has none of. It masks in the `domain` it names or, like a column, in its own name's: the path's last key. A policy on an object or an array applies to it whole.
- **`otherwise`** masks every value no field names. The default, `redact`, masks identifiers it finds by shape in text, and in whole numbers long enough to be a phone or card number (`5550109999` masks as `"555-010-9999"` would, and stays a number), and leaves other numbers and booleans as they are. **A name no field names passes through**; `audit` notes each `json` column this applies to. `otherwise: 'null'` removes every value no field names instead.
- **Keys are data too** where a document is keyed by, say, email address: `{"ann@corp.example": {...}}`. Identifiers `redact` finds in an object's keys are masked, unless `otherwise` is `keep`. Paths in `fields` match keys as they came.
- The document comes back in the form it came: an object as an object, JSON text as JSON text, keys in their order.

[`discover`](../guides/propose-a-policy.md) proposes a `json` policy for a JSON column whose sampled documents hold personal data, naming each path it found.

`email`, `digits`, `key`, `fpe` and `redact` also take a JSON document or an IP address -- a document as its JSON text, an `inet` value as it is written -- and return masked text, which a JSON or `inet` column loads as it would the literal.

## Your own strategies

A policy can name a class of your own as `module.path:ClassName`:

```python
# acme/masks.py
from bauta.masking import Strategy, canonical

class Initials(Strategy):
    OPTIONS = {'separator': str}                   # option name -> check that returns the value

    def mask(self, value):                          # called for each non-NULL value
        separator = self.options.get('separator', '.')
        suffix = self.keyedHash.digest(canonical(value)).hex()[:4]
        return separator.join(word[0] for word in value.split()) + separator + suffix
```

```yaml
columns:
  full_name: { strategy: "acme.masks:Initials", separator: "-" }
```

Derive anything random from `self.keyedHash` (`digest`, `below`, `unit`, `permute`), so the mask stays keyed, consistent within its domain, and reproducible. Key it on `canonical(value)`, the bytes the built-in strategies key on, so the same id masks the same way whether a driver returned it as a number or as text. `validate` imports the class and checks its options; the module must also be importable wherever jobs run. The manifest records the strategy by the name the policy used.

If `mask()` depends on nothing but the value, set `CACHEABLE = True` on the class, and repeated values are remembered rather than masked again. It's off by default, since a strategy could depend on something else. A strategy that returns every value exactly as it was given, as `keep` does, sets `PASSTHROUGH = True`: its columns are carried through untouched, and checks that refuse a masked watermark column or a masked primary key treat them as unmasked.

A strategy that masks parts of a value in domains other than its column's, as `json` masks its fields, overrides `bindKey(self, key)`: a masking plan calls it once, after building the strategy, with the masking key, to build a `KeyedHash(key, domain)` for each. The rest need only `self.keyedHash`.


## Limits

- **Free text** can hold personal data anywhere in it. `null` or `constant` remove it all. `redact` keeps the text and removes identifiers with a recognisable shape, but not names. `hash` would only replace the text with an opaque token, and `keep` would copy it as it is.
- **Scripts other than Latin.** `key` and `fpe` refuse letters and digits outside ASCII rather than copy them; `digits` and `redact` handle digits in any script. `redact` finds only email addresses written in ASCII.
- **Unique columns** need enough bits to avoid collisions. `hash` enforces a minimum length for that reason. The `fake*` strategies are never unique. For a unique column, use `key`, which never collides.
- **`number` with `variance`** keeps magnitudes realistic, which also reveals them roughly. Use `min`/`max` if the magnitude itself is sensitive.
- **`dateShift`** is keyed on the date, so everyone born on the same day still shares a birthday after masking, and a day's events stay a day's events whether the column holds a date or a timestamp. That's what keeps the data consistent, and it means dates are shifted, not randomized. It also means two dates in a row move independently and can change order; use [`shiftBy`](#dateshift-by-a-column-shiftby) where their order or the time between them matters.
- **`shuffle` needs large chunks.** Values only move within a chunk, so a row keeps its own value with probability 1/chunk size, and a chunk of one row isn't shuffled at all. The last chunk of a load and a small incremental run are both small. Don't use `shuffle` on incremental jobs.
- **Masking hides values, not patterns.** Row counts, NULL rates and relationships are all preserved, which is the point, and a combination of kept columns (zip code, birth year and gender) can still identify someone. Review what you `keep`.
- **Hard deletes** aren't propagated by incremental loads, masked or not. See [design.md](../concepts/how-it-works.md#deletes).

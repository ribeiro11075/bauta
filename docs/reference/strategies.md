# Masking strategies


A NULL stays NULL under every strategy except `constant` and `null`.

| Strategy | Result | Options |
| --- | --- | --- |
| `keep` | Unchanged. The explicit way to say a column was reviewed. | none |
| `null` | NULL. The right choice for free text. | none |
| `constant` | `value` in every row, NULLs included. | `value` (required) |
| `hash` | An opaque hex token, e.g. `cust_9f86d081884c7d65`. | `length` (12–64, default 16), `prefix` |
| `email` | Still an email address, e.g. `u9f86d081884c@example.test`. Keyed on the lower-cased address. | `length` (8–40, default 12), `mailDomain` (default `example.test`), `keepDomain` |
| `digits` | Each digit replaced, everything else kept: `+1 (555) 010-9999` → `+1 (831) 402-5517`. Keyed on the digits alone, so formatting doesn't matter. Integers keep their digit count. Digits in any script (full-width `１２３`, Arabic-Indic `١٢٣`) are masked too, and written back in their own script. A value the keeps cover entirely fails the job rather than being copied through: `555-0100` under `keepLeading: 3, keepTrailing: 4`. | `keepLeading`, `keepTrailing` (e.g. `4` for a card number) |
| `number` | A number of the same type and precision, either within `variance` of the original (default `0.1`) or within `min`–`max`. A value the variance would round back to itself, such as a small integer, moves one step instead; zero stays zero. | `min` + `max`, or `variance` (0–1); `decimals` |
| `dateShift` | Moved by a keyed number of whole days, never zero. The shift is keyed on the day, so a date, a timestamp and ISO text of that same day all move to the same day. Times of day are kept. ISO 8601 text, which is how SQLite stores dates, is written back in the same format. `0001-01-01` and `9999-12-31` are kept, since they mean "no date" or "forever"; a date near either is shifted away from it. | `maxDays` (default 30) |
| `key` | A one-to-one mapping, safe for primary and foreign keys. See below. | `charset`: `alphanumeric` (default), `digits`, `hex` |
| `fpe` | Like `key`, but using NIST's FF1 format-preserving encryption, for policies that must name a standard. See below. | `charset`: `alphanumeric` (default), `digits`, `hex`; `strict` |
| `fakeName`, `fakeFirstName`, `fakeLastName`, `fakeCity`, `fakeCompany`, `fakeStreetAddress` | Realistic values from bundled lists. Not unique. | `maxLength`; `locale`, below |
| `redact` | Free text with each recognisable identifier replaced: emails, phone numbers, US SSNs, card numbers and IBANs (both checksum-verified), IPv4 addresses. **Names aren't found.** See below. | `replacement`: `label` (default) or `mask`; `detect`: a list of `email`, `phone`, `ssn`, `card`, `iban`, `ip`; `patterns`: extra regular expressions |
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

## Fake data by country

The `fake*` strategies draw from an international mix of names and places by default. `locale` picks one country's names, cities and address layout instead: `en_US`, `en_GB`, `de_DE`, `fr_FR`, `es_ES`, `it_IT`, `nl_NL` or `pt_BR`.

```yaml
columns:
  full_name: { strategy: fakeName, locale: de_DE }          # Lukas Schneider
  street:    { strategy: fakeStreetAddress, locale: fr_FR } # 12 rue des Lilas
```

Leaving `locale` out keeps the original lists, so existing masks don't change.

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


## Limits

- **Free text** can hold personal data anywhere in it. `null` or `constant` remove it all. `redact` keeps the text and removes identifiers with a recognisable shape, but not names. `hash` would only replace the text with an opaque token, and `keep` would copy it as it is.
- **Scripts other than Latin.** `key` and `fpe` refuse letters and digits outside ASCII rather than copy them; `digits` and `redact` handle digits in any script. `redact` finds only email addresses written in ASCII.
- **Unique columns** need enough bits to avoid collisions. `hash` enforces a minimum length for that reason. The `fake*` strategies are never unique. For a unique column, use `key`, which never collides.
- **`number` with `variance`** keeps magnitudes realistic, which also reveals them roughly. Use `min`/`max` if the magnitude itself is sensitive.
- **`dateShift`** is keyed on the date, so everyone born on the same day still shares a birthday after masking, and a day's events stay a day's events whether the column holds a date or a timestamp. That's what keeps the data consistent, and it means dates are shifted, not randomized.
- **`shuffle` needs large chunks.** Values only move within a chunk, so a row keeps its own value with probability 1/chunk size, and a chunk of one row isn't shuffled at all. The last chunk of a load and a small incremental run are both small. Don't use `shuffle` on incremental jobs.
- **Masking hides values, not patterns.** Row counts, NULL rates and relationships are all preserved, which is the point, and a combination of kept columns (zip code, birth year and gender) can still identify someone. Review what you `keep`.
- **Hard deletes** aren't propagated by incremental loads, masked or not. See [design.md](../concepts/how-it-works.md#deletes).

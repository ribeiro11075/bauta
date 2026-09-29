# Keep joins intact


Every mask is derived from the key, a **domain**, and the value itself:

```
mask = strategy( HMAC(key, domain, value) )
```

The same value in the same domain always masks the same way, in every table and on every run. So joins survive masking as long as both sides share a domain and a strategy:

```yaml
# customers
id:          { strategy: key, domain: customer }
# orders
customer_id: { strategy: key, domain: customer }
```

The domain defaults to the column's lower-cased name, so `email` in two tables already agrees without any configuration. Set `domain` explicitly whenever the two sides of a relationship have different names.

Masking is also reproducible. Masks don't depend on row order or a random seed, so a re-run or next week's incremental load produces the same values. The one exception is `shuffle`, which depends on how rows fall into chunks.

Numbers are keyed on their decimal text. An id read as an `int` from one database, as a `Decimal` from another, or as `'42'` from a text column therefore masks the same way under `hash`. `key` is stricter, since its output keeps the input's type: pick one type per domain.

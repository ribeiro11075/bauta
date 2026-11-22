# Keep joins intact


Every mask is derived from the key, a **domain**, and the value itself:

```
mask = strategy( HMAC(key, domain, value) )
```

The same value in the same domain masks the same way in every table and on every run, so a join survives masking when both sides share a domain and a strategy:

```yaml
# customers
id:          { strategy: key, domain: customer }
# orders
customer_id: { strategy: key, domain: customer }
```

The domain defaults to the column's lower-cased name, so `email` in two tables agrees already; set `domain` where the two sides are named differently.

Masks depend on nothing but the value, key and domain, not row order or a seed, so a rerun or next week's incremental load gives the same values. `shuffle`, which depends on how rows fall into chunks, is the exception.

Numbers are keyed on their decimal text, so an id read as an `int`, a `Decimal` or `'42'` masks alike under `hash`. `key` keeps its input's type, so keep one type per domain, or give it [`normalize: [integer]`](../reference/strategies.md#values-held-more-than-one-way-normalize).

# Cover every column


**Every column `sourceQuery` returns must appear in `columns`**, or the job fails before writing anything, naming them:

```
MaskingError: column(s) returned by sourceQuery but not in the masking policy: ssn.
Add each one -- `keep` if it needs no masking -- or set defaultStrategy
```

This stops the most common masking failure: a column added to production that nobody added to the policy, whose real values would flow into the copy. With `select *`, a new column stops the job instead. A column the policy names that the query doesn't return is an error too, since it is almost always a typo leaving the real column uncovered.

These errors are never retried. `bauta run --dry-run` finds them without loading anything, and [`bauta discover --update`](propose-a-policy.md#keeping-a-policy-up-to-date) finds them across every job and proposes the fix.

`defaultStrategy` turns the check off for the columns it covers; only `'null'` and `constant` keep it safe, since they discard whatever a new column holds. To make a database refuse unmasked rows altogether, set [`requireMasking`](../reference/connections.md#requiring-masking) on its alias.

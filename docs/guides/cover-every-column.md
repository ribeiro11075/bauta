# Cover every column


**Every column `sourceQuery` returns must appear in `columns`.** Otherwise the job fails before it writes anything, and the error names the columns:

```
MaskingError: column(s) returned by sourceQuery but not in the masking policy: ssn.
Add each one -- `keep` if it needs no masking -- or set defaultStrategy
```

This guards against the most common masking failure: someone adds a column to production, nobody updates the policy, and the column's real values flow into a non-production copy. With `select *`, a new column stops the job instead.

A column named in the policy that the query doesn't return is also an error, since it's almost always a typo that leaves the real column uncovered.

[`bauta discover --update`](propose-a-policy.md#keeping-a-policy-up-to-date) finds both across every job, proposes a policy for each new column, and with `--apply` writes the change into the jobs' files for review.

These errors are never retried. `bauta run --dry-run` finds them without loading anything. It runs each masked job's query, reads one row and discards it unexamined.

`defaultStrategy` turns the check off for unlisted columns. Only `'null'` or `constant` keep the safety property, since they discard whatever a new column holds.


To make a database refuse unmasked rows altogether, set [`requireMasking`](../reference/connections.md#requiring-masking) on its alias.

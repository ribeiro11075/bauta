# Security policy

Bauta exists to keep production data out of places it shouldn't be, so a flaw that lets an unmasked value through is treated as the most serious kind of bug there is.

## Reporting a vulnerability

Please report it privately, through [GitHub's private vulnerability reporting](https://github.com/ribeiro11075/bauta/security/advisories/new), rather than in a public issue. Include what you ran, what you expected, and what reached the target, the logs or run state instead. Never include real personal data; a synthetic value that reproduces it is enough.

You can expect an acknowledgement within three working days, and a fix or a plan within fourteen. A fix is released as soon as it is ready, noted first in the [changelog](CHANGELOG.md), with credit to the reporter unless they prefer otherwise.

## What counts

Anything that breaks a promise in the [security model](docs/security.md). For example:

- an unmasked source value reaching a target table, a log line, run state, history, a manifest or an error message
- a masking policy that `validate` or `run` accepts although it leaves a returned column uncovered
- masks that let someone without the key recover or match the values behind them, beyond what the security model says masking cannot hide
- a manifest that verifies after being altered
- a credential written anywhere it shouldn't be

Weaknesses the security model already lists under what masking does not hide are known, and are not vulnerabilities in themselves.

## Supported versions

Until 1.0, only the latest release gets fixes. Upgrade to it to receive one.

"""The masking strategies that ship with the package, `keep` through `redact`;
the lists the `fake*` ones pick from are in fakeData.

A policy names these by their NAME. Your own strategies are referenced as
module.path:ClassName and subclass bauta.masking.Strategy, as these do, so
nothing here is privileged. core holds what they share -- the keyed hash, the
Strategy base, the native masker -- and the plans that apply them.

Changing what any of these returns changes every mask already made with it:
mask-rs/vectors/reference.json records them, and the native masker must match.

One module a kind: `basic` (keep, null, constant, hash, email, digits,
number, shuffle), `places` (coordinate, ip), `dates` (dateShift), `fake`,
`keys` (key, fpe) and `documents` (redact, json), with what they share --
options' validators, helpers reading a value's shape -- in `common`.
"""
from __future__ import annotations

from typing import Dict, Type

from ..core import Strategy

from .common import luhnValid, structuredText
from .basic import ConstantStrategy, DigitsStrategy, EmailStrategy, HashStrategy, KeepStrategy, NullStrategy, NumberStrategy, ShuffleStrategy
from .places import CoordinateStrategy, IpStrategy
from .dates import DateShiftStrategy
from .fake import FAKE_NAME_DOMAIN, FakeCityStrategy, FakeCompanyStrategy, FakeFirstNameStrategy, FakeLastNameStrategy, FakeNameStrategy, FakeStreetAddressStrategy
from .keys import FPEStrategy, KeyStrategy
from .documents import JsonStrategy, RedactStrategy

STRATEGIES: Dict[str, Type[Strategy]] = {
    strategy.NAME: strategy for strategy in (
        KeepStrategy, NullStrategy, ConstantStrategy, HashStrategy, EmailStrategy, DigitsStrategy, NumberStrategy, DateShiftStrategy,
        FakeFirstNameStrategy, FakeLastNameStrategy, FakeNameStrategy, FakeCityStrategy, FakeCompanyStrategy, FakeStreetAddressStrategy,
        KeyStrategy, FPEStrategy, RedactStrategy, ShuffleStrategy, JsonStrategy, CoordinateStrategy, IpStrategy,
        )
    }

__all__ = ['STRATEGIES', 'FAKE_NAME_DOMAIN', 'luhnValid', 'structuredText'] + sorted(strategy.__name__ for strategy in STRATEGIES.values())

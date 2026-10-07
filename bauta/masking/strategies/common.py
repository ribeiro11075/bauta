"""What the built-in strategies share: their options' validators, and the helpers that read a value's shape.

Part of the strategies package; see its __init__.
"""
from __future__ import annotations

import decimal
import ipaddress
import json
import unicodedata
from typing import Any, Callable, List, Optional

from ..core import MAXIMUM_KEY_LENGTH, NORMALIZE_STEPS, MaskingError


def _asciiDigits(text: str) -> str:
    """`text` with every decimal digit, in any script, written as 0-9."""

    if text.isascii():
        return text

    return ''.join(str(unicodedata.decimal(character)) if unicodedata.decimal(character, None) is not None else character for character in text)


def _inScriptOf(original: str, masked: str) -> str:
    """`masked`, which has 0-9 where `original` has a decimal digit, with each
    of those digits written back in the script of the digit it replaced.
    """

    if original.isascii():
        return masked

    characters = []
    for before, after in zip(original, masked):
        value = unicodedata.decimal(before, None)
        characters.append(chr(ord(before) - value + int(after)) if value is not None else after)

    return ''.join(characters)


def _digitCount(value: int) -> int:
    """How many digits an integer has, without writing it out -- which Python
    refuses past 4,300 digits."""

    magnitude = abs(value)
    if magnitude >= 10 ** MAXIMUM_KEY_LENGTH:
        return MAXIMUM_KEY_LENGTH + 1

    return len(str(magnitude))


def _requireIdentifierLength(strategy: str, length: int) -> None:

    if length > MAXIMUM_KEY_LENGTH:
        raise MaskingError('the {} strategy masks identifiers of up to {} characters or digits, and this value is longer; '
                           'use hash, redact or null for long values'.format(strategy, MAXIMUM_KEY_LENGTH))


def _requireAsciiCharset(strategy: str, text: str, charset: str) -> None:
    """Refuses text holding letters or digits the charset can't mask, which
    would otherwise be copied unmasked while the manifest says masked.
    """

    if text.isascii():
        return

    if charset == 'alphanumeric':
        if any(not character.isascii() and character.isalnum() for character in text):
            raise MaskingError('the {} strategy masks only ASCII letters and digits, and this value has letters or digits in another script, '
                               'which it would copy unmasked; use hash, a fake strategy or null for such text'.format(strategy))
    elif any(not character.isascii() and character.isdigit() for character in text):
        raise MaskingError('the {} strategy masks only the digits 0-9, and this value has digits in another script, '
                           'which it would copy unmasked; use the digits strategy for such text'.format(strategy))




def _integerOption(minimum: int, maximum: Optional[int] = None) -> Callable[[Any], int]:

    def check(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError('must be a whole number')
        if value < minimum or (maximum is not None and value > maximum):
            raise ValueError('must be between {} and {}'.format(minimum, maximum) if maximum is not None else 'must be at least {}'.format(minimum))
        return value

    return check


def _numberOption(value: Any) -> decimal.Decimal:

    if isinstance(value, bool) or not isinstance(value, (int, float, decimal.Decimal, str)):
        raise ValueError('must be a number')
    try:
        number = decimal.Decimal(str(value))
    except decimal.InvalidOperation:
        raise ValueError('must be a number') from None
    if not number.is_finite():
        raise ValueError('must be finite')

    return number


def _textOption(value: Any) -> str:

    if not isinstance(value, str) or not value:
        raise ValueError('must be non-empty text')

    return value


def _booleanOption(value: Any) -> bool:

    if not isinstance(value, bool):
        raise ValueError('must be true or false')

    return value


def _choiceOption(*choices: str) -> Callable[[Any], str]:

    def check(value: Any) -> str:
        if value not in choices:
            raise ValueError('must be one of: {}'.format(', '.join(choices)))
        return str(value)

    return check


def _normalizeOption(*allowed: str) -> Callable[[Any], List[str]]:
    """`normalize`: a list of the steps a strategy takes, kept in the order
    they are applied (NORMALIZE_STEPS), whatever order they are written in.
    """

    def check(value: Any) -> List[str]:
        if not isinstance(value, list) or not value or any(step not in allowed for step in value):
            raise ValueError('must be a non-empty list of: {}'.format(', '.join(allowed)))
        return [step for step in NORMALIZE_STEPS if step in value]

    return check


def _typeName(value: Any) -> str:

    return type(value).__name__


_ADDRESS_TYPES = (ipaddress.IPv4Address, ipaddress.IPv6Address, ipaddress.IPv4Interface, ipaddress.IPv6Interface,
                  ipaddress.IPv4Network, ipaddress.IPv6Network)


def structuredText(value: Any) -> Optional[str]:
    """The text a strategy that masks text reads from a value a driver returns
    structured, or None for any other value: a JSON document -- PostgreSQL's
    json and jsonb, Oracle's JSON, DuckDB's STRUCT, LIST and MAP -- as JSON, in
    the key order it came in and spaced as PostgreSQL prints it; and an IP
    address or network -- PostgreSQL's inet and cidr -- as it is written.

    The masked text is what's loaded, which a JSON or an inet column takes as
    it would the literal. Only strategies that refused these values call this,
    so no mask made before changes.
    """

    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, _ADDRESS_TYPES):
        return str(value)

    return None


def luhnValid(digits: str) -> bool:
    """Whether `digits` end in a valid Luhn check digit, as a card number does.
    Discovery flags card numbers by it and `redact` removes them by it, so the
    two share this one definition and can't disagree about what a card is.
    """

    total = 0
    for position, character in enumerate(reversed(digits)):
        digit = int(character)
        if position % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit

    return total % 10 == 0

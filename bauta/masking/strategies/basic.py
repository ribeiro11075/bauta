"""The plain strategies: keep, null, constant, hash, email, digits, number and shuffle.

Part of the strategies package; see its __init__.
"""
from __future__ import annotations

import decimal
import math
import random
import unicodedata
from typing import Any, Dict, List, Sequence, Union

from ..core import MaskingError, Strategy, canonical
from .common import _asciiDigits, _booleanOption, _inScriptOf, _integerOption, _normalizeOption, _numberOption, _textOption, _typeName, structuredText


class KeepStrategy(Strategy):
    """Leave the column as it is. The explicit way to say a column was reviewed."""

    NAME = 'keep'
    KEYED = False
    # The only strategy that returns what it was given, so BoundMasking can
    # carry these columns through untouched. `null` and `constant` below ignore
    # their input too, but still have to write a value into every row, so they
    # are masked like any other column.
    PASSTHROUGH = True

    def maskColumn(self, values: Sequence[Any], chunkIndex: int) -> List[Any]:

        return list(values)


class NullStrategy(Strategy):
    """Replace every value with NULL."""

    NAME = 'null'
    KEYED = False

    def maskColumn(self, values: Sequence[Any], chunkIndex: int) -> List[Any]:

        return [None] * len(values)


class ConstantStrategy(Strategy):
    """Replace every value, NULLs included, with `value`."""

    NAME = 'constant'
    KEYED = False
    OPTIONS = {'value': lambda value: value}
    REQUIRED = ('value',)

    def maskColumn(self, values: Sequence[Any], chunkIndex: int) -> List[Any]:

        return [self.options['value']] * len(values)


class HashStrategy(Strategy):
    """An opaque hex token: `prefix` followed by `length` hex characters, 16
    (64 bits) unless set, and at least 12 (48 bits). A truncated hash is not
    one-to-one, so a column held unique can collide: at 12, one chance in six
    over ten million rows; at 16, one in 3,600 over a hundred million. `key`
    never collides.
    """

    NAME = 'hash'
    NATIVE = 'hash'
    CACHEABLE = True
    OPTIONS = {'length': _integerOption(12, 64), 'prefix': lambda value: '' if value is None else str(value),
               'normalize': _normalizeOption('strip', 'lower')}

    def mask(self, value: Any) -> Any:

        length = self.options.get('length', 16)

        return self.options.get('prefix', '') + self.keyedHash.digest(canonical(value)).hex()[:length]


class EmailStrategy(Strategy):
    """Still shaped like an email address: `u<hex>@example.test`, a reserved
    domain that can't deliver mail. Keyed on the lower-cased address.

    `length` hex characters, 12 (48 bits) unless set: a hash cut short, so
    two addresses can mask alike, one chance in six over ten million. A
    column held unique wants 24 or more, where a hundred million addresses
    collide with odds of about one in 10**13. At least 12: 8, allowed
    before, collided more often than not past 100,000 addresses.
    """

    NAME = 'email'
    NATIVE = 'email'
    CACHEABLE = True
    OPTIONS = {'length': _integerOption(12, 40), 'mailDomain': _textOption, 'keepDomain': _booleanOption}

    @classmethod
    def checkOptions(cls, options: Dict[str, Any]) -> None:

        if options.get('keepDomain') and 'mailDomain' in options:
            raise ValueError('strategy "email" takes mailDomain or keepDomain, not both')


    def mask(self, value: Any) -> Any:

        if not isinstance(value, str):
            text = structuredText(value)
            if text is None:
                raise MaskingError('the email strategy needs text, got {}'.format(_typeName(value)))
            value = text

        address = value.strip()
        local = 'u' + self.keyedHash.digest(address.lower().encode('utf-8')).hex()[:self.options.get('length', 12)]

        if self.options.get('keepDomain') and '@' in address:
            return local + '@' + address.rsplit('@', 1)[1]

        return local + '@' + self.options.get('mailDomain', 'example.test')


class DigitsStrategy(Strategy):
    """Replace every digit with a keyed digit, keeping everything else. Keyed
    on the digits alone, so formatting doesn't change the mask.
    """

    NAME = 'digits'
    NATIVE = 'digits'
    CACHEABLE = True
    OPTIONS = {'keepLeading': _integerOption(0), 'keepTrailing': _integerOption(0)}

    def _maskDigits(self, digits: str) -> str:

        keepLeading = self.options.get('keepLeading', 0)
        keepTrailing = self.options.get('keepTrailing', 0)

        # Otherwise the value would be returned as it is, while the manifest
        # said it was masked: '555-0100' under keepLeading 3, keepTrailing 4.
        if digits and len(digits) <= keepLeading + keepTrailing:
            raise MaskingError('the digits strategy would keep every digit of this value: keepLeading {} and keepTrailing {} cover all {} of '
                               'them, so it would be copied unmasked. Lower them, or mask this column another way'.format(
                                   keepLeading, keepTrailing, len(digits)))

        stream = self.keyedHash.expand(digits.encode('ascii'), 2 * len(digits) + 32)
        generated = [str(byte % 10) for byte in stream if byte < 250]

        masked = []
        for position, digit in enumerate(digits):
            if position < keepLeading or position >= len(digits) - keepTrailing:
                masked.append(digit)
            else:
                masked.append(generated[position % len(generated)])

        return ''.join(masked)


    def mask(self, value: Any) -> Any:

        if isinstance(value, bool):
            raise MaskingError('the digits strategy needs text or an integer, got bool')

        if isinstance(value, int):
            text = str(abs(value))
            masked = self._maskDigits(text)
            if len(masked) > 1 and masked[0] == '0':
                # A leading zero would shorten the integer; keep its digit count.
                masked = str(int(self.keyedHash.below(text.encode('ascii'), 9, b'lead')) + 1) + masked[1:]
            return int(masked) * (-1 if value < 0 else 1)

        if not isinstance(value, str):
            structured = structuredText(value)
            if structured is None:
                raise MaskingError('the digits strategy needs text or an integer, got {}'.format(_typeName(value)))
            value = structured

        if any(not character.isascii() and character.isdigit() and unicodedata.decimal(character, None) is None for character in value):
            raise MaskingError('the digits strategy masks decimal digits, and this value has other digit characters '
                               '(superscript or circled, say), which it would copy unmasked')

        # Digits in any script are keyed as 0-9, so a number masks the same way
        # whichever digits it was written in, and are written back in their own.
        normalized = _asciiDigits(value)
        digits = ''.join(character for character in normalized if '0' <= character <= '9')
        if not digits:
            return value

        replacement = iter(self._maskDigits(digits))
        masked = ''.join(next(replacement) if '0' <= character <= '9' else character for character in normalized)

        return _inScriptOf(value, masked)


# The significant digits `number` computes in. mask-rs/core/src/number.rs
# repeats the arithmetic at the same precision.
_PRECISION = 60


class NumberStrategy(Strategy):
    """A keyed number of the same type and precision, within `min`-`max` or
    within `variance` of the original. A value the variance would round back
    to itself moves one step instead; zero stays zero.

    `decimals` overrides the precision -- worth setting for floats, which is
    how oracledb returns a NUMBER with a scale.
    """

    NAME = 'number'
    NATIVE = 'number'
    OPTIONS = {'min': _numberOption, 'max': _numberOption, 'variance': _numberOption, 'decimals': _integerOption(0, 38)}

    def _nativeOptions(self) -> Dict[str, Any]:
        """The Decimal options as `str` spells them, which the native masker
        reads back digit for digit; `decimals` as it is.
        """

        return {name: value if name == 'decimals' else str(value) for name, value in self.options.items()}

    @classmethod
    def checkOptions(cls, options: Dict[str, Any]) -> None:

        hasMinimum, hasMaximum = 'min' in options, 'max' in options

        if hasMinimum != hasMaximum:
            raise ValueError('strategy "number" needs both min and max, or neither')
        if hasMinimum and options['min'] >= options['max']:
            raise ValueError('strategy "number" needs min below max')
        if hasMinimum and 'variance' in options:
            raise ValueError('strategy "number" takes a min/max range or a variance, not both')
        if 'variance' in options and not 0 < options['variance'] <= 1:
            raise ValueError('strategy "number" variance must be above 0 and at most 1')


    def _target(self, value: decimal.Decimal, message: bytes) -> decimal.Decimal:
        """The masked value before it's rounded to the column's precision."""

        fraction = decimal.Decimal(self.keyedHash.unit(message))

        if 'min' in self.options:
            return self.options['min'] + (self.options['max'] - self.options['min']) * fraction

        variance = self.options.get('variance', decimal.Decimal('0.1'))

        return value * (1 + (2 * fraction - 1) * variance)


    def _clamp(self, number: decimal.Decimal, step: decimal.Decimal) -> decimal.Decimal:
        """Rounding can step just outside a range whose bounds aren't on the grid."""

        if 'min' not in self.options:
            return number

        low = self.options['min'].quantize(step, rounding=decimal.ROUND_CEILING)
        high = self.options['max'].quantize(step, rounding=decimal.ROUND_FLOOR)

        return min(max(number, low), high)


    def _moved(self, value: decimal.Decimal, masked: decimal.Decimal, step: decimal.Decimal, message: bytes) -> decimal.Decimal:
        """`masked`, or one step from `value` if the variance rounded it back to
        `value`. The direction is keyed too.
        """

        if 'min' in self.options or masked != value or value == 0:
            return masked

        return value + step if self.keyedHash.unit(message, b'step') >= 0.5 else value - step


    def mask(self, value: Any) -> Any:

        if isinstance(value, bool) or not isinstance(value, (int, float, decimal.Decimal)):
            raise MaskingError('the number strategy needs a number, got {}'.format(_typeName(value)))

        try:
            return self._mask(value)
        except decimal.InvalidOperation:
            # quantize refuses a result longer than the context's precision:
            # an integer of more than 60 digits, or a float held to more
            # decimals than 60 digits reach at its size.
            raise MaskingError('the number strategy works to {} significant digits, and this {} needs more at the precision it '
                               'keeps'.format(_PRECISION, _typeName(value))) from None


    def _mask(self, value: Union[int, float, decimal.Decimal]) -> Any:

        message = canonical(value)

        with decimal.localcontext() as context:
            context.prec = _PRECISION

            if isinstance(value, int):
                step = decimal.Decimal(1)
                masked = self._clamp(self._target(decimal.Decimal(value), message).quantize(step, rounding=decimal.ROUND_HALF_EVEN), step)
                return int(self._moved(decimal.Decimal(value), masked, step, message))

            if isinstance(value, float):
                if not math.isfinite(value):
                    return value
                target = self._target(decimal.Decimal(repr(value)), message)
                if 'decimals' in self.options:
                    step = decimal.Decimal(1).scaleb(-self.options['decimals'])
                    masked = self._clamp(target.quantize(step, rounding=decimal.ROUND_HALF_EVEN), step)
                    return float(self._moved(decimal.Decimal(repr(value)), masked, step, message))
                return float(target)

            if not value.is_finite():
                return value

            exponent = -self.options['decimals'] if 'decimals' in self.options else min(0, int(value.as_tuple().exponent))
            step = decimal.Decimal(1).scaleb(exponent)

            masked = self._clamp(self._target(value, message).quantize(step, rounding=decimal.ROUND_HALF_EVEN), step)

            return self._moved(value, masked, step, message)


class ShuffleStrategy(Strategy):
    """Shuffle the column's values among the rows of each chunk. Not
    anonymization: every real value stays in the table, and a small chunk
    barely moves them.
    """

    NAME = 'shuffle'

    def maskColumn(self, values: Sequence[Any], chunkIndex: int) -> List[Any]:

        shuffled = list(values)
        seed = int.from_bytes(self.keyedHash.digest(str(chunkIndex).encode('ascii'), b'shuffle'), 'big')
        random.Random(seed).shuffle(shuffled)

        return shuffled

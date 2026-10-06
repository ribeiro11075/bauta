"""The masking strategies that ship with the package, `keep` through `redact`;
the lists the `fake*` ones pick from are in fakeData.

A policy names these by their NAME. Your own strategies are referenced as
module.path:ClassName and subclass bauta.masking.Strategy, as these do, so
nothing here is privileged. core holds what they share -- the keyed hash, the
Strategy base, the native masker -- and the plans that apply them.

Changing what any of these returns changes every mask already made with it:
mask-rs/vectors/reference.json records them, and the native masker must match.
"""
from __future__ import annotations

import datetime
import decimal
import ipaddress
import json
import math
import random
import re
import unicodedata
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Type, Union

from .fakeData import (COMPANY_WORDS, DEFAULT_LOCALE, FEMALE_NAMES, LARGE_DEFAULT_LOCALE, LARGE_LOCALES, LOCALES, MALE_NAMES, LargeLocale,
                       Locale)
from .core import MASK_CACHE_SIZE, MAXIMUM_KEY_LENGTH, NORMALIZE_STEPS, KeyedHash, MaskingError, Strategy, canonical


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
    """An opaque hex token: `prefix` followed by `length` hex characters. At
    least 12 (48 bits), so unique columns stay unique into the millions.
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
    """

    NAME = 'email'
    NATIVE = 'email'
    CACHEABLE = True
    OPTIONS = {'length': _integerOption(8, 40), 'mailDomain': _textOption, 'keepDomain': _booleanOption}

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


# Metres in a degree of latitude, and of longitude at the equator; a degree of
# longitude spans this times the cosine of its latitude.
_METRES_PER_DEGREE = 111320.0


class CoordinateStrategy(Strategy):
    """A latitude or a longitude moved along its axis by a keyed distance
    between half of `meters` and all of it, in a keyed direction, keyed on the
    value: every point is moved by a distance on the ground, wherever it is,
    where `number`'s variance moves a point near the equator or the prime
    meridian by next to nothing.

    A degree of longitude spans fewer metres away from the equator, so a
    longitude is moved by `meters` there unless `latitudeColumn` names the
    row's latitude, which converts the distance at that latitude. A latitude
    that would pass a pole moves the other way; a longitude wraps at 180.
    Floats come back as floats, and a Decimal at its own scale, moved at least
    one step.
    """

    NAME = 'coordinate'
    OPTIONS = {'axis': _choiceOption('latitude', 'longitude'), 'meters': _integerOption(1, 1000000), 'latitudeColumn': _textOption}
    REQUIRED = ('axis',)
    CACHEABLE = True
    CONTEXT_OPTION = 'latitudeColumn'

    @classmethod
    def checkOptions(cls, options: Dict[str, Any]) -> None:

        if 'latitudeColumn' in options and options['axis'] != 'longitude':
            raise ValueError('strategy "coordinate" takes latitudeColumn for a longitude only')


    def _degrees(self, message: bytes, latitude: Any) -> float:
        """The keyed move, in degrees along the axis, signed."""

        meters = self.options.get('meters', 1000) * (0.5 + 0.5 * self.keyedHash.unit(message, b'distance'))
        direction = 1 if self.keyedHash.unit(message, b'direction') < 0.5 else -1
        perDegree = _METRES_PER_DEGREE
        if latitude is not None and self.options['axis'] == 'longitude':
            # Near a pole a degree of longitude spans almost nothing; a
            # floor keeps the move from becoming the whole circle.
            perDegree *= max(0.01, math.cos(math.radians(float(latitude))))

        return direction * meters / perDegree


    def _moved(self, value: float, degrees: float) -> float:

        moved = value + degrees
        if self.options['axis'] == 'latitude':
            if abs(value) <= 90 < abs(moved):
                moved = value - degrees
            return moved

        return (moved + 180) % 360 - 180 if -180 <= value <= 180 else moved


    def _move(self, value: Any, latitude: Any) -> Any:

        if isinstance(value, bool) or not isinstance(value, (int, float, decimal.Decimal)):
            raise MaskingError('the coordinate strategy needs a number, got {}'.format(_typeName(value)))
        if isinstance(latitude, bool) or not isinstance(latitude, (int, float, decimal.Decimal)) or not math.isfinite(latitude):
            latitude = None

        if isinstance(value, decimal.Decimal):
            if not value.is_finite():
                return value
            degrees = self._degrees(canonical(value), latitude)
            step = decimal.Decimal(1).scaleb(min(0, int(value.as_tuple().exponent)))
            moved = decimal.Decimal(repr(self._moved(float(value), degrees))).quantize(step, rounding=decimal.ROUND_HALF_EVEN)
            if moved == value:
                moved = value + step if degrees > 0 else value - step
            return moved

        if not math.isfinite(value):
            return value

        return self._moved(float(value), self._degrees(canonical(value), latitude))


    def mask(self, value: Any) -> Any:

        return self._move(value, None)


    def maskColumnWith(self, values: Sequence[Any], context: Sequence[Any], chunkIndex: int) -> List[Any]:

        return [None if value is None else self._move(value, latitude) for value, latitude in zip(values, context)]


_CALENDAR_ENDS = frozenset({datetime.date.min.toordinal(), datetime.date.max.toordinal()})
_CALENDAR_INSIDE = range(datetime.date.min.toordinal() + 1, datetime.date.max.toordinal())


class DateShiftStrategy(Strategy):
    """Move a date or timestamp by a keyed number of whole days, never zero.
    The shift is keyed on the day, so every value on a day moves alike, whether
    it is a date, a timestamp or ISO 8601 text. Text is written back in the
    same shape.

    Keyed on the day, two days move by unrelated amounts, so dates in a row --
    a start and an end -- can change order. `shiftBy` names a column of the
    row instead, a person's id say: every date of that person moves by the
    same amount, in every column and table that shifts by a column of that
    name, so their order and the time between them are kept. Its domain is
    that column's name unless the policy names one. A row whose `shiftBy`
    value is NULL is shifted by the day.

    0001-01-01 and 9999-12-31 mean "no date" or "forever", so they are kept,
    and a shift that would leave the calendar or land on them goes the other way.
    """

    NAME = 'dateShift'
    OPTIONS = {'maxDays': _integerOption(1, 36500), 'shiftBy': _textOption}
    CONTEXT_OPTION = 'shiftBy'

    def __init__(self, keyedHash: KeyedHash, options: Mapping[str, Any]) -> None:
        super().__init__(keyedHash, options)
        # Shifts already derived, by the day's ordinal. Strategy's own cache
        # can't hold these: a date and a timestamp are not among the types it
        # remembers (equal values can differ in time zone), yet the shift
        # itself depends on nothing but the day.
        self._offsets: Dict[int, datetime.timedelta] = {}
        # And by the shiftBy value, for the same reason.
        self._subjectOffsets: Dict[Tuple[type, Any], datetime.timedelta] = {}


    @classmethod
    def defaultDomain(cls, column: str, options: Mapping[str, Any]) -> str:
        """The shiftBy column's name, so the dates of one person agree across
        columns whatever each is called.
        """

        return options['shiftBy'].lower() if options.get('shiftBy') else column.lower()


    def _days(self, message: bytes, purpose: bytes) -> datetime.timedelta:
        """A keyed shift in +-maxDays, never zero."""

        maxDays = self.options.get('maxDays', 30)
        days = self.keyedHash.below(message, 2 * maxDays, purpose) - maxDays

        return datetime.timedelta(days=days + 1 if days >= 0 else days)


    def _subjectOffset(self, subject: Any) -> datetime.timedelta:
        """The shift for every date of one shiftBy value."""

        cacheKey = (type(subject), subject)
        try:
            return self._subjectOffsets[cacheKey]
        except (KeyError, TypeError):
            pass

        offset = self._days(canonical(subject), b'shiftBy')
        try:
            if len(self._subjectOffsets) >= MASK_CACHE_SIZE:
                self._subjectOffsets.clear()
            self._subjectOffsets[cacheKey] = offset
        except TypeError:
            # A value that can't be a dict key, a JSON document say: derived each time.
            pass

        return offset


    def _offset(self, value: Any) -> datetime.timedelta:
        """Keyed on the day alone, never the time of day, so everything that
        happened on one day moves to one day, whether a date, a timestamp or
        text.

        Derived once per day, since a date column holds a few thousand
        distinct days and as many rows as the table. The ordinal stands in for
        the day's ISO text, which the hash is keyed on; the two agree one for
        one.
        """

        day = value.date() if isinstance(value, datetime.datetime) else value
        ordinal = day.toordinal()

        offset = self._offsets.get(ordinal)
        if offset is not None:
            return offset

        offset = self._days(canonical(day), b'')

        if len(self._offsets) >= MASK_CACHE_SIZE:
            # Emptied, not evicted, as Strategy's own cache is.
            self._offsets.clear()
        self._offsets[ordinal] = offset

        return offset


    def _shift(self, value: Any, subject: Any = None) -> Any:
        """`value`, a date or datetime, moved by its offset: its day's, or its
        shiftBy value's.
        """

        ordinal = value.toordinal()
        if ordinal in _CALENDAR_ENDS:
            return value

        offset = self._offset(value) if subject is None else self._subjectOffset(subject)
        if ordinal + offset.days not in _CALENDAR_INSIDE:
            offset = -offset

        return value + offset


    def maskColumnWith(self, values: Sequence[Any], context: Sequence[Any], chunkIndex: int) -> List[Any]:

        return [None if value is None else self._maskShifted(value, subject) for value, subject in zip(values, context)]


    def mask(self, value: Any) -> Any:

        return self._maskShifted(value, None)


    def _maskShifted(self, value: Any, subject: Any) -> Any:

        if isinstance(value, datetime.date):
            return self._shift(value, subject)

        if not isinstance(value, str):
            raise MaskingError('the dateShift strategy needs a date, a timestamp or ISO 8601 text, got {}'.format(_typeName(value)))

        text = value.strip()

        try:
            if len(text) == 10:
                parsed: datetime.date = datetime.date.fromisoformat(text)
                return self._shift(parsed, subject).isoformat()

            parsedTimestamp = datetime.datetime.fromisoformat(text)
        except ValueError:
            raise MaskingError('the dateShift strategy could not read a text value as an ISO 8601 date') from None

        shifted = self._shift(parsedTimestamp, subject)
        separator = 'T' if 'T' in text else ' '
        timespec = 'microseconds' if '.' in text else ('seconds' if text.count(':') >= 2 else 'minutes')

        return shifted.isoformat(sep=separator, timespec=timespec)


def _listsOption(value: Any) -> int:

    if isinstance(value, bool) or value not in (1, 2):
        raise ValueError('must be 1 or 2')

    return int(value)


# The domain the name strategies mask in under `lists: 2` when the policy names
# none, so a first name masks alike in a first_name column, a full_name column
# and any table, as a key masks alike in every column of its domain.
FAKE_NAME_DOMAIN = 'fake name'

# Lower-case words that begin a surname: `de Jong`, `van der Berg`, `da Silva`.
_SURNAME_PARTICLES = frozenset({'da', 'das', 'de', 'del', 'della', 'der', 'di', 'do', 'dos', 'du', 'la', 'le', 'ten', 'ter', 'van', 'von',
                                'den', 'het', "'t", 'y'})


_FIRST_NAMES: Dict[int, Tuple[str, ...]] = {}


def _firstNames(large: LargeLocale) -> Tuple[str, ...]:
    """A locale's first names of both genders, joined once."""

    joined = _FIRST_NAMES.get(id(large))
    if joined is None:
        joined = _FIRST_NAMES[id(large)] = large.femaleNames + large.maleNames

    return joined


class _FakeStrategy(Strategy):
    """A realistic-looking replacement, chosen from bundled lists by the hash.
    Not unique.

    `lists: 2` picks from fakeData's longer lists, which nothing masked
    before used, so the masks made with the default `lists: 1` are unchanged.
    """

    CACHEABLE = True
    OPTIONS = {'maxLength': _integerOption(1), 'locale': _choiceOption(*sorted(LOCALES)), 'lists': _listsOption}

    @property
    def locale(self) -> Locale:

        return LOCALES[self.options['locale']] if 'locale' in self.options else DEFAULT_LOCALE


    @property
    def large(self) -> Optional[LargeLocale]:
        """The `lists: 2` lists, or None under `lists: 1`."""

        if self.options.get('lists', 1) != 2:
            return None

        return LARGE_LOCALES[self.options['locale']] if 'locale' in self.options else LARGE_DEFAULT_LOCALE


    def _buildNative(self) -> Any:
        """None for `lists: 2`, which only Python implements."""

        return None if self.options.get('lists', 1) == 2 else super()._buildNative()


    def _nativeOptions(self) -> Dict[str, Any]:
        """The lists themselves, so they are defined once, here: the native
        masker picks from what it's handed.
        """

        locale = self.locale

        return {'maxLength': self.options.get('maxLength'), 'firstNames': list(locale.firstNames), 'lastNames': list(locale.lastNames),
                'cities': list(locale.cities), 'streets': list(locale.streets), 'streetKinds': list(locale.streetKinds),
                'address': locale.address, 'companySuffixes': list(locale.companySuffixes), 'companyWords': list(COMPANY_WORDS)}

    def _pick(self, choices: Sequence[str], message: bytes, purpose: bytes) -> str:

        return choices[self.keyedHash.below(message, len(choices), purpose)]


    def generate(self, message: bytes) -> str:

        raise NotImplementedError


    def mask(self, value: Any) -> Any:

        generated = self.generateLarge(value) if self.large is not None else self.generate(canonical(value))
        maxLength = self.options.get('maxLength')

        return generated[:maxLength] if maxLength else generated


    def generateLarge(self, value: Any) -> str:
        """The replacement under `lists: 2`; the same as `lists: 1` gives,
        from the longer lists, unless a strategy says otherwise.
        """

        return self.generate(canonical(value))


class _FakePersonNameStrategy(_FakeStrategy):
    """What the three name strategies share under `lists: 2`: names keyed
    as a person writes them, whatever their case or spacing, in one domain,
    so a full name's parts mask as the first and last names on their own
    columns do; written back in the original's case; and with `matchGender`,
    a first name replaced by one of the same gender where the lists know it.
    """

    OPTIONS = dict(_FakeStrategy.OPTIONS, matchGender=_booleanOption)

    @classmethod
    def checkOptions(cls, options: Dict[str, Any]) -> None:

        if options.get('matchGender') and options.get('lists', 1) != 2:
            raise ValueError('strategy "{}" takes matchGender with lists: 2 only'.format(cls.NAME))


    @classmethod
    def defaultDomain(cls, column: str, options: Mapping[str, Any]) -> str:

        return FAKE_NAME_DOMAIN if options.get('lists', 1) == 2 else column.lower()


    @staticmethod
    def _key(name: str) -> bytes:

        return ' '.join(name.split()).casefold().encode('utf-8')


    @staticmethod
    def _inCaseOf(original: str, replacement: str) -> str:

        if original.isupper():
            return replacement.upper()
        if original.islower():
            return replacement.lower()

        return replacement


    def _firstName(self, name: str) -> str:

        large = self.large
        assert large is not None
        folded = ' '.join(name.split()).casefold()
        if self.options.get('matchGender') and folded in FEMALE_NAMES:
            choices = large.femaleNames
        elif self.options.get('matchGender') and folded in MALE_NAMES:
            choices = large.maleNames
        else:
            choices = _firstNames(large)

        return self._inCaseOf(name, self._pick(choices, self._key(name), b'first'))


    def _lastName(self, name: str) -> str:

        large = self.large
        assert large is not None

        return self._inCaseOf(name, self._pick(large.lastNames, self._key(name), b'last'))


class FakeFirstNameStrategy(_FakePersonNameStrategy):

    NAME = 'fakeFirstName'
    NATIVE = 'fakeFirstName'

    def generate(self, message: bytes) -> str:

        return self._pick(self.locale.firstNames, message, b'first')


    def generateLarge(self, value: Any) -> str:

        return self._firstName(value if isinstance(value, str) else canonical(value).decode('utf-8', 'replace'))


class FakeLastNameStrategy(_FakePersonNameStrategy):

    NAME = 'fakeLastName'
    NATIVE = 'fakeLastName'

    def generate(self, message: bytes) -> str:

        return self._pick(self.locale.lastNames, message, b'last')


    def generateLarge(self, value: Any) -> str:

        return self._lastName(value if isinstance(value, str) else canonical(value).decode('utf-8', 'replace'))


class FakeNameStrategy(_FakePersonNameStrategy):

    NAME = 'fakeName'
    NATIVE = 'fakeName'

    def generate(self, message: bytes) -> str:

        return '{} {}'.format(self._pick(self.locale.firstNames, message, b'first'), self._pick(self.locale.lastNames, message, b'last'))


    def generateLarge(self, value: Any) -> str:
        """Each part of a full name as fakeFirstName and fakeLastName mask it:
        the given names, then the surname with any particle before it
        (`Jan de Jong`), or `Surname, Given Names`. A single word is a given name.
        """

        text = value if isinstance(value, str) else canonical(value).decode('utf-8', 'replace')

        if ',' in text:
            surname, _, given = text.partition(',')
            givenNames = given.split()
            maskedGiven = ' '.join(self._firstName(name) for name in givenNames)
            return '{}, {}'.format(self._lastName(surname.strip()), maskedGiven) if givenNames else self._lastName(surname.strip())

        words = text.split()
        if not words:
            return text
        if len(words) == 1:
            return self._firstName(words[0])

        start = len(words) - 1
        while start > 1 and words[start - 1].casefold() in _SURNAME_PARTICLES:
            start -= 1

        return ' '.join([self._firstName(name) for name in words[:start]] + [self._lastName(' '.join(words[start:]))])


class FakeCityStrategy(_FakeStrategy):

    NAME = 'fakeCity'
    NATIVE = 'fakeCity'

    def generate(self, message: bytes) -> str:

        return self._pick(self.large.cities if self.large is not None else self.locale.cities, message, b'city')


class FakeCompanyStrategy(_FakeStrategy):

    NAME = 'fakeCompany'
    NATIVE = 'fakeCompany'

    def generate(self, message: bytes) -> str:

        return '{} {}'.format(self._pick(COMPANY_WORDS, message, b'company'), self._pick(self.locale.companySuffixes, message, b'suffix'))


class FakeStreetAddressStrategy(_FakeStrategy):

    NAME = 'fakeStreetAddress'
    NATIVE = 'fakeStreetAddress'

    def generate(self, message: bytes) -> str:

        number = self.keyedHash.below(message, 9999, b'number') + 1
        locale = self.locale

        return locale.address.format(number=number, street=self._pick(locale.streets, message, b'street'),
                                     kind=self._pick(locale.streetKinds, message, b'suffix'))


_ALPHANUMERIC_CLASSES = ('0123456789', 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')
# Per case, so a hex value keeps each character's case and two spellings of
# one value never share a mask. FF1 needs one alphabet for every position,
# so `fpe` uses both cases together instead (_FPE_ALPHABETS).
_HEX_CLASSES = ('0123456789', 'abcdef', 'ABCDEF')


class KeyStrategy(Strategy):
    """A one-to-one mapping, safe for primary and foreign keys, that keeps the
    input's shape: an integer's sign and digit count, text's length and every
    character outside `charset`.

    One-to-one because it is a permutation within each shape, and different
    shapes can't meet. That is why `charset` is fixed per column rather than
    detected per value, which would let shapes overlap.
    """

    NAME = 'key'
    NATIVE = 'key'
    CACHEABLE = True
    OPTIONS = {'charset': _choiceOption('alphanumeric', 'digits', 'hex'), 'normalize': _normalizeOption(*NORMALIZE_STEPS)}

    def _maskInteger(self, value: int) -> int:

        _requireIdentifierLength('key', _digitCount(value))
        magnitude = abs(value)
        digitCount = len(str(magnitude))
        # Zero belongs to the non-negative one-digit range only; letting a
        # negative digit map to it would make -0 collide with 0's own image.
        low = 10 ** (digitCount - 1) if digitCount > 1 or value < 0 else 0
        size = 10 ** digitCount - low
        masked = low + self.keyedHash.permute(size, magnitude - low, b'negative' if value < 0 else b'integer')

        return -masked if value < 0 else masked


    def _alphabets(self, text: str, charset: str) -> List[Optional[str]]:
        """Per position, the alphabet it's masked within, or None to keep it."""

        alphabets: List[Optional[str]] = []

        for character in text:
            if charset == 'hex':
                alphabets.append(next((alphabet for alphabet in _HEX_CLASSES if character in alphabet), None))
            elif charset == 'digits':
                alphabets.append(_ALPHANUMERIC_CLASSES[0] if '0' <= character <= '9' else None)
            else:
                alphabets.append(next((alphabet for alphabet in _ALPHANUMERIC_CLASSES if character in alphabet), None))

        return alphabets


    def _maskText(self, text: str, charset: str) -> str:

        _requireIdentifierLength('key', len(text))
        _requireAsciiCharset('key', text, charset)
        alphabets = self._alphabets(text, charset)

        size = 1
        number = 0
        for character, alphabet in zip(text, alphabets):
            if alphabet is not None:
                size *= len(alphabet)
                number = number * len(alphabet) + alphabet.index(character)

        if size == 1:
            return text

        shape = ''.join(character if alphabet is None else str(len(alphabet)) for character, alphabet in zip(text, alphabets))
        masked = self.keyedHash.permute(size, number, b'text|' + charset.encode('ascii') + b'|' + shape.encode('utf-8'))

        characters = []
        for character, alphabet in reversed(list(zip(text, alphabets))):
            if alphabet is None:
                characters.append(character)
            else:
                masked, index = divmod(masked, len(alphabet))
                characters.append(alphabet[index])

        return ''.join(reversed(characters))


    def mask(self, value: Any) -> Any:

        if isinstance(value, bool):
            raise MaskingError('the key strategy cannot mask a bool')

        if isinstance(value, int):
            return self._maskInteger(value)

        if isinstance(value, decimal.Decimal):
            if not (value.is_finite() and value == value.to_integral_value()):
                raise MaskingError('the key strategy needs a whole number, got a fractional Decimal')
            return decimal.Decimal(self._maskInteger(int(value)))

        if isinstance(value, uuid.UUID):
            return uuid.UUID(self._maskText(str(value), 'hex'))

        text = value if isinstance(value, str) else structuredText(value)
        if text is not None:
            return self._maskText(text, self.options.get('charset', 'alphanumeric'))

        raise MaskingError('the key strategy needs an integer or text, got {}'.format(_typeName(value)))


_FPE_ALPHABETS = {
    'digits': '0123456789',
    'hex': '0123456789abcdefABCDEF',
    'alphanumeric': '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ',
    }


class FPEStrategy(Strategy):
    """NIST FF1 format-preserving encryption (SP 800-38G Rev. 1), shaped like
    `key`. The domain goes into FF1's tweak.

    FF1 needs at least a million possible values, so shorter values fall back
    to `key`'s permutation -- no collision, since neither changes a length --
    or, with `strict`, fail. Needs the `fpe` extra.
    """

    NAME = 'fpe'
    NATIVE = 'fpe'
    CACHEABLE = True
    OPTIONS = {'charset': _choiceOption(*_FPE_ALPHABETS), 'strict': _booleanOption, 'normalize': _normalizeOption(*NORMALIZE_STEPS)}

    def __init__(self, keyedHash: KeyedHash, options: Mapping[str, Any]) -> None:
        super().__init__(keyedHash, options)
        self._ciphers: Dict[int, Any] = {}
        self._short = KeyStrategy(keyedHash, {})


    def _cipher(self, radix: int) -> Any:

        from .fpe import FF1

        if radix not in self._ciphers:
            self._ciphers[radix] = FF1(self.keyedHash.digest(b'', b'ff1 key'), radix)

        return self._ciphers[radix]


    def _tooShort(self, cipher: Any, what: str) -> None:
        """Raises under `strict`; otherwise the caller falls back to `key`."""

        if self.options.get('strict'):
            raise MaskingError('the fpe strategy is strict, and FF1 needs at least {} {} in a value; this one has fewer'.format(
                cipher.minimumLength, what))


    def _maskInteger(self, value: int) -> int:

        _requireIdentifierLength('fpe', _digitCount(value))
        digits = [int(character) for character in str(abs(value))]
        cipher = self._cipher(10)

        if len(digits) < cipher.minimumLength:
            self._tooShort(cipher, 'digits')
            return self._short._maskInteger(value)

        # Cycle-walk past results with a leading zero, which would shorten the
        # number. The input has none, so the walk comes back to such a value.
        tweak = b'negative' if value < 0 else b'integer'
        masked = cipher.encrypt(digits, tweak)
        while masked[0] == 0:
            masked = cipher.encrypt(masked, tweak)

        number = int(''.join(map(str, masked)))

        return -number if value < 0 else number


    def _maskText(self, text: str, charset: str) -> str:

        _requireIdentifierLength('fpe', len(text))
        _requireAsciiCharset('fpe', text, charset)
        alphabet = _FPE_ALPHABETS[charset]
        positions = [index for index, character in enumerate(text) if character in alphabet]
        cipher = self._cipher(len(alphabet))

        if len(positions) < cipher.minimumLength:
            self._tooShort(cipher, '{} characters'.format(charset))
            return self._short._maskText(text, charset)

        masked = set(positions)
        shape = ''.join('\x00' if index in masked else character for index, character in enumerate(text))
        numerals = cipher.encrypt([alphabet.index(text[index]) for index in positions], ('text|' + charset + '|' + shape).encode('utf-8'))

        characters = list(text)
        for index, numeral in zip(positions, numerals):
            characters[index] = alphabet[numeral]

        return ''.join(characters)


    def mask(self, value: Any) -> Any:

        if isinstance(value, bool):
            raise MaskingError('the fpe strategy cannot mask a bool')

        if isinstance(value, int):
            return self._maskInteger(value)

        if isinstance(value, decimal.Decimal):
            if not (value.is_finite() and value == value.to_integral_value()):
                raise MaskingError('the fpe strategy needs a whole number, got a fractional Decimal')
            return decimal.Decimal(self._maskInteger(int(value)))

        if isinstance(value, uuid.UUID):
            return uuid.UUID(self._maskText(str(value), 'hex'))

        text = value if isinstance(value, str) else structuredText(value)
        if text is not None:
            return self._maskText(text, self.options.get('charset', 'alphanumeric'))

        raise MaskingError('the fpe strategy needs an integer or text, got {}'.format(_typeName(value)))


def _listOption(*choices: str) -> Callable[[Any], List[str]]:

    def check(value: Any) -> List[str]:
        if not isinstance(value, list) or not value or any(item not in choices for item in value):
            raise ValueError('must be a non-empty list of: {}'.format(', '.join(choices)))
        return list(value)

    return check


def _patternsOption(value: Any) -> List[str]:

    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError('must be a non-empty list of regular expressions')
    for pattern in value:
        try:
            re.compile(pattern)
        except re.error as error:
            raise ValueError('{!r} is not a valid regular expression: {}'.format(pattern, error)) from None

    return list(value)


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


def _validIban(text: str) -> bool:

    compact = text.replace(' ', '')
    rearranged = compact[4:] + compact[:4]

    return 15 <= len(compact) <= 34 and int(''.join(str(int(character, 36)) for character in rearranged)) % 97 == 1


def _validIpv4(text: str) -> bool:

    return all(int(octet) <= 255 for octet in text.split('.'))


_DATE_LIKE = re.compile(r'^(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})$')


def _phoneLike(text: str) -> bool:
    """7 to 15 digits -- E.164's limit -- and not a date, whose separators
    otherwise make it look like a number.
    """

    return 7 <= sum(character.isdigit() for character in text) <= 15 and not _DATE_LIKE.match(text.strip())


# (kind, pattern, check). Earlier kinds win where matches overlap, so the
# specific ones -- validated by a checksum or a fixed shape -- come before the
# loose phone pattern, which would otherwise swallow a card number.
_DETECTORS: Tuple[Tuple[str, 're.Pattern[str]', Callable[[str], bool]], ...] = (
    ('email', re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}'), lambda text: True),
    ('iban', re.compile(r'\b[A-Z]{2}\d{2}(?: ?[A-Z\d]){11,30}\b'), _validIban),
    ('card', re.compile(r'(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])'), lambda text: luhnValid(re.sub(r'\D', '', text))),
    ('ssn', re.compile(r'(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])'), lambda text: True),
    ('ip', re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\.?\d)'), _validIpv4),
    ('phone', re.compile(r'(?<![\w+])\+?\(?\d[\d ().-]{5,}\d(?!\w)'), _phoneLike),
    )

_DETECTOR_KINDS = tuple(kind for kind, _, _ in _DETECTORS)


class RedactStrategy(Strategy):
    """Finds recognisable identifiers inside free text and replaces only those,
    with a label or a keyed value of the same shape. It cannot find names.
    """

    NAME = 'redact'
    OPTIONS = {'replacement': _choiceOption('label', 'mask'), 'detect': _listOption(*_DETECTOR_KINDS), 'patterns': _patternsOption}

    def __init__(self, keyedHash: KeyedHash, options: Mapping[str, Any]) -> None:
        super().__init__(keyedHash, options)
        kinds = set(self.options.get('detect', _DETECTOR_KINDS))
        self._detectors = [detector for detector in _DETECTORS if detector[0] in kinds]
        self._detectors += [('pattern', re.compile(pattern), lambda text: True) for pattern in self.options.get('patterns', [])]
        self._email = EmailStrategy(keyedHash, {})
        self._digits = DigitsStrategy(keyedHash, {})
        self._card = DigitsStrategy(keyedHash, {'keepTrailing': 4})
        self._key = KeyStrategy(keyedHash, {})


    def _spans(self, text: str) -> List[Tuple[int, int, str]]:
        """Non-overlapping (start, end, kind), earlier detectors winning."""

        taken: List[Tuple[int, int, str]] = []
        # One byte per character, set once a span claims it: checking a match
        # against it costs the match's length, not the number of spans so far.
        occupied = bytearray(len(text))
        for kind, pattern, check in self._detectors:
            for match in pattern.finditer(text):
                start, end = match.span()
                if start == end or occupied.find(1, start, end) != -1 or not check(match.group(0)):
                    continue
                occupied[start:end] = b'\x01' * (end - start)
                taken.append((start, end, kind))

        return sorted(taken)


    def _replace(self, kind: str, found: str) -> str:

        if self.options.get('replacement', 'label') == 'label':
            return '[REDACTED]' if kind == 'pattern' else '[{}]'.format(kind.upper())

        if kind == 'email':
            return str(self._email.mask(found))
        if kind == 'card':
            return str(self._card.mask(found))
        if kind in ('phone', 'ssn'):
            return str(self._digits.mask(found))
        if kind == 'iban':
            # The detectors accept digits in any script; the key strategy
            # doesn't, so they're masked as 0-9 and written back in their own.
            rest = found[2:]
            return found[:2] + _inScriptOf(rest, str(self._key.mask(_asciiDigits(rest))))
        if kind == 'ip':
            octets = self.keyedHash.digest(_asciiDigits(found).encode('ascii'), b'ip')
            return '10.{}.{}.{}'.format(octets[0], octets[1], octets[2])

        return 'redacted-' + self.keyedHash.digest(found.encode('utf-8'), b'pattern').hex()[:12]


    def maskNumber(self, value: int) -> Any:
        """A whole number that, written out, is an identifier redact finds --
        a phone number or a card number held as a JSON number -- masked as its
        digits are and kept a number, or its label; any other, as it is.
        """

        spans = self._spans(str(abs(value)))
        if not spans:
            return value

        kind = spans[0][2]
        if self.options.get('replacement', 'label') == 'label':
            return '[REDACTED]' if kind == 'pattern' else '[{}]'.format(kind.upper())

        return (self._card if kind == 'card' else self._digits).mask(value)


    def mask(self, value: Any) -> Any:

        if not isinstance(value, str):
            text = structuredText(value)
            if text is None:
                raise MaskingError('the redact strategy needs text, got {}'.format(_typeName(value)))
            value = text

        pieces = []
        position = 0
        for start, end, kind in self._spans(value):
            pieces.append(value[position:start])
            pieces.append(self._replace(kind, value[start:end]))
            position = end
        pieces.append(value[position:])

        return ''.join(pieces)


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


# A path into a JSON document: keys joined with dots, `[]` for every element
# of an array: `contact.alt_email`, `orders[].card`, `[].email`.
_JSON_PATH = re.compile(r'^(?:[^.\[\]]+|\[\])(?:\.[^.\[\]]+|\[\])*$')


def _jsonPolicy(value: Any) -> Dict[str, Any]:
    """A policy for part of a JSON document: any column policy but `shuffle`,
    which moves values between rows, and a single value has none to move to.
    """

    from .core import validateColumnPolicy

    from .core import resolveStrategy

    policy = validateColumnPolicy(value)
    if policy['strategy'] == 'shuffle':
        raise ValueError('shuffle moves values between rows, so it would leave a value inside a document where it is')
    contextOption = resolveStrategy(policy['strategy']).CONTEXT_OPTION
    if contextOption and contextOption in policy:
        raise ValueError('{} names a column of the row, which a value inside a document has none of'.format(contextOption))

    return policy


def _jsonFields(value: Any) -> Dict[str, Dict[str, Any]]:

    if not isinstance(value, Mapping) or not value:
        raise ValueError('must map paths in the document, such as contact.email or items[].card, to a policy each')

    fields = {}
    for path, policy in value.items():
        if not isinstance(path, str) or not _JSON_PATH.match(path):
            raise ValueError('{!r} is not a path: name keys with dots and every element of an array with [], as in items[].card'.format(path))
        try:
            fields[path] = _jsonPolicy(policy)
        except ValueError as error:
            raise ValueError('{}: {}'.format(path, error)) from None

    return fields


def _pathText(path: Tuple[str, ...]) -> str:

    return '.'.join(path).replace('.[]', '[]')


class JsonStrategy(Strategy):
    """Masks inside a JSON document: each path `fields` names with its own
    policy, and every other value with `otherwise` -- by default `redact`,
    which masks the identifiers it finds by their shape in text, and in whole
    numbers long enough to be a phone or card number, and leaves other numbers
    and booleans as they are. A policy named for a path holding an object or an
    array applies to it whole, so `null` drops it.

    An object's keys are data too where a document is keyed by, say, email
    address, so identifiers redact finds in them are masked, unless
    `otherwise` keeps values as they are.

    A field masks in the domain it names, or in its last key's, as a column
    masks in its name's: `contact.customer_id: {strategy: key, domain:
    customers}` matches the customers table's masked ids.

    The document comes back as it came: an object or array as one, and JSON
    text, as MySQL and DuckDB return it, as text.
    """

    NAME = 'json'
    OPTIONS = {'fields': _jsonFields, 'otherwise': _jsonPolicy}
    REQUIRED = ('fields',)

    def __init__(self, keyedHash: KeyedHash, options: Mapping[str, Any]) -> None:

        super().__init__(keyedHash, options)
        otherwise = self.options.get('otherwise', {'strategy': 'redact', 'replacement': 'mask'})
        self._otherwise = self._build(keyedHash, otherwise)
        self._fields: Optional[Dict[str, Strategy]] = None
        self._keys = None if self._otherwise.PASSTHROUGH else RedactStrategy(keyedHash, {'replacement': 'mask'})


    @staticmethod
    def _build(keyedHash: KeyedHash, policy: Mapping[str, Any]) -> Strategy:

        from .core import POLICY_FIELDS, resolveStrategy

        strategy = resolveStrategy(policy['strategy'])(keyedHash, {name: value for name, value in policy.items() if name not in POLICY_FIELDS})

        return strategy


    def bindKey(self, key: str) -> None:

        self._fields = {}
        for path, policy in self.options['fields'].items():
            domain = policy.get('domain') or next(part for part in reversed(path.replace('[]', '.').split('.')) if part).lower()
            self._fields[path] = self._build(KeyedHash(key, domain), policy)
            self._fields[path].bindKey(key)
        self._otherwise.bindKey(key)


    def _maskPart(self, strategy: Strategy, value: Any) -> Any:

        if isinstance(strategy, RedactStrategy) and not isinstance(value, str):
            return strategy.maskNumber(value) if isinstance(value, int) and not isinstance(value, bool) else value

        return strategy.maskColumn([value], 0)[0]


    def _maskKey(self, key: Any) -> Any:
        """An object's key, with the identifiers in it masked. Paths in
        `fields` are matched against the key as it came.
        """

        if self._keys is None or not isinstance(key, str):
            return key

        return self._keys.mask(key)


    def _walk(self, node: Any, path: Tuple[str, ...]) -> Any:

        assert self._fields is not None
        if path:
            strategy = self._fields.get(_pathText(path))
            if strategy is not None:
                return self._maskPart(strategy, node)
        if isinstance(node, dict):
            return {self._maskKey(key): self._walk(value, path + (str(key),)) for key, value in node.items()}
        if isinstance(node, list):
            return [self._walk(value, path + ('[]',)) for value in node]
        if node is None:
            return None

        return self._maskPart(self._otherwise, node)


    def mask(self, value: Any) -> Any:

        if self._fields is None:
            raise MaskingError('the json strategy masks its fields under the masking key, which only a masking plan gives it')

        if isinstance(value, str):
            try:
                document = json.loads(value)
            except ValueError:
                raise MaskingError('the json strategy needs a JSON document, and this text is not one') from None
            return json.dumps(self._walk(document, ()), ensure_ascii=False)

        if isinstance(value, (dict, list)):
            return self._walk(value, ())

        raise MaskingError('the json strategy needs a JSON document, got {}'.format(_typeName(value)))


STRATEGIES: Dict[str, Type[Strategy]] = {
    strategy.NAME: strategy for strategy in (
        KeepStrategy, NullStrategy, ConstantStrategy, HashStrategy, EmailStrategy, DigitsStrategy, NumberStrategy, DateShiftStrategy,
        FakeFirstNameStrategy, FakeLastNameStrategy, FakeNameStrategy, FakeCityStrategy, FakeCompanyStrategy, FakeStreetAddressStrategy,
        KeyStrategy, FPEStrategy, RedactStrategy, ShuffleStrategy, JsonStrategy, CoordinateStrategy,
        )
    }

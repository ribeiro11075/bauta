"""The one-to-one strategies: key, a keyed permutation, and fpe, NIST FF1.

Part of the strategies package; see its __init__.
"""
from __future__ import annotations

import decimal
import uuid
from typing import Any, Dict, List, Mapping, Optional

from ..core import NORMALIZE_STEPS, KeyedHash, MaskingError, Strategy
from .common import _booleanOption, _choiceOption, _digitCount, _normalizeOption, _requireAsciiCharset, _requireIdentifierLength, _typeName, structuredText


# _ALPHANUMERIC_CLASSES, _HEX_CLASSES: the alphabets `key` permutes each
#   character within, by charset. Per case, so a value keeps each character's
#   case and two spellings of one value never share a mask.
# _FPE_ALPHABETS: `fpe`'s alphabet for each charset. FF1 needs one alphabet for
#   every position, so it takes both cases together.
_ALPHANUMERIC_CLASSES = ('0123456789', 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')
_HEX_CLASSES = ('0123456789', 'abcdef', 'ABCDEF')
_FPE_ALPHABETS = {
    'digits': '0123456789',
    'hex': '0123456789abcdefABCDEF',
    'alphanumeric': '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ',
    }


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
        """An integer permuted among those with its sign and digit count. Zero
        belongs to the non-negative one-digit range only: letting a negative
        digit map to it would make -0 collide with 0's own image.
        """

        _requireIdentifierLength('key', _digitCount(value))
        magnitude = abs(value)
        digitCount = len(str(magnitude))
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
        """Text with the characters `charset` covers permuted within their
        classes, by position, and every other character kept where it is. The
        permutation is keyed on the text's shape too, so different shapes never
        meet.
        """

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
        """An integer, whole Decimal, UUID or text masked one-to-one in its own
        shape. Raises MaskingError for anything else.
        """

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
        """Adds the FF1 ciphers built so far, by radix, and the `key` strategy
        that masks values too short for FF1.
        """

        super().__init__(keyedHash, options)
        self._ciphers: Dict[int, Any] = {}
        self._short = KeyStrategy(keyedHash, {})


    def _cipher(self, radix: int) -> Any:
        """The FF1 cipher for `radix`, under a key derived from the column's
        subkey, built once.
        """

        from ..fpe import FF1

        if radix not in self._ciphers:
            self._ciphers[radix] = FF1(self.keyedHash.digest(b'', b'ff1 key'), radix)

        return self._ciphers[radix]


    def _tooShort(self, cipher: Any, what: str) -> None:
        """Raises under `strict`; otherwise the caller falls back to `key`."""

        if self.options.get('strict'):
            raise MaskingError('the fpe strategy is strict, and FF1 needs at least {} {} in a value; this one has fewer'.format(
                cipher.minimumLength, what))


    def _maskInteger(self, value: int) -> int:
        """An integer encrypted with FF1 over its digits, keeping its sign and
        digit count; one too short for FF1 is masked by `key` instead. Results
        with a leading zero, which would shorten the number, are cycle-walked
        past: the input has none, so the walk comes back to such a value.
        """

        _requireIdentifierLength('fpe', _digitCount(value))
        digits = [int(character) for character in str(abs(value))]
        cipher = self._cipher(10)

        if len(digits) < cipher.minimumLength:
            self._tooShort(cipher, 'digits')
            return self._short._maskInteger(value)

        tweak = b'negative' if value < 0 else b'integer'
        masked = cipher.encrypt(digits, tweak)
        while masked[0] == 0:
            masked = cipher.encrypt(masked, tweak)

        number = int(''.join(map(str, masked)))

        return -number if value < 0 else number


    def _maskText(self, text: str, charset: str) -> str:
        """Text with the characters of `charset` encrypted together with FF1
        and the rest kept; text with too few of them is masked by `key`
        instead.
        """

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
        """An integer, whole Decimal, UUID or text encrypted with FF1 in its
        own shape. Raises MaskingError for anything else.
        """

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

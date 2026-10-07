"""The fake* strategies: realistic names, cities, companies and street addresses from the lists in fakeData.

Part of the strategies package; see its __init__.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from ..fakeData import (COMPANY_WORDS, DEFAULT_LOCALE, FEMALE_NAMES, LARGE_DEFAULT_LOCALE, LARGE_LOCALES, LOCALES, MALE_NAMES, LargeLocale,
                       Locale)
from ..core import Strategy, canonical
from .common import _booleanOption, _choiceOption, _integerOption


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

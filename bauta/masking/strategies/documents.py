"""Text and documents: redact, which masks the identifiers it finds in text, and json, which masks inside a document.

Part of the strategies package; see its __init__.
"""
from __future__ import annotations

import functools
import json
import re
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Tuple

from ..core import KeyedHash, MaskingError, Strategy
from .common import _asciiDigits, _choiceOption, _inScriptOf, _typeName, luhnValid, structuredText
from .basic import DigitsStrategy, EmailStrategy
from .keys import KeyStrategy


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


# A path into a JSON document: keys joined with dots, `[]` for every element
# of an array: `contact.alt_email`, `orders[].card`, `[].email`.
_JSON_PATH = re.compile(r'^(?:[^.\[\]]+|\[\])(?:\.[^.\[\]]+|\[\])*$')


def _jsonPolicy(value: Any) -> Dict[str, Any]:
    """A policy for part of a JSON document: any column policy but `shuffle`,
    which moves values between rows, and a single value has none to move to.
    """

    from ..core import validateColumnPolicy

    from ..core import resolveStrategy

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


class _JsonNumber(float):
    """A JSON number with a fraction or an exponent, as a float -- what every
    strategy masks, and keys on, as it always has -- that remembers how it
    was written. A float holds 17 digits: 12345678901234567890.123 came back
    as 12345678901234567000, and 1.10 as 1.1, from documents only masked
    elsewhere. Written back unmasked, it is written as it came.
    """

    text: str

    def __new__(cls, text: str) -> '_JsonNumber':

        number = super().__new__(cls, text)
        number.text = text
        return number


def _jsonDocument(text: str) -> Tuple[Any, bool]:
    """JSON text parsed, and whether it holds a number _JsonNumber keeps."""

    found = False

    def number(text: str) -> _JsonNumber:
        nonlocal found
        found = True
        return _JsonNumber(text)

    return json.loads(text, parse_float=number), found


def _jsonText(node: Any) -> str:
    """A document as json.dumps writes it, spaced and escaped alike, except
    that a _JsonNumber is written as it came.
    """

    if isinstance(node, dict):
        return '{' + ', '.join('{}: {}'.format(json.dumps(str(key), ensure_ascii=False), _jsonText(value)) for key, value in node.items()) + '}'
    if isinstance(node, list):
        return '[' + ', '.join(_jsonText(value) for value in node) + ']'
    if isinstance(node, _JsonNumber):
        return node.text

    return json.dumps(node, ensure_ascii=False)


# What a value in a JSON document is masked with when its policy sets no
# `otherwise` and names no path to it, by the words of the key it is under,
# read as discover reads a column's name. Before, `redact` alone took such a
# value, and it finds identifiers by their shape: {"name": "Ann Smith", "dob":
# "1984-03-02", "address": "12 Main St"} came out unchanged. Kept here rather
# than read from discover's rules, so that changing those never changes a
# mask. The first entry with a word in the key wins: `company_name` is a
# company, `email_address` an email.
_JSON_KEY_POLICIES: Tuple[Tuple[FrozenSet[str], Dict[str, Any]], ...] = tuple((frozenset(words), policy) for words, policy in (
    (('email', 'emailaddress', 'mail'), {'strategy': 'email'}),
    (('password', 'passwd', 'pwd', 'secret', 'token', 'apikey', 'salt'), {'strategy': 'hash'}),
    (('ssn', 'socialsecurity', 'socialsecuritynumber', 'nationalid', 'taxid', 'tin', 'passport', 'passportnumber', 'licensenumber',
      'licencenumber', 'driverslicense'), {'strategy': 'key'}),
    (('creditcard', 'cardnumber', 'ccnumber', 'pan'), {'strategy': 'digits', 'keepTrailing': 4}),
    (('cvv', 'cvv2', 'cvc', 'cvc2', 'csc', 'cardsecuritycode', 'cardverificationcode', 'cardverificationvalue'), {'strategy': 'null'}),
    (('iban', 'accountnumber', 'routingnumber', 'bankaccount', 'sortcode'), {'strategy': 'digits'}),
    (('phone', 'phonenumber', 'mobile', 'cell', 'fax', 'telephone', 'tel'), {'strategy': 'digits'}),
    (('company', 'companyname', 'employer', 'organization', 'organisation'), {'strategy': 'fakeCompany'}),
    (('firstname', 'givenname', 'forename'), {'strategy': 'fakeFirstName'}),
    (('lastname', 'surname', 'familyname'), {'strategy': 'fakeLastName'}),
    (('name', 'fullname', 'contactname', 'customername', 'displayname', 'personname', 'employeename'), {'strategy': 'fakeName'}),
    (('username', 'login', 'handle', 'screenname'), {'strategy': 'hash'}),
    (('ip', 'ipaddress', 'ipaddr'), {'strategy': 'hash'}),
    (('street', 'address', 'addressline', 'addr', 'line1', 'line2', 'streetaddress'), {'strategy': 'fakeStreetAddress'}),
    (('city', 'town'), {'strategy': 'fakeCity'}),
    (('zip', 'zipcode', 'postal', 'postalcode', 'postcode'), {'strategy': 'digits'}),
    (('birth', 'birthdate', 'dob', 'birthday', 'dateofbirth'), {'strategy': 'dateShift', 'maxDays': 30}),
    (('gender', 'sex', 'race', 'ethnicity', 'religion', 'nationality'), {'strategy': 'null'}),
    (('latitude', 'lat', 'longitude', 'lng', 'lon'), {'strategy': 'null'}),
    ))


def _keyWords(key: str) -> FrozenSet[str]:
    """A JSON key's words, lower-cased, with the whole key run together and
    each word's singular: `homeAddresses` gives home, addresses, address and
    homeaddresses.
    """

    split = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', key)
    words = [word for word in re.split(r'[^a-z0-9]+', split.lower()) if word]
    singular = {word[:-2] if word.endswith(('sses', 'ches', 'shes', 'xes')) else word[:-1] for word in words if word.endswith('s') and len(word) > 3}

    return frozenset(words) | {''.join(words)} | singular


@functools.lru_cache(maxsize=4096)
def _jsonKeyPolicy(key: Any) -> Optional[Dict[str, Any]]:
    """The policy _JSON_KEY_POLICIES gives a value under `key`, or None.
    Remembered, since documents repeat their keys: read afresh for every
    one, matching cost a third of a document's masking.
    """

    if not isinstance(key, str):
        return None
    words = _keyWords(key)

    return next((policy for keyWords, policy in _JSON_KEY_POLICIES if words & keyWords), None)


class JsonStrategy(Strategy):
    """Masks inside a JSON document: each path `fields` names with its own
    policy, and every other value with `otherwise` -- by default `redact`,
    which masks the identifiers it finds by their shape in text, and in whole
    numbers long enough to be a phone or card number, and leaves other numbers
    and booleans as they are. A policy named for a path holding an object or an
    array applies to it whole, so `null` drops it.

    Without `otherwise`, a value under a key that names personal data -- `name`,
    `dob`, `address`, `city` and the rest of _JSON_KEY_POLICIES -- is masked as
    that kind, in its key's domain, as each element of an array under it is.
    A value that kind's strategy can't mask fails the job, naming its path.
    Setting `otherwise` masks every value it covers with it alone.

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
        # By key, only where `otherwise` is left to its default; built as
        # keys are met, each in its key's domain.
        self._byKey = 'otherwise' not in self.options
        self._keyStrategies: Dict[Tuple[int, str], Strategy] = {}
        self._maskingKey: Optional[str] = None
        self._keys = None if self._otherwise.PASSTHROUGH else RedactStrategy(keyedHash, {'replacement': 'mask'})


    @staticmethod
    def _build(keyedHash: KeyedHash, policy: Mapping[str, Any]) -> Strategy:

        from ..core import POLICY_FIELDS, resolveStrategy

        strategy = resolveStrategy(policy['strategy'])(keyedHash, {name: value for name, value in policy.items() if name not in POLICY_FIELDS})

        return strategy


    def bindKey(self, key: str) -> None:

        self._fields = {}
        for path, policy in self.options['fields'].items():
            domain = policy.get('domain') or next(part for part in reversed(path.replace('[]', '.').split('.')) if part).lower()
            self._fields[path] = self._build(KeyedHash(key, domain), policy)
            self._fields[path].bindKey(key)
        self._otherwise.bindKey(key)
        self._maskingKey = key
        self._keyStrategies = {}


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


    def _keyStrategy(self, key: Any) -> Optional[Strategy]:
        """The strategy for a value under `key` by its words, or None."""

        if not self._byKey or self._maskingKey is None:
            return None
        policy = _jsonKeyPolicy(key)
        if policy is None:
            return None

        domain = str(key).lower()
        cacheKey = (id(policy), domain)
        if cacheKey not in self._keyStrategies:
            strategy = self._build(KeyedHash(self._maskingKey, domain), policy)
            strategy.bindKey(self._maskingKey)
            self._keyStrategies[cacheKey] = strategy

        return self._keyStrategies[cacheKey]


    def _walk(self, node: Any, path: Tuple[str, ...], byKey: Optional[Strategy] = None, inherited: bool = False) -> Any:
        """`byKey` is the strategy the nearest key above names, carried into
        an array's elements and into an object's values, where a value's own
        key names none: {"name": {"first": "Ann", "last": "Lee"}} was copied
        as it stood, since `first` and `last` alone name nothing. `inherited`
        says it came from further up than the value's own key: then a value
        it can't mask -- a count under `byEmail` -- goes to `otherwise`
        rather than failing the job, as one under its own key does.
        """

        assert self._fields is not None
        if path:
            strategy = self._fields.get(_pathText(path))
            if strategy is not None:
                return self._maskPart(strategy, node)
        if isinstance(node, dict):
            masked = {}
            for key, value in node.items():
                own = self._keyStrategy(key)
                masked[self._maskKey(key)] = self._walk(value, path + (str(key),), own or byKey, inherited=own is None and byKey is not None)
            return masked
        if isinstance(node, list):
            return [self._walk(value, path + ('[]',), byKey, inherited) for value in node]
        if node is None:
            return None

        if byKey is not None and not isinstance(node, bool) and not (isinstance(node, str) and not node.strip()):
            try:
                return byKey.maskColumn([node], 0)[0]
            except MaskingError as error:
                if inherited:
                    return self._maskPart(self._otherwise, node)
                raise MaskingError('{} masks a value at {} as its key says, and {}; name the path in fields to mask it '
                                   'another way'.format(self.NAME, _pathText(path), error)) from None

        return self._maskPart(self._otherwise, node)


    def mask(self, value: Any) -> Any:

        if self._fields is None:
            raise MaskingError('the json strategy masks its fields under the masking key, which only a masking plan gives it')

        if isinstance(value, str):
            try:
                document, exact = _jsonDocument(value)
            except ValueError:
                raise MaskingError('the json strategy needs a JSON document, and this text is not one') from None
            masked = self._walk(document, ())
            # json.dumps, in C, wherever no number needs writing as it came.
            return _jsonText(masked) if exact else json.dumps(masked, ensure_ascii=False)

        if isinstance(value, (dict, list)):
            return self._walk(value, ())

        raise MaskingError('the json strategy needs a JSON document, got {}'.format(_typeName(value)))

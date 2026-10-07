"""Generates the vectors the Rust port is checked against.

The Python implementation is the reference: every vector here is what
bauta.masking produces today, and a Rust build that disagrees with
any of them is a silent key change for anyone who has already masked data.
The strategies with no port are recorded as well, under `pythonOnly`, so that
tests/masking/test_maskVectors.py catches Python changing those masks too.

Run from the repository root:  python3 mask-rs/generate_vectors.py
"""
from __future__ import annotations

import datetime
import decimal
import ipaddress
import hashlib
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bauta.masking import COMPANY_WORDS, DEFAULT_LOCALE, LOCALES, STRATEGIES, KeyedHash
from bauta.masking.fakeData import LARGE_LOCALES

KEY = 'a-test-key-that-is-long-enough'
DOMAIN = 'vectors'

# Domain sizes that exercise the awkward corners of permute(): the degenerate
# sizes, both parities of bit width, the byte edges where a Feistel half stops
# fitting its serialised width, and the sizes real shapes actually produce.
PERMUTE_SIZES = [
    1, 2, 3, 4, 5, 7, 8, 9, 15, 16, 17, 31, 32, 33, 63, 64, 65, 100, 255, 256, 257, 1000,
    10 ** 6, 10 ** 7 - 10 ** 6, 2 ** 16, 2 ** 17, 2 ** 32, 2 ** 33, 2 ** 48, 2 ** 49,
    16 ** 32,                  # a UUID: exactly 2**128, one bit past u128
    26 ** 4 * 10 ** 7,         # an 11-character alphanumeric shape, odd bit width
    62 ** 8, 62 ** 20,         # inside u128
    62 ** 22,                  # the first alphanumeric length past u128
    10 ** 40, 62 ** 60,        # well into bignum
    ]

# Values chosen for the boundaries rather than for volume: the u128 cliff at 21
# to 22 alphanumerics and 32 to 33 hex, MAXIMUM_KEY_LENGTH at 256, and the
# one-character and empty shapes that make `size` degenerate.
TEXTS = [
    '', 'a', '0', 'Z', 'ab', '00', 'user0000001', 'USER0000001',
    '0123456789', '01234567890123456789', '012345678901234567890',
    '0123456789012345678901', 'a' * 21, 'a' * 22, 'A1b2C3d4E5f6G7h8I9j0K',
    'deadbeef', 'deadbeefcafebabe0123456789abcdef', 'deadbeefcafebabe0123456789abcdef0',
    'DEADBEEF', 'DeadBeef', '00000000-0000-4000-a000-000000000000',
    'x' * 255, 'x' * 256, 'user@example.com', '+1 (555) 010-9999', 'héllo',
    ]

INTEGERS = [0, 1, -1, 9, 10, -10, 99, 100, 12345, -12345, 10 ** 6, 10 ** 18, -10 ** 18, 10 ** 38, 10 ** 40]

# What `number` masks besides integers: floats as repr spells them and Decimals
# as str does, including the spellings, scales and signed zeros that decimal
# arithmetic treats specially.
NUMBERS = INTEGERS + [0.0, -0.0, 0.5, 2.5, -0.001, 1e-07, 123456.789, 1e22, 1.5e16, float('inf'), float('nan')] + [
    decimal.Decimal(text) for text in ('0', '-0.00', '12.30', '-12.3400', '1.2E+5', '0.0000123', '1.005', '99999.99', '-7.5', 'NaN',
                                       '1.2345678901234567890123456789')]


def hashVectors() -> dict:
    keyedHash = KeyedHash(KEY, DOMAIN)
    messages = [b'', b'\x00', b'a', b'hello', b'x' * 63, b'x' * 64, b'x' * 65, b'\xff' * 200]
    purposes = [b'', b'integer', b'negative', b'text|hex|8']

    return {
        'digest': [
            {'message': message.hex(), 'purpose': purpose.hex(), 'digest': keyedHash.digest(message, purpose).hex()}
            for message in messages for purpose in purposes
            ],
        'expand': [
            {'message': message.hex(), 'length': length, 'expand': keyedHash.expand(message, length).hex()}
            for message in messages[:4] for length in (1, 7, 31, 32, 33, 64, 65, 200)
            ],
        'below': [
            # `upper` is text, not a JSON number: 2**128 has no exact JSON
            # number, and every other integer here is written the same way.
            {'message': message.hex(), 'upper': str(upper), 'below': str(keyedHash.below(message, upper))}
            for message in messages[:4] for upper in (1, 2, 7, 256, 10 ** 6, 2 ** 64, 2 ** 128 + 1)
            ],
        'unit': [
            {'message': message.hex(), 'unit': repr(keyedHash.unit(message))}
            for message in messages
            ],
        'permute': [
            {'size': str(size), 'value': str(value), 'permute': str(keyedHash.permute(size, value))}
            for size in PERMUTE_SIZES for value in {0, 1, size // 2, size - 1, min(size - 1, 12345)}
            ],
        }


FAKE_STRATEGIES = ['fakeFirstName', 'fakeLastName', 'fakeName', 'fakeCity', 'fakeCompany', 'fakeStreetAddress']


def fakeLists() -> dict:
    """The lists the fake* strategies pick from, per locale -- the default ones
    under "default". The Rust tests build their maskers from these, as the
    extension builds them from what Python hands it, so there is one copy.
    """

    def lists(locale) -> dict:
        return {'firstNames': list(locale.firstNames), 'lastNames': list(locale.lastNames), 'cities': list(locale.cities),
                'streets': list(locale.streets), 'streetKinds': list(locale.streetKinds), 'address': locale.address,
                'companySuffixes': list(locale.companySuffixes), 'companyWords': list(COMPANY_WORDS)}

    return {'default': lists(DEFAULT_LOCALE), **{name: lists(locale) for name, locale in sorted(LOCALES.items())}}


def strategyVectors() -> dict:
    """Every strategy the port will cover, over the boundary corpus.

    A refusal is a vector too: the Rust layer has to raise MaskingError with
    the same message, or a job that fails today would quietly succeed.
    """

    combinations = [
        ('key', {}), ('key', {'charset': 'hex'}), ('key', {'charset': 'digits'}),
        ('fpe', {}), ('fpe', {'charset': 'hex'}), ('fpe', {'charset': 'digits'}), ('fpe', {'strict': True}),
        ('hash', {}), ('hash', {'length': 20, 'prefix': 'c_'}),
        ('email', {}), ('email', {'keepDomain': True}),
        ('digits', {}),
        ] + [(name, options) for name in FAKE_STRATEGIES for options in [{}, {'maxLength': 4}] + [{'locale': locale} for locale in sorted(LOCALES)]]

    combinations += [('number', {}), ('number', {'variance': '0.5'}), ('number', {'min': '0', 'max': '100'}), ('number', {'decimals': 2}),
                     ('number', {'min': '1', 'max': '2', 'decimals': 3})]

    values = TEXTS + INTEGERS + [uuid.UUID('00000000-0000-4000-a000-000000000000'), None, True]
    out = {}

    for name, options in combinations:
        strategyClass = STRATEGIES[name]
        strategy = strategyClass(KeyedHash(KEY, DOMAIN), strategyClass.validateOptions(options))
        results = []

        for value in (NUMBERS + [None, True, 'text'] if name == 'number' else values):
            entry = {'type': type(value).__name__, 'value': None if value is None else str(value)}
            try:
                masked = strategy.maskColumn([value], 0)[0]
                entry['masked'] = None if masked is None else str(masked)
                entry['maskedType'] = type(masked).__name__
            except Exception as error:
                entry['error'] = type(error).__name__
                entry['message'] = str(error)
            results.append(entry)

        out['{} {}'.format(name, json.dumps(options, sort_keys=True))] = results

    return out


DATES = [datetime.date(2020, 2, 29), datetime.date(1970, 1, 1), datetime.date.min, datetime.date.max, datetime.date(1, 1, 2),
         datetime.date(9999, 12, 30), datetime.datetime(2026, 9, 21, 16, 30, 5), datetime.datetime(2026, 9, 21, 23, 59, 59, 999999),
         '2026-09-21', '2026-09-21T16:30:05', '2026-09-21 16:30:05', '2026-09-21T16:30:05+02:00', '0001-01-01', 20260921]

REDACT_TEXTS = [
    'Called Ann at +1 (555) 010-9999, email Ann.Lee@corp.example.com; card 4111 1111 1111 1111 SSN 123-45-6789 '
    'IBAN GB82 WEST 1234 5698 7654 32 from 192.168.1.20. Order 2026-01-02, qty 12, v1.2.3.',
    'reach me at ann.lee@CORP.example.com about ACC-123456', 'no identifiers here', '',
    ]


COORDINATES = [51.50135, -0.14189, 0.0, 89.9999, -89.9999, 179.9999, -180.0, decimal.Decimal('38.722300'), decimal.Decimal('-9.1'), 7]

# Addresses as each driver returns them, and what ip refuses.
IP_ADDRESSES = ['192.168.1.1', '10.0.0.1', '0.0.0.0', '255.255.255.255', '2001:db8::1', 'fe80::1/64', '10.1.2.3/24', '10.20.0.0/16',
                ipaddress.ip_address('172.16.254.3'), ipaddress.ip_interface('10.1.2.3/24'), b'\x0a\x00\x00\x01', 3232235777, 'not an address']

# Degrees times a scale, as a column of microdegrees holds them; the last two
# are past the poles even scaled, and refused.
SCALED_COORDINATES = [51501350, -141890, 0, 89999900, decimal.Decimal('38722300.5'), 51501350.0, 90000001, 51501350000]

NORMALIZED_TEXTS = ['AB12CD', 'ab12cd   ', ' Ab12Cd ', '1234567', '-1234567', '0012345', 1234567, '', None]

JSON_DOCUMENTS = [
    {'contact': {'email': 'Ann.Lee@corp.example.com', 'phone': 5550109999}, 'card': 4111111111111111, 'visits': 3, 'total': 12.5,
     'vip': True, 'byEmail': {'bob@corp.example': 1}, 'notes': ['call +1 555 010 9999', None]},
    '{"customer_id": 42, "ssn": "123-45-6789", "ssnNumber": 123456789}',
    ]


def pythonOnlyVectors() -> dict:
    """The strategies only Python implements. No port reads these; they are
    here so that a change to what they return fails test_maskVectors.py as
    loudly as a change to the ported ones would, since it changes every mask
    already made just the same.

    shuffle is keyed on the chunk rather than a value, so its vectors are
    whole columns at a few chunk positions.
    """

    def build(name, options):
        return STRATEGIES[name](KeyedHash(KEY, DOMAIN), STRATEGIES[name].validateOptions(options))

    def entry(strategy, value):
        result = {'type': type(value).__name__, 'value': str(value)}
        try:
            masked = strategy.maskColumn([value], 0)[0]
            result.update(masked=str(masked), maskedType=type(masked).__name__)
        except Exception as error:
            result.update(error=type(error).__name__, message=str(error))
        return result

    out = {}
    for name, options, values in [
            ('dateShift', {}, DATES), ('dateShift', {'maxDays': 10}, DATES),
            ('ip', {}, IP_ADDRESSES), ('ip', {'keepPrefix': 16}, IP_ADDRESSES),
            ('redact', {}, REDACT_TEXTS), ('redact', {'replacement': 'mask'}, REDACT_TEXTS),
            ('redact', {'replacement': 'mask', 'detect': ['email'], 'patterns': [r'ACC-\d{6}']}, REDACT_TEXTS),
            # normalize runs in Python before either implementation masks.
            ('key', {'normalize': ['strip', 'lower', 'integer']}, NORMALIZED_TEXTS),
            ('fpe', {'normalize': ['integer'], 'charset': 'digits'}, NORMALIZED_TEXTS),
            ('hash', {'normalize': ['strip', 'lower']}, NORMALIZED_TEXTS),
            ]:
        strategy = build(name, options)
        out['{} {}'.format(name, json.dumps(options, sort_keys=True))] = [entry(strategy, value) for value in values]

    # Strategies that mask with another column of the row, given as (value, that column's value).
    def contextEntry(strategy, value, context):
        result = entry(strategy, value)
        result.update(context=str(context), contextType=type(context).__name__)
        try:
            masked = strategy.maskColumnWith([value], [context], 0)[0]
            result.update(masked=str(masked), maskedType=type(masked).__name__)
        except Exception as error:
            result.update(error=type(error).__name__, message=str(error))
        return result

    for name, options, pairs in [
            ('dateShift', {'shiftBy': 'patient_id'}, [(value, subject) for value in DATES[:9] for subject in (101, '101', 'P-7', None)]),
            ('coordinate', {'axis': 'latitude'}, [(value, None) for value in COORDINATES]),
            ('coordinate', {'axis': 'longitude', 'meters': 5000, 'latitudeColumn': 'lat'},
             [(value, latitude) for value in COORDINATES for latitude in (0, 51.5, -89.99, None)]),
            ('coordinate', {'axis': 'latitude', 'scale': 1000000}, [(value, None) for value in SCALED_COORDINATES]),
            ('coordinate', {'axis': 'longitude', 'latitudeColumn': 'lat', 'scale': 10000000},
             [(value, latitude) for value in (-1418900, decimal.Decimal('-1418900.5'), -1800000000) for latitude in (515013500, None)]),
            ]:
        strategy = build(name, options)
        out['{} {}'.format(name, json.dumps(options, sort_keys=True))] = [contextEntry(strategy, value, context) for value, context in pairs]

    # lists: 2, which only Python implements: names as people write them, and
    # every list's digest, since the masks above sample only some entries.
    names = ['John', 'MARY', 'smith', 'John Smith', 'Smith, Mary Ann', 'Jan de Jong', '  ana   souza ', 'Karl-Heinz', '', 'O\'Brien', 42]
    for name, options in [('fakeFirstName', {'lists': 2}), ('fakeFirstName', {'lists': 2, 'matchGender': True, 'locale': 'de_DE'}),
                          ('fakeLastName', {'lists': 2}), ('fakeName', {'lists': 2, 'matchGender': True}),
                          ('fakeName', {'lists': 2, 'locale': 'nl_NL', 'maxLength': 12}), ('fakeCity', {'lists': 2, 'locale': 'pt_BR'})]:
        strategy = build(name, options)
        out['{} {}'.format(name, json.dumps(options, sort_keys=True))] = [entry(strategy, value) for value in names]
    out['largeFakeLists'] = {locale: {field: hashlib.sha256(json.dumps(names, ensure_ascii=False).encode('utf-8')).hexdigest()
                                      for field, names in lists._asdict().items()}
                             for locale, lists in sorted(LARGE_LOCALES.items())}

    # json masks its fields in domains of their own, under the key itself.
    for options in ({'fields': {'contact.email': 'email', 'customer_id': {'strategy': 'key', 'domain': 'customers'}}},
                    {'fields': {'card': 'null'}, 'otherwise': {'strategy': 'redact', 'replacement': 'label'}},
                    {'fields': {'visits': 'keep'}, 'otherwise': 'keep'}):
        strategy = build('json', options)
        strategy.bindKey(KEY)
        out['json {}'.format(json.dumps(options, sort_keys=True))] = [entry(strategy, value) for value in JSON_DOCUMENTS]

    columns = [list(range(20)), ['v{}'.format(index) for index in range(7)], [1, 2]]
    out['shuffle {}'] = [{'chunk': chunk, 'column': column, 'masked': build('shuffle', {}).maskColumn(column, chunk)}
                         for chunk in (0, 1, 7) for column in columns]

    return out


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    vectors = {
        'key': KEY,
        'domain': DOMAIN,
        'keyedHash': hashVectors(),
        'strategies': strategyVectors(),
        'pythonOnly': pythonOnlyVectors(),
        'fakeLists': fakeLists(),
        }

    path = os.path.join(here, 'vectors', 'reference.json')
    with open(path, 'w') as handle:
        json.dump(vectors, handle, indent=1, sort_keys=True)
        handle.write('\n')

    counts = {name: len(entries) for name, entries in vectors['keyedHash'].items()}
    counts['strategies'] = sum(len(entries) for entries in vectors['strategies'].values())
    counts['pythonOnly'] = sum(len(entries) for entries in vectors['pythonOnly'].values())
    print('wrote {} ({:,} bytes)'.format(path, os.path.getsize(path)))
    for name, count in sorted(counts.items()):
        print('  {:<12} {:>6,} vectors'.format(name, count))


if __name__ == '__main__':
    main()

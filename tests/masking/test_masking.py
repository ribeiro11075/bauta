"""The masking engine, without a database.

Most of what matters about masking is a property rather than an example --
deterministic, consistent within a domain, one-to-one for keys, type-preserving
for numbers -- so these tests state the property and check it over many values.
"""
import datetime
import decimal
import hmac
import ipaddress
import json
import re
import unicodedata
import uuid

import pytest

from bauta.configuration import Configuration, ConfigurationError, DataJobsFile
from bauta.jobs.dependencyGraph import JobOutcome, JobStatus
from bauta.masking import (FIRST_NAMES, LAST_NAMES, STRATEGIES, KeyedHash, MaskingError, MaskingPlan, buildMaskingManifest,
                                     keyFingerprint, validateColumnPolicy)
from tests.jobConfigs import dataJobFields

KEY = 'a-test-key-that-is-long-enough'
OTHER_KEY = 'a-different-key-also-long-enough'


def strategy(name, key=KEY, domain='test', **options):
    return STRATEGIES[name](KeyedHash(key, domain), STRATEGIES[name].validateOptions(options))


def pythonStrategy(name, **options):
    """A strategy with the native masker off, for the tests that are about the
    Python implementation itself -- its cache, which the extension replaces with
    per-batch deduplication. What both must agree on is masks, not bookkeeping.
    """

    built = strategy(name, **options)
    built._native = None

    return built


def maskOne(name, value, **options):
    return strategy(name, **options).maskColumn([value], 0)[0]


# --- the keyed hash ---------------------------------------------------------

def test_the_same_value_masks_the_same_way_every_time():
    assert maskOne('hash', 'alice') == maskOne('hash', 'alice')


def test_the_same_value_masks_the_same_way_in_the_same_domain_across_columns():
    """The property referential consistency rests on: domain, not column, decides."""
    first = strategy('key', domain='customer').maskColumn([41, 42], 0)
    second = strategy('key', domain='customer').maskColumn([42, 41], 0)

    assert first == list(reversed(second))


def test_different_domains_mask_differently():
    assert strategy('hash', domain='a').maskColumn(['alice'], 0) != strategy('hash', domain='b').maskColumn(['alice'], 0)


def test_a_different_key_changes_every_mask():
    values = ['alice', 'bob', 'carol']

    assert all(left != right for left, right in zip(strategy('hash').maskColumn(values, 0), strategy('hash', key=OTHER_KEY).maskColumn(values, 0)))


def test_an_integer_and_its_decimal_and_text_forms_mask_identically():
    """Drivers disagree on numeric types -- int, Decimal, a whole float -- and some schemas store ids as text."""
    masked = {maskOne('hash', 42), maskOne('hash', decimal.Decimal('42')), maskOne('hash', decimal.Decimal('42.00')), maskOne('hash', 42.0), maskOne('hash', '42')}

    assert len(masked) == 1


@pytest.mark.parametrize('size', [1, 2, 3, 10, 17, 100, 256, 1000])
def test_permute_is_a_bijection(size):
    keyedHash = KeyedHash(KEY, 'permute')

    assert sorted(keyedHash.permute(size, value) for value in range(size)) == list(range(size))


def test_permute_handles_domains_wider_than_one_digest():
    keyedHash = KeyedHash(KEY, 'wide')
    size = 62 ** 60

    outputs = {keyedHash.permute(size, value) for value in range(200)}

    assert len(outputs) == 200
    assert all(0 <= output < size for output in outputs)


@pytest.mark.parametrize('message', [b'', b'a', b'\x00', b'x' * 63, b'x' * 64, b'x' * 65, b'\xff' * 1000])
@pytest.mark.parametrize('purpose', [b'', b'integer', b'#\x00\x00\x00\x01'])
def test_digest_is_hmac_sha256_over_the_documented_message(message, purpose):
    """security.md promises digest(m, p) = HMAC-SHA256(subkey, p || 0x00 || m).

    KeyedHash keeps the two pad states rather than re-keying per call, so this
    checks the shortcut against the library's one-shot HMAC. Every mask in the
    package comes from here: a digest that drifted from HMAC would silently
    change every masked value, which reads downstream as a changed key.
    """

    keyedHash = KeyedHash(KEY, 'digest')
    subkey = hmac.digest(KEY.encode('utf-8'), b'domain\x00' + b'digest', 'sha256')

    assert keyedHash.digest(message, purpose) == hmac.digest(subkey, purpose + b'\x00' + message, 'sha256')


def test_digest_matches_recorded_values():
    """A known answer, so that changing both the shortcut and the comparison
    above at once still fails. These are the masks a deployment already holds.
    """

    keyedHash = KeyedHash('a-test-key-that-is-long-enough', 'recorded')

    assert keyedHash.digest(b'').hex() == '9338fbaa8004b74d220a3d9949fba47b9fe660625f7fa6f7511d97fd4bcc6e53'


def test_the_key_fingerprint_is_stable_and_does_not_reveal_the_key():
    assert keyFingerprint(KEY) == keyFingerprint(KEY)
    assert keyFingerprint(KEY) != keyFingerprint(OTHER_KEY)
    assert len(keyFingerprint(KEY)) == 12
    assert KEY not in keyFingerprint(KEY)


@pytest.mark.parametrize('key', ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'abababababababababab', '1212121212121212', 'abcdefgabcdefgabcdefg'])
def test_a_key_that_is_a_pattern_is_refused_without_quoting_it(key):
    from bauta.masking import validateKey

    with pytest.raises(ValueError, match='pattern rather than a secret') as error:
        MaskingPlan(key, {'a': 'hash'})
    assert key[:8] not in str(error.value)

    with pytest.raises(ValueError, match='manifest signing key'):
        validateKey(key, 'manifest signing key')


def test_key_strength_counts_repetition_and_the_alphabet_against_a_key():
    """A fresh random key was asserted to estimate over 200 bits, and about one
    in 290 doesn't -- repeated characters count against it -- so the test
    failed at random, about one CI run in 25. Over 200 is checked on a key
    that is; what matters of random ones, that none is ever warned about, on
    many, against the bound that warns.
    """
    import secrets
    from bauta.masking import KEY_RECOMMENDED_BITS, keyStrengthBits

    assert keyStrengthBits('Zk8_qV2xN7mWcT4pLr9sHd6yBf3jGe1uKa5oXn0iQwE') > 200
    assert min(keyStrengthBits(secrets.token_urlsafe(32)) for _ in range(5000)) > KEY_RECOMMENDED_BITS
    assert keyStrengthBits('Password1234567!') < KEY_RECOMMENDED_BITS
    assert keyStrengthBits('abcdefgh' * 4) < keyStrengthBits(secrets.token_urlsafe(24))
    assert keyStrengthBits('') == 0


def test_a_weak_key_is_warned_about_once_by_its_fingerprint(caplog):
    import logging
    import secrets
    from bauta.masking import warnIfWeakKey

    weak, strong = 'weak-but-allowed-' + secrets.token_hex(2), secrets.token_urlsafe(32)
    packageLogger = logging.getLogger('bauta')
    packageLogger.addHandler(caplog.handler)
    try:
        for _ in range(3):
            warnIfWeakKey(weak)
            warnIfWeakKey(strong)
    finally:
        packageLogger.removeHandler(caplog.handler)

    warnings = [record.getMessage() for record in caplog.records if 'estimated' in record.getMessage()]
    assert len(warnings) == 1 and keyFingerprint(weak) in warnings[0]
    assert weak not in warnings[0]


# --- NULLs and the simple strategies ----------------------------------------

@pytest.mark.parametrize('name', ['hash', 'email', 'digits', 'number', 'dateShift', 'fakeName', 'key', 'keep'])
def test_null_passes_through(name):
    assert strategy(name).maskColumn([None], 0) == [None]


def test_keep_leaves_values_alone():
    assert strategy('keep').maskColumn(['a', 1, None], 0) == ['a', 1, None]


def test_null_replaces_everything():
    assert strategy('null').maskColumn(['a', 1, None], 0) == [None, None, None]


def test_constant_replaces_nulls_too():
    assert strategy('constant', value='redacted').maskColumn(['a', None], 0) == ['redacted', 'redacted']


def test_constant_requires_a_value():
    with pytest.raises(ValueError, match='requires option'):
        validateColumnPolicy({'strategy': 'constant'})


# --- hash ---------------------------------------------------------------------

def test_hash_honours_length_and_prefix():
    masked = maskOne('hash', 'alice', length=20, prefix='cust_')

    assert masked.startswith('cust_')
    assert len(masked) == 25
    assert int(masked[5:], 16) >= 0


def test_hash_refuses_a_length_too_short_to_stay_unique():
    with pytest.raises(ValueError, match='between 12 and 64'):
        validateColumnPolicy({'strategy': 'hash', 'length': 8})


def test_email_refuses_a_length_too_short_to_stay_unique():
    """8 hex characters, 32 bits, was allowed: two of 100,000 addresses
    masked alike more often than not, and a unique email column refused the
    load. 12 is hash's minimum too.
    """
    with pytest.raises(ValueError, match='between 12 and 40'):
        validateColumnPolicy({'strategy': 'email', 'length': 8})
    validateColumnPolicy({'strategy': 'email', 'length': 24})


# --- email -------------------------------------------------------------------

def test_email_stays_shaped_like_an_email():
    masked = maskOne('email', 'Alice.Smith@corp.com')

    local, domain = masked.split('@')
    assert domain == 'example.test'
    assert local.startswith('u') and len(local) == 13


def test_email_is_case_insensitive():
    assert maskOne('email', 'Alice@Corp.com') == maskOne('email', 'alice@corp.com ')


def test_email_can_keep_or_replace_the_domain():
    assert maskOne('email', 'alice@corp.com', keepDomain=True).endswith('@corp.com')
    assert maskOne('email', 'alice@corp.com', mailDomain='masked.invalid').endswith('@masked.invalid')


def test_email_rejects_both_domain_options():
    with pytest.raises(ValueError, match='not both'):
        validateColumnPolicy({'strategy': 'email', 'keepDomain': True, 'mailDomain': 'x.test'})


def test_email_rejects_a_non_text_value_without_echoing_it():
    with pytest.raises(MaskingError) as error:
        maskOne('email', 5551234)

    assert '5551234' not in str(error.value)


# --- digits ------------------------------------------------------------------

def test_digits_keeps_the_format():
    masked = maskOne('digits', '+1 (555) 010-9999')

    assert len(masked) == len('+1 (555) 010-9999')
    assert [character.isdigit() for character in masked] == [character.isdigit() for character in '+1 (555) 010-9999']
    assert masked[0] == '+' and masked[3] == '(' and masked[7:9] == ') '
    assert masked != '+1 (555) 010-9999'


def test_digits_masks_the_same_number_the_same_way_however_it_is_formatted():
    plain = maskOne('digits', '5550109999')
    formatted = maskOne('digits', '(555) 010-9999')

    assert ''.join(character for character in formatted if character.isdigit()) == plain


def test_digits_can_keep_leading_and_trailing_digits():
    masked = maskOne('digits', '4111 1111 1111 1234', keepLeading=1, keepTrailing=4)

    assert masked.startswith('4')
    assert masked.endswith('1234')


def test_digits_keeps_an_integers_digit_count_and_sign():
    for value in (7, 10, 5550109999, -12345):
        masked = maskOne('digits', value)
        assert isinstance(masked, int)
        assert len(str(abs(masked))) == len(str(abs(value)))
        assert (masked < 0) == (value < 0)


def test_digits_leaves_text_without_digits_alone():
    assert maskOne('digits', 'n/a') == 'n/a'


# --- number ------------------------------------------------------------------

def test_number_keeps_an_int_an_int():
    assert isinstance(maskOne('number', 1234), int)


def test_number_keeps_a_decimals_precision():
    """NUMERIC columns arrive as Decimal from PostgreSQL, MySQL and SQL Server;
    the old random masking crashed on them, and on floats.
    """
    masked = maskOne('number', decimal.Decimal('1234.56'))

    assert isinstance(masked, decimal.Decimal)
    assert masked.as_tuple().exponent == -2


def test_number_handles_an_integral_decimal():
    masked = maskOne('number', decimal.Decimal('1234'))

    assert isinstance(masked, decimal.Decimal)
    assert masked == masked.to_integral_value()


def test_number_keeps_a_float_a_float():
    assert isinstance(maskOne('number', 12.5), float)


def test_number_stays_within_its_variance():
    masked = strategy('number', variance=0.1).maskColumn([decimal.Decimal('200.00')] + [decimal.Decimal(value) for value in range(100, 200)], 0)

    assert decimal.Decimal('180') <= masked[0] <= decimal.Decimal('220')


def test_number_stays_within_its_range():
    masked = strategy('number', min=18, max=90).maskColumn(list(range(1000)), 0)

    assert all(18 <= value <= 90 for value in masked)
    assert len(set(masked)) > 30


def test_number_respects_a_range_whose_bounds_are_off_the_values_grid():
    masked = strategy('number', min=0.005, max=0.015).maskColumn([decimal.Decimal('1.00')] * 1 + [decimal.Decimal(value) / 7 for value in range(1, 50)], 0)

    assert all(decimal.Decimal('0.005') <= value <= decimal.Decimal('0.015') for value in masked[:1])


def test_number_decimals_overrides_the_precision():
    masked = maskOne('number', 12.3456, decimals=1)

    assert masked == round(masked, 1)


def test_number_passes_non_finite_values_through():
    assert maskOne('number', decimal.Decimal('NaN')).is_nan()
    assert maskOne('number', float('inf')) == float('inf')


@pytest.mark.parametrize('value', ['12', True, datetime.date(2026, 1, 1)])
def test_number_rejects_what_is_not_a_number(value):
    with pytest.raises(MaskingError, match='needs a number'):
        maskOne('number', value)


@pytest.mark.parametrize('value,options', [(10 ** 61, {}), (-(10 ** 70), {'variance': 0.5}), (1.7976931348623157e308, {'decimals': 2})])
def test_number_refuses_a_value_past_its_precision_as_a_masking_error(value, options):
    """decimal raised InvalidOperation, which a plan passed on without the
    column's name, and printed nothing a person could act on."""
    bound = MaskingPlan(KEY, {'amount': dict(options, strategy='number')}).bind(['amount'])

    with pytest.raises(MaskingError, match=r'column "amount": the number strategy works to 60 significant digits') as excinfo:
        bound.apply([(value,)])

    assert str(value)[:12] not in str(excinfo.value)


@pytest.mark.parametrize('options,message', [
    ({'min': 1}, 'both min and max'),
    ({'min': 5, 'max': 5}, 'min below max'),
    ({'min': 1, 'max': 5, 'variance': 0.1}, 'not both'),
    ({'variance': 2}, 'at most 1'),
    ({'variance': 'lots'}, 'must be a number'),
    ])
def test_number_rejects_incoherent_options(options, message):
    with pytest.raises(ValueError, match=message):
        validateColumnPolicy(dict(options, strategy='number'))


# --- dateShift ---------------------------------------------------------------

def test_date_shift_moves_a_date_by_a_bounded_nonzero_number_of_days():
    dates = [datetime.date(1990, 1, 1) + datetime.timedelta(days=offset) for offset in range(500)]
    masked = strategy('dateShift', maxDays=10).maskColumn(dates, 0)

    shifts = {(after - before).days for before, after in zip(dates, masked)}

    assert 0 not in shifts
    assert shifts <= set(range(-10, 11))
    assert len(shifts) > 10


def test_date_shift_keeps_a_timestamps_time_of_day():
    value = datetime.datetime(2026, 3, 4, 13, 14, 15, 161718)
    masked = maskOne('dateShift', value)

    assert isinstance(masked, datetime.datetime)
    assert masked.time() == value.time()


@pytest.mark.parametrize('text', ['2026-03-04', '2026-03-04 13:14:15', '2026-03-04T13:14:15', '2026-03-04 13:14:15.000000', '2026-03-04 13:14'])
def test_date_shift_writes_iso_text_back_in_the_same_shape(text):
    masked = maskOne('dateShift', text)

    assert len(masked) == len(text)
    assert masked[10:] == text[10:]
    assert masked != text


def test_date_shift_moves_everything_on_one_day_to_one_day():
    """It is keyed on the day, so a day's rows stay a day's rows. Keyed on the
    whole value, two timestamps hours apart landed days apart, and a date and a
    timestamp of the same day disagreed about where that day went.
    """
    day = datetime.date(2026, 3, 4)
    values = [day, datetime.datetime(2026, 3, 4, 0, 0), datetime.datetime(2026, 3, 4, 23, 59, 59, 999999),
              '2026-03-04', '2026-03-04 13:14:15', '2026-03-04T13:14:15.500000']

    masked = [maskOne('dateShift', value) for value in values]
    days = {value if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime) else
            (value.date() if isinstance(value, datetime.datetime) else datetime.date.fromisoformat(value[:10]))
            for value in masked}

    assert len(days) == 1
    assert days != {day}


def test_date_shift_rejects_text_that_is_not_a_date_without_echoing_it():
    with pytest.raises(MaskingError) as error:
        maskOne('dateShift', 'Springfield')

    assert 'Springfield' not in str(error.value)


@pytest.mark.parametrize('value', [20260101, 1.5, True, b'2026-01-01'])
def test_date_shift_rejects_what_is_not_a_date_or_text(value):
    with pytest.raises(MaskingError, match='needs a date, a timestamp or ISO 8601 text, got {}'.format(type(value).__name__)):
        maskOne('dateShift', value)


# --- fake --------------------------------------------------------------------

def test_fake_values_come_from_the_bundled_lists():
    first, last = maskOne('fakeName', 'Real Person').split(' ')

    assert first in FIRST_NAMES and last in LAST_NAMES
    assert maskOne('fakeFirstName', 'Real') in FIRST_NAMES


def test_fake_values_respect_max_length():
    assert len(maskOne('fakeStreetAddress', '1 Real Street', maxLength=5)) <= 5


@pytest.mark.parametrize('name', ['fakeCity', 'fakeCompany', 'fakeLastName', 'fakeStreetAddress'])
def test_fake_values_are_deterministic(name):
    assert maskOne(name, 'input') == maskOne(name, 'input')


# --- key ---------------------------------------------------------------------

@pytest.mark.parametrize('values', [range(10), range(10, 100), range(100, 1000)])
def test_key_permutes_each_digit_length_onto_itself(values):
    masked = strategy('key').maskColumn(list(values), 0)

    assert sorted(masked) == list(values)


def test_key_keeps_the_sign_of_an_integer():
    masked = strategy('key').maskColumn(list(range(-500, 500)), 0)

    assert len(set(masked)) == 1000
    assert all((after < 0) == (before < 0) for before, after in zip(range(-500, 500), masked))


def test_key_preserves_an_integral_decimals_type():
    masked = maskOne('key', decimal.Decimal('123456'))

    assert isinstance(masked, decimal.Decimal)
    assert len(str(masked)) == 6


def test_key_rejects_a_fractional_decimal():
    with pytest.raises(MaskingError, match='whole number'):
        maskOne('key', decimal.Decimal('1.5'))


def test_key_is_one_to_one_on_short_codes():
    codes = ['{}{}{}'.format(first, second, third) for first in 'aB' for second in '0123456789' for third in 'xyzXYZ']
    masked = strategy('key').maskColumn(codes, 0)

    assert len(set(masked)) == len(codes)
    for before, after in zip(codes, masked):
        assert [character.isdigit() for character in before] == [character.isdigit() for character in after]
        assert [character.isupper() for character in before] == [character.isupper() for character in after]


def test_key_keeps_characters_it_does_not_mask():
    masked = maskOne('key', 'AB-1234/x')

    assert masked[2] == '-' and masked[7] == '/'


def test_key_with_the_digits_charset_leaves_letters_alone():
    masked = maskOne('key', 'SSN 123-45-6789', charset='digits')

    assert masked.startswith('SSN ')
    assert masked[7] == '-' and masked[10] == '-'


def test_key_with_the_hex_charset_keeps_a_uuid_a_uuid():
    value = str(uuid.uuid4())
    masked = maskOne('key', value, charset='hex')

    assert uuid.UUID(masked)
    assert masked != value


def test_key_keeps_a_uuid_object_a_uuid():
    value = uuid.uuid4()

    assert isinstance(maskOne('key', value), uuid.UUID)


def test_key_masks_a_uuid_object_and_its_text_consistently():
    value = uuid.uuid4()

    assert str(maskOne('key', value)) == maskOne('key', str(value), charset='hex')


def test_key_rejects_bool_and_float():
    for value in (True, 1.5):
        with pytest.raises(MaskingError):
            maskOne('key', value)


def test_key_is_consistent_between_the_two_ends_of_a_foreign_key():
    parents = strategy('key', domain='customer').maskColumn([1001, 1002, 1003], 0)
    children = strategy('key', domain='customer').maskColumn([1003, 1001, 1001], 0)

    assert children == [parents[2], parents[0], parents[0]]


# --- shuffle -----------------------------------------------------------------

def test_shuffle_keeps_the_values_and_is_reproducible_per_chunk():
    values = list(range(50))

    first = strategy('shuffle').maskColumn(values, 0)
    again = strategy('shuffle').maskColumn(values, 0)
    nextChunk = strategy('shuffle').maskColumn(values, 1)

    assert sorted(first) == values
    assert first == again
    assert first != values
    assert first != nextChunk


# --- policies ----------------------------------------------------------------

def test_a_policy_may_be_just_a_strategy_name():
    assert validateColumnPolicy('email') == {'strategy': 'email'}


@pytest.mark.parametrize('policy,message', [
    ('scramble', 'unknown strategy'),
    ({'strategy': ['hash']}, 'unknown strategy'),
    ({'strategy': 'hash', 'size': 3}, 'does not take option'),
    ({'strategy': 'hash', 'domain': ''}, 'domain'),
    (42, 'strategy name or a mapping'),
    ({'strategy': 'key', 'charset': 'emoji'}, 'must be one of'),
    ({'strategy': 'dateShift', 'maxDays': 0}, 'between 1'),
    ])
def test_invalid_policies_are_rejected(policy, message):
    with pytest.raises(ValueError, match=message):
        validateColumnPolicy(policy)


def test_a_short_key_is_rejected():
    with pytest.raises(ValueError, match='at least 16'):
        MaskingPlan(key='short', columns={})


def test_binding_fails_on_a_column_the_policy_does_not_cover():
    plan = MaskingPlan(key=KEY, columns={'id': 'keep'})

    with pytest.raises(MaskingError, match='not in the masking policy: ssn'):
        plan.bind(['id', 'ssn'])


def test_binding_fails_on_a_policy_column_the_query_does_not_return():
    plan = MaskingPlan(key=KEY, columns={'id': 'keep', 'emial': 'email'})

    with pytest.raises(MaskingError, match='does not return: emial'):
        plan.bind(['id'])


def test_default_strategy_covers_unlisted_columns():
    bound = MaskingPlan(key=KEY, columns={'id': 'keep'}, defaultStrategy='null').bind(['id', 'notes'])

    assert bound.apply([(1, 'secret')]) == [(1, None)]
    assert [entry.source for entry in bound.manifest] == ['column', 'defaultStrategy']


def test_columns_match_case_insensitively():
    """Oracle reports unquoted identifiers in upper case."""
    bound = MaskingPlan(key=KEY, columns={'id': 'keep', 'email': 'email'}).bind(['ID', 'EMAIL'])

    assert bound.apply([(1, 'a@b.com')])[0][1].endswith('@example.test')


def test_a_policy_naming_one_column_twice_in_different_case_is_rejected():
    with pytest.raises(ValueError, match='differ only in case'):
        MaskingPlan(key=KEY, columns={'email': 'email', 'EMAIL': 'keep'})


def test_the_domain_defaults_to_the_lower_cased_column_name():
    bound = MaskingPlan(key=KEY, columns={'Email': 'email', 'id': 'keep'}).bind(['Email', 'id'])

    assert [entry.domain for entry in bound.manifest] == ['email', None]


def test_a_masking_error_names_the_column_but_not_the_value():
    bound = MaskingPlan(key=KEY, columns={'amount': 'number'}).bind(['amount'])

    with pytest.raises(MaskingError) as error:
        bound.apply([('4111111111111111',)])

    assert 'amount' in str(error.value)
    assert '4111111111111111' not in str(error.value)


def test_an_all_keep_policy_returns_rows_unchanged():
    bound = MaskingPlan(key=KEY, columns={'a': 'keep', 'b': 'keep'}).bind(['a', 'b'])

    assert bound.apply([(1, 2)]) == [(1, 2)]


# --- configuration -----------------------------------------------------------

def _jobsFile(masking):
    return {'workers': 1, 'jobs': {'job': dataJobFields(masking=masking)}}


def test_a_masked_job_validates_and_normalizes_its_policy():
    jobsFile = Configuration.validateJobConfiguration(_jobsFile({'key': KEY, 'columns': {'id': 'keep', 'total': {'strategy': 'number', 'min': 1, 'max': 9}}}), DataJobsFile)

    masking = jobsFile.jobs['job'].masking
    assert masking.columns['id'] == {'strategy': 'keep'}
    assert masking.columns['total']['min'] == decimal.Decimal(1)


def test_the_key_never_appears_in_a_repr_or_a_validation_error():
    jobsFile = Configuration.validateJobConfiguration(_jobsFile({'key': KEY, 'columns': {'id': 'keep'}}), DataJobsFile)

    assert KEY not in repr(jobsFile)

    with pytest.raises(ConfigurationError) as error:
        Configuration.validateJobConfiguration(_jobsFile({'key': 'tooShortSecret', 'columns': {'id': 'bogus'}}), DataJobsFile)

    assert 'tooShortSecret' not in str(error.value)
    assert 'at least 16' in str(error.value)
    assert 'unknown strategy' in str(error.value)


# --- manifest ----------------------------------------------------------------

def test_the_manifest_records_completed_failed_and_skipped_masked_jobs():
    declared = {name: {'targetTable': name, 'keyFingerprint': keyFingerprint(KEY)} for name in ('done', 'broken', 'waiting')}
    applied = {'columns': [{'column': 'id', 'strategy': 'key', 'domain': 'customer', 'source': 'column'}]}
    outcomes = [
        JobOutcome(job='done', status=JobStatus.COMPLETED, rowCount=5, masking=applied),
        JobOutcome(job='broken', status=JobStatus.FAILED),
        JobOutcome(job='waiting', status=JobStatus.SKIPPED),
        JobOutcome(job='unmasked', status=JobStatus.COMPLETED, rowCount=9),
        ]

    manifest = buildMaskingManifest(outcomes, declared, generatedAt=datetime.datetime(2026, 9, 16, tzinfo=datetime.timezone.utc))

    assert manifest['generatedAt'] == '2026-09-16T00:00:00+00:00'
    assert [(job['job'], job['status'], job['rowCount']) for job in manifest['jobs']] == [('done', 'completed', 5), ('broken', 'failed', 0), ('waiting', 'skipped', 0)]
    assert manifest['jobs'][0]['columns'] == applied['columns']
    assert manifest['jobs'][1]['columns'] == []
    assert KEY not in str(manifest)


def _manifest():
    return {'generatedAt': '2026-09-16T00:00:00+00:00', 'jobs': [{'job': 'maskCustomers', 'status': 'completed', 'rowCount': 3,
                                                                 'columns': [{'column': 'email', 'strategy': 'email'}]}]}


def test_a_sealed_manifest_verifies_until_it_is_changed():
    import json
    from bauta.masking import sealManifest, verifyManifest

    sealed = json.loads(json.dumps(sealManifest(_manifest()), indent=4))

    assert verifyManifest(sealed) == (True, False, False, None)

    sealed['jobs'][0]['rowCount'] = 4
    assert verifyManifest(sealed).digestValid is False


def test_a_signed_manifest_verifies_only_with_its_key():
    from bauta.masking import keyFingerprint, sealManifest, verifyManifest

    key = 'a-manifest-signing-key'
    sealed = sealManifest(_manifest(), signingKey=key)

    assert sealed['integrity']['signingKeyFingerprint'] == keyFingerprint(key)
    assert verifyManifest(sealed, signingKey=key) == (True, True, True, keyFingerprint(key))
    assert verifyManifest(sealed, signingKey='another-signing-key').signatureValid is False
    assert verifyManifest(sealed).signatureValid is False


def test_a_forged_signature_does_not_verify():
    """Anyone can recompute a digest after editing; only the key can re-sign."""
    import hashlib
    from bauta.masking.core import _canonicalManifest
    from bauta.masking import sealManifest, verifyManifest

    key = 'a-manifest-signing-key'
    sealed = sealManifest(_manifest(), signingKey=key)
    sealed['jobs'][0]['status'] = 'failed'
    sealed['integrity']['digest'] = hashlib.sha256(_canonicalManifest(sealed)).hexdigest()

    verification = verifyManifest(sealed, signingKey=key)

    assert verification.digestValid is True
    assert verification.signatureValid is False


def test_a_manifest_without_an_integrity_section_cannot_be_verified():
    from bauta.masking import verifyManifest

    with pytest.raises(ValueError, match='no integrity section'):
        verifyManifest(_manifest())


def test_a_signing_key_must_be_long_enough():
    from bauta.masking import sealManifest

    with pytest.raises(ValueError, match='at least 16'):
        sealManifest(_manifest(), signingKey='short')


GOLDEN_KEY = 'a-golden-value-masking-key'
FAKE_STRATEGIES = ['fakeFirstName', 'fakeLastName', 'fakeName', 'fakeCity', 'fakeCompany', 'fakeStreetAddress']


def test_fake_values_without_a_locale_are_unchanged_by_locale_support():
    """Captured before locales existed. A mask that changes between versions
    breaks every copy already loaded with it.
    """
    bound = MaskingPlan(GOLDEN_KEY, {name: name for name in FAKE_STRATEGIES}).bind(FAKE_STRATEGIES)

    assert bound.apply([('ann@example.test',) * 6]) == [('Elena', 'Singh', 'Quinn Becker', 'Elmstead', 'Acorn Holdings', '1137 Spring Avenue')]


@pytest.mark.parametrize('locale', ['de_DE', 'fr_FR', 'es_ES', 'pt_BR', 'it_IT', 'nl_NL', 'en_US', 'en_GB'])
def test_a_locale_draws_from_its_own_lists(locale):
    from bauta.masking import LOCALES

    policy = {name: {'strategy': name, 'locale': locale} for name in FAKE_STRATEGIES}
    bound = MaskingPlan(GOLDEN_KEY, policy).bind(FAKE_STRATEGIES)
    lists = LOCALES[locale]

    for value in ('ann@example.test', 'bo@example.test', 42):
        first, last, full, city, company, address = bound.apply([(value,) * 6])[0]
        assert first in lists.firstNames and last in lists.lastNames and city in lists.cities
        assert any(full == '{} {}'.format(a, b) for a in lists.firstNames for b in lists.lastNames)
        assert any(company.endswith(' ' + suffix) for suffix in lists.companySuffixes)
        assert any(street in address for street in lists.streets) and any(character.isdigit() for character in address)


def test_locale_address_layouts_differ():
    policy = {'de': {'strategy': 'fakeStreetAddress', 'locale': 'de_DE'}, 'fr': {'strategy': 'fakeStreetAddress', 'locale': 'fr_FR'}}
    german, french = MaskingPlan(GOLDEN_KEY, policy).bind(['de', 'fr']).apply([('x', 'x')])[0]

    assert german.split()[-1].isdigit()
    assert french.split()[0].isdigit()


def test_an_unknown_locale_is_refused():
    with pytest.raises(ValueError, match='locale: must be one of'):
        validateColumnPolicy({'strategy': 'fakeName', 'locale': 'xx_XX'})


def _fpe(policy, values):
    pytest.importorskip('cryptography')
    return [row[0] for row in MaskingPlan(GOLDEN_KEY, {'c': policy}).bind(['c']).apply([(value,) for value in values])]


def test_fpe_is_one_to_one_and_keeps_an_integers_digit_count():
    values = list(range(999_000, 1_001_000)) + list(range(-1_000_500, -999_500))
    masked = _fpe('fpe', values)

    assert len(set(masked)) == len(values)
    assert all(len(str(abs(a))) == len(str(abs(b))) and (a < 0) == (b < 0) for a, b in zip(values, masked))


def test_fpe_masks_values_too_short_for_ff1_one_to_one_too():
    values = list(range(-999, 1000))
    masked = _fpe('fpe', values)

    assert len(set(masked)) == len(values)
    assert all(len(str(abs(a))) == len(str(abs(b))) for a, b in zip(values, masked))


def test_fpe_keeps_a_texts_shape():
    phone, code, token = _fpe({'strategy': 'fpe', 'charset': 'digits'}, ['+1 (555) 010-9999']) + _fpe('fpe', ['AB-12cd9']) + \
        _fpe({'strategy': 'fpe', 'charset': 'hex'}, ['DEADBEEF-00'])

    assert re.fullmatch(r'\+\d \(\d{3}\) \d{3}-\d{4}', phone) and phone != '+1 (555) 010-9999'
    assert re.fullmatch(r'[0-9A-Za-z]{2}-[0-9A-Za-z]{5}', code) and code != 'AB-12cd9'
    # hex is case-sensitive, and fpe masks both cases within one alphabet,
    # so a masked value may mix them.
    assert re.fullmatch(r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{2}', token) and token != 'DEADBEEF-00'


def test_fpe_keeps_a_uuid_a_uuid():
    value = uuid.UUID('12345678-1234-5678-1234-567812345678')
    (masked,) = _fpe('fpe', [value])

    assert isinstance(masked, uuid.UUID) and masked != value


def test_fpe_is_deterministic_and_depends_on_the_domain():
    pytest.importorskip('cryptography')
    plan = MaskingPlan(GOLDEN_KEY, {'a': {'strategy': 'fpe', 'domain': 'customer'}, 'b': {'strategy': 'fpe', 'domain': 'customer'},
                                    'c': {'strategy': 'fpe', 'domain': 'order'}})
    a, b, c = plan.bind(['a', 'b', 'c']).apply([(1234567, 1234567, 1234567)])[0]

    assert a == b != c


@pytest.mark.parametrize('value', [True, 1.5, decimal.Decimal('1.5'), datetime.date(2026, 1, 1)])
def test_fpe_refuses_what_it_cannot_shape(value):
    with pytest.raises(MaskingError, match='fpe'):
        _fpe('fpe', [value])


def test_a_custom_strategy_is_named_by_module_and_class():
    bound = MaskingPlan(GOLDEN_KEY, {'name': {'strategy': 'tests.masking.customStrategies:Initials', 'separator': '-'}}).bind(['name'])

    (masked,) = bound.apply([('Ann Lee',)])[0]

    assert re.fullmatch(r'A-L-[0-9a-f]{4}', masked)
    assert bound.manifest[0].strategy == 'tests.masking.customStrategies:Initials'
    assert bound.manifest[0].domain == 'name'


@pytest.mark.parametrize('reference,message', [
    ('tests.masking.customStrategies:Missing', 'could not be imported'),
    ('tests.noSuchModule:Initials', 'could not be imported'),
    ('tests.masking.customStrategies:NotAStrategy', 'is not a subclass'),
    ])
def test_a_bad_custom_strategy_reference_is_refused(reference, message):
    with pytest.raises(ValueError, match=message):
        validateColumnPolicy(reference)


def test_a_custom_strategy_checks_its_own_options():
    with pytest.raises(ValueError, match='strategy "tests.masking.customStrategies:Initials" does not take option'):
        validateColumnPolicy({'strategy': 'tests.masking.customStrategies:Initials', 'colour': 'red'})


def test_strict_fpe_refuses_a_value_too_short_for_ff1():
    strict = {'strategy': 'fpe', 'strict': True}

    assert len(str(_fpe(strict, [1234567])[0])) == 7

    with pytest.raises(MaskingError, match='strict, and FF1 needs at least 6 digits') as excinfo:
        _fpe(strict, [12345])
    assert '12345' not in str(excinfo.value)

    with pytest.raises(MaskingError, match='at least 4 alphanumeric characters'):
        _fpe(strict, ['ab-1'])


def test_strict_must_be_a_boolean():
    with pytest.raises(ValueError, match='strict: must be true or false'):
        validateColumnPolicy({'strategy': 'fpe', 'strict': 'yes'})


SAMPLE_TEXT = ('Called Ann at +1 (555) 010-9999, email Ann.Lee@corp.example.com; card 4111 1111 1111 1111 '
               'SSN 123-45-6789 IBAN GB82 WEST 1234 5698 7654 32 from 192.168.1.20. '
               'Order 2026-01-02 or 02/01/2026, qty 12, v1.2.3.')


def _redact(policy, values):
    return [row[0] for row in MaskingPlan(GOLDEN_KEY, {'notes': policy}).bind(['notes']).apply([(value,) for value in values])]


def test_redact_labels_each_identifier_and_keeps_the_rest():
    (redacted,) = _redact('redact', [SAMPLE_TEXT])

    assert redacted == ('Called Ann at [PHONE], email [EMAIL]; card [CARD] SSN [SSN] IBAN [IBAN] from [IP]. '
                        'Order 2026-01-02 or 02/01/2026, qty 12, v1.2.3.')


def test_redact_mask_mode_writes_consistent_values_of_the_same_shape():
    first, second = _redact({'strategy': 'redact', 'replacement': 'mask'}, [SAMPLE_TEXT, 'reach me at ann.lee@CORP.example.com'])

    for secret in ('555', '010-9999', 'Ann.Lee', '4111 1111 1111 1111', '123-45-6789', '1234 5698', '192.168'):
        assert secret not in first
    assert re.search(r'\+\d \(\d{3}\) \d{3}-\d{4}', first)
    assert re.search(r'card \d{4} \d{4} \d{4} 1111 ', first)
    assert re.search(r'SSN \d{3}-\d{2}-\d{4} ', first)
    assert re.search(r'IBAN GB[0-9A-Z]{2} [0-9A-Z]{4} ', first)
    assert re.search(r'from 10\.\d+\.\d+\.\d+\.', first)
    assert first.split('email ')[1].split(';')[0] == second.split('at ')[1]
    assert 'Order 2026-01-02 or 02/01/2026, qty 12, v1.2.3.' in first


@pytest.mark.parametrize('text', [
    'card 4111 1111 1111 1112',     # fails the Luhn check
    'IBAN GB82 WEST 1234 5698 7654 33',  # fails the mod-97 check
    'address 999.1.1.1',
    'build 20260102',
    'call 12345',
    ])
def test_redact_leaves_lookalikes_that_fail_their_checks(text):
    (redacted,) = _redact({'strategy': 'redact', 'detect': ['card', 'iban', 'ip', 'ssn', 'email']}, [text])

    assert redacted == text


def test_redact_can_be_narrowed_and_extended():
    (redacted,) = _redact({'strategy': 'redact', 'detect': ['email'], 'patterns': [r'ACC-\d{6}']},
                          ['ann@example.com called about ACC-123456 from +1 555 010 9999'])

    assert redacted == '[EMAIL] called about [REDACTED] from +1 555 010 9999'


@pytest.mark.parametrize('policy,message', [
    ({'strategy': 'redact', 'detect': ['names']}, 'detect: must be a non-empty list of'),
    ({'strategy': 'redact', 'patterns': ['(unclosed']}, 'is not a valid regular expression'),
    ({'strategy': 'redact', 'replacement': 'blank'}, 'replacement: must be one of'),
    ])
def test_redact_options_are_checked(policy, message):
    with pytest.raises(ValueError, match=message):
        validateColumnPolicy(policy)


def test_redact_needs_text():
    with pytest.raises(MaskingError, match='redact strategy needs text, got int'):
        _redact('redact', [5])


# --- remembered masks -------------------------------------------------------

CACHED_CASES = [
    ('hash', {}, ['a', 'b', 'a', 7, 7, 'x' * 300, 'x' * 300]),
    ('email', {}, ['Ann@Corp.com', 'ann@corp.com', 'Ann@Corp.com']),
    ('digits', {'keepTrailing': 2}, ['+1 555 010 9999', 5550109999, '+1 555 010 9999', -424242, -424242]),
    ('fakeName', {'maxLength': 8}, ['ann', 'bob', 'ann', 3, 3]),
    ('key', {}, [41, 42, 41, 'AB-12', 'AB-12', uuid.UUID(int=5), uuid.UUID(int=5), decimal.Decimal('41'), decimal.Decimal('41.0')]),
    ('fpe', {}, [1234567, 1234567, 'AB12-CD34', 'AB12-CD34', 12, 12]),
    ]


@pytest.mark.parametrize('name,options,values', CACHED_CASES, ids=[case[0] for case in CACHED_CASES])
def test_remembered_masks_are_the_masks_themselves(name, options, values):
    if name == 'fpe':
        pytest.importorskip('cryptography')
    remembered = strategy(name, **options)
    fresh = [strategy(name, **options).mask(value) for value in values]

    first = remembered.maskColumn(values, 0)
    second = remembered.maskColumn(values, 1)

    assert first == second == fresh
    assert [type(value) for value in first] == [type(value) for value in fresh]


def test_only_values_whose_equals_always_mask_alike_are_remembered():
    remembered = pythonStrategy('hash')

    remembered.maskColumn([41, True, decimal.Decimal('41'), 41.0, datetime.date(2020, 1, 1), 'x' * 257, 'short', uuid.UUID(int=1), None], 0)

    assert sorted((kind.__name__, value) for kind, value in remembered._cache) == [('UUID', uuid.UUID(int=1)), ('int', 41), ('str', 'short')]


def test_the_cache_is_bounded(monkeypatch):
    import bauta.masking.core as masking

    monkeypatch.setattr(masking, 'MASK_CACHE_SIZE', 10)
    remembered = pythonStrategy('hash')

    for start in range(0, 100, 7):
        assert remembered.maskColumn(list(range(start, start + 7)), 0) == [maskOne('hash', value) for value in range(start, start + 7)]
        assert len(remembered._cache) <= 10


def test_a_value_that_fails_fails_every_time():
    remembered = strategy('email')

    for _ in range(2):
        with pytest.raises(MaskingError):
            remembered.maskColumn([12345], 0)
    assert remembered._cache == {}


def test_a_custom_strategy_is_not_assumed_to_be_cacheable():
    from bauta.masking import resolveStrategy

    custom = resolveStrategy('customStrategies:Initials')
    instance = custom(KeyedHash(KEY, 'name'), {})

    assert custom.CACHEABLE is False
    assert instance.maskColumn(['Ann Lee', 'Ann Lee'], 0)[0] == instance.maskColumn(['Ann Lee'], 0)[0]
    assert instance._cache == {}


def test_strategies_that_depend_on_more_than_the_value_are_not_cached():
    assert not any(STRATEGIES[name].CACHEABLE for name in ('shuffle', 'number', 'dateShift', 'redact', 'keep', 'null', 'constant'))


# --- text beyond ASCII ------------------------------------------------------

NON_LATIN = ['Дмитрий Иванов', '王伟', 'محمد', 'José', '１２３４５６', '٣٤٥٦٧٨٩', 'x²']


@pytest.mark.parametrize('name', ['key', 'fpe'])
@pytest.mark.parametrize('value', NON_LATIN)
def test_key_and_fpe_refuse_letters_and_digits_they_cannot_mask(name, value):
    """They mask ASCII only, and used to copy anything else as it was while
    the manifest said the column was masked.
    """
    if name == 'fpe':
        pytest.importorskip('cryptography')

    with pytest.raises(MaskingError, match='another script') as raised:
        maskOne(name, value)
    assert value not in str(raised.value)


@pytest.mark.parametrize('charset', ['digits', 'hex'])
def test_key_with_a_digit_charset_refuses_only_other_scripts_digits(charset):
    assert maskOne('key', 'Дмитрий-12', charset=charset).startswith('Дмитрий-')

    with pytest.raises(MaskingError, match='digits in another script'):
        maskOne('key', 'AB-١٢', charset=charset)


def test_key_and_fpe_still_keep_non_letters_outside_ascii():
    assert maskOne('key', 'ab–12 €')[2:3] == '–' and maskOne('key', 'ab–12 €').endswith(' €')


@pytest.mark.parametrize('value', ['１２３４５６', '٣٤٥-٦٧-٨٩٠١', '+٩٦٦ ٥٠ ١٢٣ ٤٥٦٧', '電話 ０３-１２３４-５６７８'])
def test_digits_masks_digits_in_any_script_and_keeps_the_script(value):
    masked = maskOne('digits', value)

    assert masked != value and len(masked) == len(value)
    for before, after in zip(value, masked):
        if unicodedata.decimal(before, None) is None:
            assert after == before
        else:
            assert unicodedata.decimal(after) is not None and ord(after) - unicodedata.decimal(after) == ord(before) - unicodedata.decimal(before)


def test_digits_masks_a_number_alike_whichever_digits_it_is_written_in():
    ascii, arabicIndic = maskOne('digits', '555-0199'), maskOne('digits', '٥٥٥-٠١٩٩')

    assert ''.join(str(unicodedata.decimal(character)) if character.isdigit() else character for character in arabicIndic) == ascii


def test_digits_refuses_digit_characters_it_cannot_write_back():
    with pytest.raises(MaskingError, match='superscript or circled'):
        maskOne('digits', 'call ①②③')


@pytest.mark.parametrize('text,kind', [
    ('card ４１１１ １１１１ １１１１ １１１１', 'card'), ('ssn ١٢٣-٤٥-٦٧٨٩', 'ssn'), ('call +٩٦٦ ٥٠ ١٢٣ ٤٥٦٧', 'phone'),
    ('ip ١٩٢.١٦٨.١.١', 'ip'), ('iban DE٨٩٣٧٠٤٠٠٤٤٠٥٣٢٠١٣٠٠٠', 'iban'),
    ])
def test_redact_masks_identifiers_written_in_other_digits(text, kind):
    label = maskOne('redact', text)
    masked = maskOne('redact', text, replacement='mask')

    assert '[{}]'.format(kind.upper()) in label
    assert masked != text
    ascii = ''.join(str(unicodedata.decimal(character)) if unicodedata.decimal(character, None) is not None else character for character in text)
    if kind in ('ip', 'iban'):
        assert maskOne('redact', ascii, replacement='mask').split()[-1] == ''.join(
            str(unicodedata.decimal(character)) if unicodedata.decimal(character, None) is not None else character for character in masked.split()[-1])


# --- how big a value may be -------------------------------------------------

@pytest.mark.parametrize('name', ['key', 'fpe'])
def test_key_and_fpe_refuse_values_longer_than_an_identifier(name):
    if name == 'fpe':
        pytest.importorskip('cryptography')
    from bauta.masking import MAXIMUM_KEY_LENGTH

    assert len(maskOne(name, 'a1' * (MAXIMUM_KEY_LENGTH // 2))) == MAXIMUM_KEY_LENGTH
    assert maskOne(name, 10 ** (MAXIMUM_KEY_LENGTH - 1)) >= 10 ** (MAXIMUM_KEY_LENGTH - 1)
    for value in ('a' * (MAXIMUM_KEY_LENGTH + 1), 10 ** MAXIMUM_KEY_LENGTH, -10 ** 5000):
        with pytest.raises(MaskingError, match='identifiers of up to'):
            maskOne(name, value)


def test_redact_takes_time_in_proportion_to_the_text():
    import time

    def seconds(count):
        # The fastest of three: one run a busy machine stalls says nothing of
        # redact, and a CI runner once took three times as long for one.
        text = ' '.join('u{}@corp.com call 555-010-{:04d}'.format(index, index % 10000) for index in range(count))
        fastest = float('inf')
        for _ in range(3):
            started = time.perf_counter()
            maskOne('redact', text)
            fastest = min(fastest, time.perf_counter() - started)
        return fastest

    seconds(100)
    # 16 times the text in about 16 times as long; quadratic would be 256.
    assert seconds(16000) < 40 * max(seconds(1000), 0.001)


def test_redact_keeps_earlier_detectors_winning_where_matches_overlap():
    assert maskOne('redact', 'card 4111 1111 1111 1111 and 555-010-9999') == 'card [CARD] and [PHONE]'


# --- dateShift at the calendar's ends, number near zero ----------------------

@pytest.mark.parametrize('value', [datetime.date.max, datetime.date.min, datetime.datetime(9999, 12, 31, 23, 59, 59),
                                   datetime.datetime(1, 1, 1, tzinfo=datetime.timezone.utc), '9999-12-31', '0001-01-01 00:00:00'])
def test_date_shift_keeps_the_calendars_ends(value):
    assert maskOne('dateShift', value) == value


@pytest.mark.parametrize('value', [datetime.date(9999, 12, 30), datetime.date(1, 1, 2), datetime.datetime(9999, 12, 20, 12, 0),
                                   '9999-12-25'])
def test_date_shift_turns_back_rather_than_leave_the_calendar(value):
    for domain in range(30):
        masked = strategy('dateShift', domain=str(domain), maxDays=30).mask(value)
        parsed = datetime.date.fromisoformat(masked) if isinstance(masked, str) else masked
        original = datetime.date.fromisoformat(value) if isinstance(value, str) else value
        assert parsed != original
        assert parsed.toordinal() not in (datetime.date.min.toordinal(), datetime.date.max.toordinal())
        assert abs(parsed.toordinal() - original.toordinal()) <= 30


@pytest.mark.parametrize('value', [1, 2, 5, -3, 10, decimal.Decimal('1.00'), decimal.Decimal('7')])
def test_number_never_gives_a_non_zero_value_back_unchanged(value):
    masks = [strategy('number', domain=str(domain)).mask(value) for domain in range(200)]

    assert value not in masks
    assert len(set(masks)) > 1


def test_number_keeps_zero():
    assert {strategy('number', domain=str(domain)).mask(0) for domain in range(20)} == {0}


def test_hex_is_case_sensitive():
    """Folding the case gave two spellings of one value the same mask, which
    merged rows of a key. `key` keeps each character's case; `fpe` masks both
    cases within one alphabet, as FF1 needs a single one.
    """
    values = ['ab12cd34', 'AB12CD34', 'aB12cd34']

    keyed = [row[0] for row in MaskingPlan(GOLDEN_KEY, {'c': {'strategy': 'key', 'charset': 'hex'}}).bind(['c']).apply(
        [(value,) for value in values])]
    encrypted = _fpe({'strategy': 'fpe', 'charset': 'hex'}, values)

    assert len(set(keyed)) == 3 and len(set(encrypted)) == 3
    assert [''.join('u' if character.isupper() else 'l' if character.islower() else 'd' for character in value)
            for value in keyed] == ['llddlldd', 'uudduudd', 'luddlldd']
    assert all(re.fullmatch(r'[0-9A-Fa-f]{8}', value) for value in keyed + encrypted)


def test_digits_refuses_a_policy_that_would_keep_every_digit():
    """Otherwise '555-0100' comes back as it is while the manifest says it was masked."""
    policy = {'strategy': 'digits', 'keepLeading': 3, 'keepTrailing': 4}

    def mask(value):
        return MaskingPlan(GOLDEN_KEY, {'c': policy}).bind(['c']).apply([(value,)])[0][0]

    assert mask('+1 (555) 010-9999') != '+1 (555) 010-9999'

    with pytest.raises(MaskingError, match='keepLeading 3 and keepTrailing 4 cover all 7 of them'):
        mask('555-0100')


# The passthrough flag, and the splice it enables -------------------------------

def test_only_keep_declares_itself_passthrough():
    """PASSTHROUGH is what lets BoundMasking carry a column through untouched,
    so a strategy that rewrites anything must not claim it. `null` and
    `constant` ignore their input but still write every row.
    """
    from bauta.masking.strategies import STRATEGIES as BUILTIN

    passthrough = sorted(name for name, strategyType in BUILTIN.items() if strategyType.PASSTHROUGH)

    assert passthrough == ['keep']


def test_masking_a_mix_of_kept_and_masked_columns_matches_masking_every_column():
    """The spliced path and the all-columns path must agree value for value:
    only which columns are read and rewritten differs.
    """
    columns = ['id', 'name', 'note', 'email', 'city']
    rows = [(index, 'name{}'.format(index), 'note{}'.format(index), 'user{}@example.com'.format(index), 'city{}'.format(index))
            for index in range(50)]

    policy = {'id': 'keep', 'name': 'hash', 'note': 'keep', 'email': 'email', 'city': 'keep'}
    spliced = MaskingPlan(key=KEY, columns=policy).bind(columns).apply(rows, chunkIndex=0)

    everyColumn = MaskingPlan(key=KEY, columns=dict(policy, id='hash', note='hash', city='hash')).bind(columns).apply(rows, chunkIndex=0)

    # The masked columns agree; the kept ones are the originals.
    assert [row[1] for row in spliced] == [row[1] for row in everyColumn]
    assert [row[3] for row in spliced] == [row[3] for row in everyColumn]
    assert [(row[0], row[2], row[4]) for row in spliced] == [(row[0], row[2], row[4]) for row in rows]


def test_kept_columns_carry_through_as_the_objects_that_were_read():
    columns = ['id', 'note']
    rows = [(1, 'a note'), (2, 'another')]

    masked = MaskingPlan(key=KEY, columns={'id': 'hash', 'note': 'keep'}).bind(columns).apply(rows, chunkIndex=0)

    assert masked[0][1] is rows[0][1]


def test_every_column_kept_returns_the_rows_unchanged():
    columns = ['id', 'note']
    rows = [(1, 'a'), (2, 'b')]

    masked = MaskingPlan(key=KEY, columns={'id': 'keep', 'note': 'keep'}).bind(columns).apply(rows, chunkIndex=0)

    assert masked == rows
    assert masked[0] is rows[0]


def test_shuffle_beside_kept_columns_still_keys_on_the_chunk():
    """shuffle is the one strategy whose result depends on the chunk's position
    rather than the value, so the spliced path must pass it through unchanged.
    """
    columns = ['id', 'amount']
    rows = [(index, index * 10) for index in range(40)]
    plan = MaskingPlan(key=KEY, columns={'id': 'keep', 'amount': 'shuffle'})

    first = plan.bind(columns).apply(rows, chunkIndex=0)
    again = plan.bind(columns).apply(rows, chunkIndex=0)
    other = plan.bind(columns).apply(rows, chunkIndex=1)

    assert [row[1] for row in first] == [row[1] for row in again]
    assert [row[1] for row in first] != [row[1] for row in other]
    assert sorted(row[1] for row in first) == sorted(row[1] for row in rows)
    assert [row[0] for row in first] == [row[0] for row in rows]


def test_a_masking_error_names_its_column_on_the_spliced_path():
    columns = ['id', 'when']
    rows = [(1, 'not a date')]

    with pytest.raises(MaskingError, match='column "when"'):
        MaskingPlan(key=KEY, columns={'id': 'keep', 'when': 'dateShift'}).bind(columns).apply(rows, chunkIndex=0)


def test_rows_that_do_not_match_the_bound_columns_are_refused():
    """A bound plan applies to the rows of the query it was bound to. This used
    to raise IndexError, or silently ignore the extra columns.
    """
    bound = MaskingPlan(key=KEY, columns={'id': 'hash', 'name': 'hash'}).bind(['id', 'name'])

    with pytest.raises(MaskingError, match='covers 2 column'):
        bound.apply([(1,)], chunkIndex=0)

    with pytest.raises(MaskingError, match='covers 2 column'):
        bound.apply([(1, 'a', 'extra')], chunkIndex=0)


# dateShift's per-day cache ------------------------------------------------------

def test_date_shift_gives_one_day_one_shift_however_many_rows_share_it():
    """The shift is derived once per day and reused. Same answer, whether the
    value is a date, a timestamp on that day, or the day as ISO text.
    """
    day = datetime.date(2026, 3, 17)
    shifter = strategy('dateShift')

    shiftedDate = shifter.mask(day)
    offset = shiftedDate.toordinal() - day.toordinal()

    assert shifter.mask(datetime.datetime(2026, 3, 17, 9, 30)).date().toordinal() - day.toordinal() == offset
    assert datetime.date.fromisoformat(shifter.mask(day.isoformat())).toordinal() - day.toordinal() == offset
    # Reused across many rows of the same day without changing.
    assert all(shifter.mask(day) == shiftedDate for _ in range(100))


def test_date_shift_caches_per_day_without_growing_without_bound():
    from bauta.masking import MASK_CACHE_SIZE

    shifter = strategy('dateShift')
    start = datetime.date(1800, 1, 1)

    for offset in range(MASK_CACHE_SIZE + 50):
        shifter.mask(start + datetime.timedelta(days=offset))

    assert len(shifter._offsets) <= MASK_CACHE_SIZE


@pytest.mark.parametrize('digits,valid', [
    ('4111111111111111', True), ('5500005555555559', True), ('378282246310005', True), ('79927398713', True),
    ('4111111111111112', False), ('79927398710', False), ('1234567812345678', False),
    ])
def test_the_luhn_check_digit_decides_what_is_a_card(digits, valid):
    """Discovery flags card numbers and `redact` removes them by this one
    check; they had a copy each, free to drift apart.
    """
    from bauta.generate.builtinDiscovery import isCard
    from bauta.masking.strategies import luhnValid

    assert luhnValid(digits) is valid
    if len(digits) >= 13:
        assert isCard(digits) is valid


def test_both_ways_of_splicing_masked_columns_give_the_same_rows():
    """Rows are rebuilt from columns for a narrow table or one mostly masked,
    and spliced row by row otherwise, whichever is faster; never differently.
    """
    columns = ['c{}'.format(index) for index in range(30)]
    policy = {name: 'keep' for name in columns}
    policy.update(c1='hash', c7='email', c29='hash')
    rows = [tuple(None if (row + column) % 11 == 0 else 'v{}-{}@corp.com'.format(row, column) for column in range(30)) for row in range(50)]
    bound = MaskingPlan(GOLDEN_KEY, policy).bind(columns)

    bound._transposeToSplice = True
    transposed = bound.apply(rows, 0)
    bound._transposeToSplice = False
    spliced = bound.apply(rows, 0)

    assert transposed == spliced
    assert all(type(row) is tuple for row in transposed + spliced)
    assert [row[0] for row in transposed] == [row[0] for row in rows]


@pytest.mark.parametrize('width,masked,transposes', [(20, 1, True), (21, 7, True), (21, 6, False), (60, 20, True), (60, 19, False)])
def test_rows_are_rebuilt_from_columns_where_that_was_measured_faster(width, masked, transposes):
    columns = ['c{}'.format(index) for index in range(width)]
    policy = {name: ('hash' if index < masked else 'keep') for index, name in enumerate(columns)}

    assert MaskingPlan(GOLDEN_KEY, policy).bind(columns)._transposeToSplice is transposes


# --- values a driver returns structured -------------------------------------

def test_redact_masks_inside_a_json_document_and_returns_its_text():
    """psycopg returns jsonb as a dict, which redact used to refuse; the
    document's text is masked, in its own key order, and a JSON column loads it.
    """
    document = {'contact': {'alt_email': 'ann.lee@corp.example.com'}, 'newsletter': True, 'nome': 'José'}

    (masked,) = _redact({'strategy': 'redact', 'replacement': 'mask'}, [document])

    assert 'ann.lee' not in masked and 'corp.example.com' not in masked
    parsed = json.loads(masked)
    assert list(parsed) == ['contact', 'newsletter', 'nome'] and parsed['newsletter'] is True and parsed['nome'] == 'José'
    assert _redact({'strategy': 'redact', 'replacement': 'mask'}, [[document]]) == ['[{}]'.format(masked)]


@pytest.mark.parametrize('address', [ipaddress.ip_address('10.0.1.7'), ipaddress.ip_interface('10.0.1.7/24'),
                                     ipaddress.ip_address('2001:db8::1'), ipaddress.ip_network('192.168.0.0/16')])
def test_an_ip_address_is_masked_as_the_text_it_is_written_as(address):
    assert maskOne('digits', address) == maskOne('digits', str(address))
    assert maskOne('key', address) == maskOne('key', str(address))
    assert _redact({'strategy': 'redact', 'detect': ['ip']}, [address]) == _redact({'strategy': 'redact', 'detect': ['ip']}, [str(address)])


def test_email_and_fpe_take_the_text_of_a_structured_value_too():
    pytest.importorskip('cryptography')

    assert maskOne('email', ['ann@corp.example']) == maskOne('email', '["ann@corp.example"]')
    assert maskOne('fpe', ipaddress.ip_address('10.0.1.7')) == maskOne('fpe', '10.0.1.7')


def test_a_value_of_any_other_type_is_still_refused_without_being_echoed():
    with pytest.raises(MaskingError, match='the redact strategy needs text, got bytes') as error:
        maskOne('redact', b'ann@corp.example')

    assert 'ann' not in str(error.value)


# --- dateShift by a column, and coordinate -----------------------------------------

def test_dateshift_by_the_day_can_put_a_start_after_its_end():
    """What shiftBy is for: each day moves its own way."""
    start = datetime.date(2024, 1, 1)
    rows = [(start + datetime.timedelta(days=offset), start + datetime.timedelta(days=offset + 1)) for offset in range(60)]

    masked = _maskedTogether({'starts': {'strategy': 'dateShift', 'domain': 'd'}, 'ends': {'strategy': 'dateShift', 'domain': 'd'}}, rows)

    assert any(end < begin for begin, end in masked)


def test_dateshift_by_a_column_moves_every_date_of_one_subject_alike():
    start = datetime.date(2024, 1, 1)
    rows = [(patient, start + datetime.timedelta(days=offset), start + datetime.timedelta(days=offset + 3),
             datetime.datetime(2024, 2, 1, 9, 30) + datetime.timedelta(days=offset))
            for patient in (101, 102, 103) for offset in range(0, 40, 7)]
    policy = {'strategy': 'dateShift', 'shiftBy': 'patient_id'}

    masked = _maskedTogether({'patient_id': 'keep', 'admitted': policy, 'discharged': policy, 'seen_at': dict(policy, maxDays=10)}, rows)

    for (patient, admitted, discharged, seen), (_, maskedAdmitted, maskedDischarged, maskedSeen) in zip(rows, masked):
        shift = maskedAdmitted - admitted
        assert shift and maskedDischarged - discharged == shift and maskedDischarged - maskedAdmitted == discharged - admitted
        assert abs((maskedSeen - seen).days) <= 10 and maskedSeen.time() == seen.time()
    shifts = {patient: masked[index][1] - rows[index][1] for index, (patient, *_) in enumerate(rows)}
    assert len(set(shifts.values())) > 1, 'different subjects move differently'


def test_dateshift_by_a_column_agrees_across_tables_in_the_columns_domain():
    def shifted(column, rows):
        return _maskedTogether({'patient_id': 'keep', column: {'strategy': 'dateShift', 'shiftBy': 'patient_id'}}, rows)

    ((_, admitted),) = shifted('admitted', [(7, datetime.date(2024, 1, 1))])
    ((_, billed),) = shifted('billed_on', [(7, '2024-01-01')])

    assert admitted.isoformat() == billed
    plan = MaskingPlan(KEY, {'patient_id': 'keep', 'admitted': {'strategy': 'dateShift', 'shiftBy': 'PATIENT_ID'}}).bind(['patient_id', 'admitted'])
    assert [entry.domain for entry in plan.manifest] == [None, 'patient_id']


def test_dateshift_by_a_null_subject_shifts_by_the_day():
    day = datetime.date(2024, 5, 5)

    ((_, bySubject),) = _maskedTogether({'p': 'keep', 'd': {'strategy': 'dateShift', 'shiftBy': 'p', 'domain': 'x'}}, [(None, day)])
    ((byDay,),) = _maskedTogether({'d': {'strategy': 'dateShift', 'domain': 'x'}}, [(day,)])

    assert bySubject == byDay


@pytest.mark.parametrize('columns,policy,message', [
    (['d'], {'strategy': 'dateShift', 'shiftBy': 'missing'}, 'names missing, which must be another column'),
    (['d'], {'strategy': 'dateShift', 'shiftBy': 'd'}, 'names d, which must be another column'),
    ])
def test_a_context_column_must_be_another_returned_column(columns, policy, message):
    with pytest.raises(MaskingError, match=message):
        MaskingPlan(KEY, {'d': policy}).bind(columns)


def test_a_json_field_cannot_mask_by_a_column_of_the_row():
    with pytest.raises(ValueError, match='shiftBy names a column of the row'):
        validateColumnPolicy({'strategy': 'json', 'fields': {'seen': {'strategy': 'dateShift', 'shiftBy': 'patient_id'}}})


def _metres(before, after, latitude):
    import math
    (lat, lng), (maskedLat, maskedLng) = before, after
    return (float(maskedLat) - float(lat)) * 111320, (float(maskedLng) - float(lng)) * 111320 * math.cos(math.radians(float(latitude)))


@pytest.mark.parametrize('point', [(51.50135, -0.14189), (0.3476, 32.5825), (-33.8688, 151.2093), (64.1466, -21.9426), (0.0, 0.0)])
def test_coordinates_move_by_a_distance_on_the_ground_wherever_they_are(point):
    policies = {'lat': {'strategy': 'coordinate', 'axis': 'latitude', 'meters': 2000},
                'lng': {'strategy': 'coordinate', 'axis': 'longitude', 'meters': 2000, 'latitudeColumn': 'lat'}}

    ((masked),) = _maskedTogether(policies, [point])

    for moved in _metres(point, masked, point[0]):
        assert 999 <= abs(moved) <= 2001


def test_coordinates_keep_their_type_and_scale_and_equal_values_move_alike():
    policies = {'lat': {'strategy': 'coordinate', 'axis': 'latitude'}, 'lng': {'strategy': 'coordinate', 'axis': 'longitude'}}

    (asDecimal, asFloat, again, empty) = _maskedTogether(policies, [(decimal.Decimal('38.722300'), decimal.Decimal('-9.139300')),
                                                                   (38.7223, -9.1393), (38.7223, -9.1393), (None, None)])

    assert all(value.as_tuple().exponent == -6 for value in asDecimal) and asDecimal != (decimal.Decimal('38.722300'), decimal.Decimal('-9.139300'))
    assert asFloat == again and abs(float(asDecimal[0]) - asFloat[0]) < 1e-6
    assert empty == (None, None)


def test_coordinates_stay_on_the_globe():
    policies = {'lat': {'strategy': 'coordinate', 'axis': 'latitude', 'meters': 1000000},
                'lng': {'strategy': 'coordinate', 'axis': 'longitude', 'meters': 1000000}}

    for lat, lng in _maskedTogether(policies, [(89.9 - index * 0.01, 179.99 - index * 0.01) for index in range(50)]):
        assert -90 <= lat <= 90 and -180 <= lng < 180


def test_coordinate_options_are_checked():
    for policy, message in (({'strategy': 'coordinate'}, 'requires option'), ({'strategy': 'coordinate', 'axis': 'up'}, 'must be one of'),
                            ({'strategy': 'coordinate', 'axis': 'latitude', 'latitudeColumn': 'x'}, 'for a longitude only'),
                            ({'strategy': 'coordinate', 'axis': 'latitude', 'meters': 0}, 'between 1')):
        with pytest.raises(ValueError, match=message):
            validateColumnPolicy(policy)
    with pytest.raises(MaskingError, match='needs a number, got str'):
        _maskedTogether({'lat': {'strategy': 'coordinate', 'axis': 'latitude'}}, [('51.5',)])
    with pytest.raises(ValueError, match='between 1'):
        validateColumnPolicy({'strategy': 'coordinate', 'axis': 'latitude', 'scale': 0})


@pytest.mark.parametrize('axis, value', [('latitude', 40712776), ('latitude', 95.0), ('latitude', decimal.Decimal('1E+30')),
                                         ('longitude', -181), ('longitude', 1512093000)])
def test_a_coordinate_outside_its_axis_fails_rather_than_barely_moving(axis, value):
    """A column of microdegrees was moved as if it held degrees: 40712776
    under `meters: 1000` came back 40712775.99, nine tenths of a millimetre
    away, which an integer column then rounded back to the very value. Out of
    range is no position to move, so it fails, naming the column, not the value.
    """
    with pytest.raises(MaskingError) as raised:
        _maskedTogether({'place': {'strategy': 'coordinate', 'axis': axis, 'meters': 1000}}, [(value,)])

    assert 'column "place"' in str(raised.value) and 'set scale' in str(raised.value)
    assert str(value) not in str(raised.value)


def test_scaled_coordinates_move_by_the_distance_and_keep_their_type():
    policies = {'lat': {'strategy': 'coordinate', 'axis': 'latitude', 'meters': 2000, 'scale': 1000000},
                'lng': {'strategy': 'coordinate', 'axis': 'longitude', 'meters': 2000, 'latitudeColumn': 'lat', 'scale': 1000000}}
    point = (51501350, -141890)

    ((masked),) = _maskedTogether(policies, [point])

    assert all(isinstance(value, int) for value in masked)
    for moved in _metres((51.50135, -0.14189), (masked[0] / 1e6, masked[1] / 1e6), 51.50135):
        assert 999 <= abs(moved) <= 2001
    ((decimals),) = _maskedTogether(policies, [(decimal.Decimal('51501350.5'), decimal.Decimal('-141890.5'))])
    assert all(value.as_tuple().exponent == -1 for value in decimals)
    with pytest.raises(MaskingError, match='even divided by its scale'):
        _maskedTogether(policies, [(90000001, 0)])


def test_coordinates_in_range_mask_as_they_did_before_scale_existed():
    """The range check refuses only what was never a position: every value
    in range keeps its mask, as the recorded vectors also check.
    """
    policies = {'lat': {'strategy': 'coordinate', 'axis': 'latitude'}}

    assert _maskedTogether(policies, [(51.50135,)]) == _maskedTogether(policies, [(51.50135,)])
    assert _maskedTogether(policies, [(float('nan'),)])[0][0] != _maskedTogether(policies, [(float('nan'),)])[0][0]


# --- fake names from the larger lists ------------------------------------------------

LARGE = {'lists': 2}


def test_larger_lists_give_far_more_distinct_names():
    people = [('Given{}'.format(index), 'Family{}'.format(index)) for index in range(3000)]

    def distinct(policy):
        masked = _maskedTogether({'first': dict(policy, strategy='fakeFirstName'), 'last': dict(policy, strategy='fakeLastName')}, people)
        return len({first for first, _ in masked}), len({last for _, last in masked})

    assert distinct({}) == (len(FIRST_NAMES), len(LAST_NAMES))
    larger = distinct(LARGE)
    assert larger[0] > 10 * len(FIRST_NAMES) and larger[1] > 10 * len(LAST_NAMES)


def test_a_full_name_agrees_with_its_parts_in_their_own_columns_and_tables():
    policies = {'first_name': dict(LARGE, strategy='fakeFirstName'), 'last_name': dict(LARGE, strategy='fakeLastName'),
                'full_name': dict(LARGE, strategy='fakeName'), 'listed_as': dict(LARGE, strategy='fakeName')}

    ((first, last, full, listed),) = _maskedTogether(policies, [('John', 'Smith', 'John Smith', 'Smith, John')])
    ((elsewhere,),) = _maskedTogether({'contact': dict(LARGE, strategy='fakeName')}, [('john  SMITH',)])

    assert full == '{} {}'.format(first, last) and listed == '{}, {}'.format(last, first)
    assert elsewhere.split()[0].casefold() == first.casefold() and first != 'John'


def test_surname_particles_stay_with_the_surname():
    ((full, last),) = _maskedTogether({'full': dict(LARGE, strategy='fakeName'), 'last': dict(LARGE, strategy='fakeLastName')},
                                      [('Jan van der Berg', 'van der Berg')])

    assert full.endswith(' ' + last) and len(full.split()) == 1 + len(last.split())


def test_case_is_written_back_as_it_came():
    ((upper, lower),) = _maskedTogether({'a': dict(LARGE, strategy='fakeFirstName'), 'b': dict(LARGE, strategy='fakeFirstName')},
                                        [('JOHN', 'john')])

    assert upper.isupper() and lower.islower() and upper.casefold() == lower


def test_match_gender_keeps_a_known_first_name_s_gender():
    from bauta.masking.fakeData import FEMALE_NAMES, MALE_NAMES

    policy = dict(LARGE, strategy='fakeFirstName', matchGender=True)
    women = ['Mary', 'Sophie', 'Lucía', 'Giulia', 'Helena', 'Jade', 'Fenna', 'Hannah']
    men = ['James', 'Lukas', 'Mateo', 'Francesco', 'Miguel', 'Gabriel', 'Daan', 'George']

    masked = _maskedTogether({'a': policy, 'b': policy}, list(zip(women, men)))

    assert all(woman.casefold() in FEMALE_NAMES and man.casefold() in MALE_NAMES for woman, man in masked)


def test_the_larger_lists_are_opt_in_and_python_only():
    for policy, message in (({'strategy': 'fakeFirstName', 'matchGender': True}, 'with lists: 2 only'),
                            ({'strategy': 'fakeCity', 'lists': 3}, 'must be 1 or 2'), ({'strategy': 'fakeCity', 'matchGender': True}, 'does not take')):
        with pytest.raises(ValueError, match=message):
            validateColumnPolicy(policy)
    plan = MaskingPlan(KEY, {'a': dict(LARGE, strategy='fakeName'), 'b': 'fakeName'}).bind(['a', 'b'])
    assert [entry.domain for entry in plan.manifest] == ['fake name', 'b']
    assert plan.strategies[0]._native is None


# --- normalize -----------------------------------------------------------------

def _maskedTogether(policies, rows):
    names = list(policies)
    return MaskingPlan(KEY, policies).bind(names).apply(rows)


@pytest.mark.parametrize('strategy', ['key', 'hash', 'fpe'])
def test_strip_and_lower_mask_values_a_database_compares_as_equal_alike(strategy):
    """A CHAR column pads with spaces, and a case-insensitive collation joins
    'AB12' to 'ab12'; masked as written they'd stop matching.
    """
    policy = {'strategy': strategy, 'domain': 'codes', 'normalize': ['strip', 'lower']}

    masked = _maskedTogether({'code': policy}, [('AB12CD34',), ('ab12cd34   ',), (' Ab12Cd34',)])

    assert len({row[0] for row in masked}) == 1


def test_without_normalize_padding_and_case_change_the_mask():
    masked = _maskedTogether({'code': {'strategy': 'key', 'domain': 'codes'}}, [('AB12',), ('AB12  ',), ('ab12',)])

    assert masked[0][0] != masked[1][0].strip() and masked[0][0] != masked[2][0]


@pytest.mark.parametrize('strategy', ['key', 'fpe'])
def test_integer_masks_an_id_held_as_text_as_it_masks_the_number_and_keeps_it_text(strategy):
    policies = {'asNumber': {'strategy': strategy, 'domain': 'customers'},
                'asText': {'strategy': strategy, 'domain': 'customers', 'normalize': ['integer']}}

    ((number, text), (_, leading), (_, word)) = _maskedTogether(policies, [(1234567, '1234567'), (None, '0012345'), (None, 'C-77')])

    assert isinstance(text, str) and text == str(number)
    # Only text that spells an integer one way: the rest masks as text.
    assert len(leading) == 7 and leading != '0012345' and word[1:2] == '-' and len(word) == 4


def test_normalize_steps_apply_in_one_order_and_are_checked():
    assert validateColumnPolicy({'strategy': 'key', 'normalize': ['integer', 'strip']})['normalize'] == ['strip', 'integer']
    for strategy, steps in (('hash', ['integer']), ('key', []), ('key', ['trim']), ('email', ['strip']), ('key', 'strip')):
        with pytest.raises(ValueError):
            validateColumnPolicy({'strategy': strategy, 'normalize': steps})


def test_normalize_leaves_values_that_are_not_text_alone():
    masked = _maskedTogether({'id': {'strategy': 'key', 'normalize': ['strip', 'lower', 'integer']}}, [(42,), (None,)])

    assert masked[0][0] == _maskedTogether({'id': 'key'}, [(42,)])[0][0] and masked[1][0] is None


# --- json ----------------------------------------------------------------------

def _json(policy, values, key=GOLDEN_KEY, extra=None):
    columns = {'doc': dict(policy, strategy='json'), **(extra or {})}
    names = list(columns)
    return MaskingPlan(key, columns).bind(names).apply([tuple(value) if isinstance(value, tuple) else (value,) for value in values])


def test_json_masks_each_named_field_with_its_own_policy_and_redacts_the_rest():
    document = {'contact': {'alt_email': 'ann@corp.example', 'phone': '+1 555 010 9999'}, 'first_name': 'Ana', 'visits': 3,
                'vip': True, 'tags': [{'note': 'call Ana', 'by': 'bob@corp.example'}], 'gone': None}

    ((masked,),) = _json({'fields': {'contact.alt_email': 'email', 'first_name': 'fakeFirstName', 'tags[].note': 'null'}}, [document])

    assert masked['contact']['alt_email'].endswith('@example.test')
    assert masked['first_name'] not in ('Ana', None)
    assert masked['tags'][0]['note'] is None
    # Not named, so redacted: identifiers found by shape, the rest kept.
    assert masked['contact']['phone'] != '+1 555 010 9999' and masked['tags'][0]['by'].endswith('@example.test')
    assert (masked['visits'], masked['vip'], masked['gone']) == (3, True, None)
    assert list(masked) == list(document)


def test_json_masks_identifiers_held_as_numbers_and_keeps_them_numbers():
    document = {'phone': 5550109999, 'card': 4111111111111111, 'ssn': 123456789, 'visits': 3, 'total': 1234567.5, 'year': 2026,
                'negative': -5550109999, 'vip': True}

    ((masked,),) = _json({'fields': {'visits': 'keep'}}, [document])

    for name in ('phone', 'card', 'ssn', 'negative'):
        assert isinstance(masked[name], int) and masked[name] != document[name]
        assert len(str(abs(masked[name]))) == len(str(abs(document[name])))
    assert str(masked['card']).endswith('1111') and masked['negative'] < 0
    # Too short to be one, or not whole: kept.
    assert (masked['visits'], masked['total'], masked['year'], masked['vip']) == (3, 1234567.5, 2026, True)


def test_a_number_masks_as_the_same_digits_written_as_text():
    ((masked,),) = _json({'fields': {'x': 'null'}}, [{'asNumber': 5550109999, 'asText': '5550109999'}])

    assert str(masked['asNumber']) == masked['asText']


def test_json_labels_a_number_it_redacts_with_labels():
    ((masked,),) = _json({'fields': {'x': 'null'}, 'otherwise': {'strategy': 'redact', 'replacement': 'label'}}, [{'phone': 5550109999}])

    assert masked == {'phone': '[PHONE]'}


def test_json_masks_identifiers_in_object_keys_unless_otherwise_keeps():
    document = {'byEmail': {'ann@corp.example': {'visits': 1}}, 'plain key': 2}

    ((masked,),) = _json({'fields': {'x': 'null'}}, [document])
    ((nulled,),) = _json({'fields': {'x': 'null'}, 'otherwise': 'null'}, [document])
    ((kept,),) = _json({'fields': {'x': 'null'}, 'otherwise': 'keep'}, [document])

    (maskedKey,) = masked['byEmail']
    assert maskedKey.endswith('@example.test') and masked['plain key'] == 2
    assert list(nulled['byEmail']) == [maskedKey]
    assert kept == document


def test_json_matches_field_paths_against_keys_as_they_came():
    ((masked,),) = _json({'fields': {'ann@corp.example.n': 'null'}}, [{'ann@corp.example': {'n': 5}}])

    assert list(masked.values()) == [{'n': None}]


def test_a_json_field_masks_in_the_domain_it_names_so_it_matches_a_column():
    ((document, customer),) = _json({'fields': {'owner.id': {'strategy': 'key', 'domain': 'customers'}}}, [({'owner': {'id': 7}}, 7)],
                                    extra={'id': {'strategy': 'key', 'domain': 'customers'}})

    assert document['owner']['id'] == customer != 7


def test_json_text_comes_back_as_json_text_and_a_named_container_is_masked_whole():
    ((masked,),) = _json({'fields': {'address': 'null'}, 'otherwise': 'keep'}, ['{"address": {"line1": "1 Main St"}, "plan": "gold"}'])

    assert json.loads(masked) == {'address': None, 'plan': 'gold'}


def test_json_refuses_shuffle_and_a_path_it_cannot_read():
    for fields, message in (({'email': 'shuffle'}, 'shuffle moves values between rows'), ({'a..b': 'null'}, 'is not a path'), ({}, 'must map paths')):
        with pytest.raises(ValueError, match=message):
            validateColumnPolicy({'strategy': 'json', 'fields': fields})


def test_json_refuses_what_is_not_a_document_without_echoing_it():
    for value, message in (('ann@corp.example', 'this text is not one'), (42, 'got int')):
        with pytest.raises(MaskingError, match=message) as error:
            _json({'fields': {'a': 'null'}}, [value])
        assert 'ann' not in str(error.value)


def test_json_masks_a_value_by_what_its_key_names_where_no_policy_says_otherwise():
    """Fields a policy didn't name were redacted, which finds identifiers by
    their shape: a name, a date of birth and an address have none, so
    {"name": "Ann Smith", "dob": "1984-03-02", "address": "12 Main St"}
    was copied as it stood.
    """
    document = {'name': 'Ann Smith', 'dob': '1984-03-02', 'address': '12 Main St', 'homeAddresses': ['1 Elm St', '2 Oak Rd'],
                'billing': {'city': 'Springfield', 'postcode': '62704'}, 'company_name': 'Acme Ltd', 'gender': 'F',
                'visits': 3, 'vip': True, 'nickname': ''}

    ((masked,),) = _json({'fields': {'visits': 'keep'}}, [document])

    for path in (('name',), ('dob',), ('address',), ('billing', 'city'), ('billing', 'postcode'), ('company_name',)):
        before, after = document, masked
        for part in path:
            before, after = before[part], after[part]
        assert after != before and isinstance(after, str), path
    assert datetime.date.fromisoformat(masked['dob'])
    assert all(after != before for before, after in zip(document['homeAddresses'], masked['homeAddresses']))
    assert masked['gender'] is None
    assert (masked['visits'], masked['vip'], masked['nickname']) == (3, True, '')


def test_a_json_value_masked_by_its_key_masks_as_a_column_of_that_name():
    ((document, column),) = _json({'fields': {'x': 'null'}}, [({'ssn': '123-45-6789'}, '123-45-6789')], extra={'ssn': {'strategy': 'key'}})

    assert document['ssn'] == column != '123-45-6789'


def test_json_masks_only_with_otherwise_where_it_is_set():
    document = {'name': 'Ann Smith', 'dob': '1984-03-02'}

    ((kept,),) = _json({'fields': {'x': 'null'}, 'otherwise': 'keep'}, [document])
    ((redacted,),) = _json({'fields': {'x': 'null'}, 'otherwise': {'strategy': 'redact'}}, [document])

    assert kept == document and redacted == document


def test_a_json_value_its_key_cannot_mask_fails_naming_the_path_not_the_value():
    with pytest.raises(MaskingError, match='at contact.dob as its key says') as error:
        _json({'fields': {'x': 'null'}}, [{'contact': {'dob': 'sometime in 1984'}}])

    assert '1984' not in str(error.value)


def test_a_json_document_masks_the_same_as_text_as_it_did_parsed():
    """PostgreSQL's json and jsonb now arrive as their text, as MySQL's do,
    where psycopg handed over dicts. The json strategy must mask a document
    the same either way, or every masked jsonb column's masks would change.
    """
    import json

    from bauta.masking import MaskingPlan

    document = {'email': 'ana@corp.example', 'phone': 5550109999, 'score': 0.1, 'tiny': 1e-07, 'tags': ['call 555-010-9999'], 'ok': True}
    plan = MaskingPlan(key='a-json-masking-test-key-0123456789', columns={'doc': {'strategy': 'json', 'fields': {'email': {'strategy': 'email'}}}})

    parsed = plan.bind(['doc']).apply([(document,)])[0][0]
    asText = plan.bind(['doc']).apply([(json.dumps(document),)])[0][0]

    assert json.loads(asText) == parsed


def test_a_json_number_left_unmasked_is_written_as_it_came():
    """Through a float, 12345678901234567890.123 came back as
    12345678901234567000, and 1.10 as 1.1.
    """
    from bauta.masking import MaskingPlan

    plan = MaskingPlan(key='a-json-masking-test-key-0123456789', columns={'doc': {'strategy': 'json', 'fields': {'email': {'strategy': 'email'}}}})
    text = '{"n": 12345678901234567890.123, "p": 1.10, "e": 1.5E+3, "email": "ana@corp.example", "list": [2.50, {"x": -0.0}]}'

    masked = plan.bind(['doc']).apply([(text,)])[0][0]

    assert masked.startswith('{"n": 12345678901234567890.123, "p": 1.10, "e": 1.5E+3, "email": "u')
    assert masked.endswith('"list": [2.50, {"x": -0.0}]}')


# --- ip --------------------------------------------------------------------------------


def test_ip_masks_each_address_to_another_of_its_family_one_to_one():
    """No strategy kept an address valid: digits and key made 589.439.074.458,
    and hash a hex token, which a PostgreSQL inet column refused.
    """
    import ipaddress

    addresses = [str(ipaddress.IPv4Address(number)) for number in range(3232235520, 3232235520 + 3000)] + ['2001:db8::{:x}'.format(index)
                                                                                                         for index in range(300)]
    masked = [value for value, in _maskedTogether({'ip': {'strategy': 'ip'}}, [(address,) for address in addresses])]

    assert len(set(masked)) == len(addresses)
    assert all(ipaddress.ip_address(after).version == ipaddress.ip_address(before).version for before, after in zip(addresses, masked))
    assert masked == [value for value, in _maskedTogether({'ip': {'strategy': 'ip'}}, [(address,) for address in addresses])]


def test_ip_keeps_the_type_a_driver_gave_and_a_prefix_it_names():
    import ipaddress

    values = [ipaddress.ip_address('172.16.254.3'), ipaddress.ip_interface('10.1.2.3/24'), ipaddress.ip_network('10.20.0.0/16'),
              '10.1.2.3/24', b'\x0a\x00\x00\x01', 3232235777]

    masked = [value for value, in _maskedTogether({'ip': {'strategy': 'ip', 'keepPrefix': 16}}, [(value,) for value in values])]

    assert [type(after) for after in masked] == [type(before) for before in values]
    assert str(masked[0]).startswith('172.16.') and masked[0] != values[0]
    assert masked[1].network.prefixlen == 24 and str(masked[1]).startswith('10.1.')
    assert masked[2] == values[2]
    assert masked[3].startswith('10.1.') and masked[3].endswith('/24')
    assert masked[4][:2] == b'\x0a\x00' and masked[5] >> 16 == 3232235777 >> 16


def test_ip_refuses_what_is_not_an_address_without_echoing_it():
    for value, message in (('10.1.2', 'could not read a text value'), (1.5, 'got float'), (b'abc', 'got bytes')):
        with pytest.raises(MaskingError, match=message) as error:
            _maskedTogether({'ip': {'strategy': 'ip'}}, [(value,)])
        assert '10.1.2' not in str(error.value)


def test_a_name_held_as_an_object_is_masked_through_its_parts():
    """{"name": {"first": "Ann", "last": "Lee"}} was copied as it stood: the
    key that named the value was not carried into the object, whose own keys
    name nothing. One it can't mask goes to `otherwise`, not a failure.
    """
    ((masked,),) = _json({'fields': {'x': 'null'}}, [{'name': {'first': 'Ann', 'last': 'Lee'}, 'byEmail': {'ann@corp.example': {'visits': 3}}}])

    assert 'Ann' not in masked['name'].values() and 'Lee' not in masked['name'].values()
    assert list(masked['byEmail'].values()) == [{'visits': 3}]

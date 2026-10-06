"""The native masker against the Python one, over the values that break things.

The extension is optional and its masks must be indistinguishable from Python's:
a difference isn't a wrong answer, it's a silent key change, and it would reach a
deployment as joins that quietly stop matching. mask-rs/vectors/reference.json
pins the Rust side from below; this pins the whole path, through the conversions
and the fallbacks, from above.

Skipped entirely when the extension isn't installed, which is also how the rest
of the suite runs in that configuration.
"""
import datetime
import decimal
import importlib.metadata
import logging
import math
import random
import sys
import types
import uuid

import pytest

from bauta.masking import STRATEGIES, KeyedHash, MaskingError, nativeVersion

native = pytest.mark.skipif(nativeVersion() is None, reason='the bauta_rs extension is not installed')

KEY = 'a-test-key-that-is-long-enough'
ALPHANUMERIC = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'

# Shaped rather than merely plentiful. Random text would essentially never land
# on the boundaries that matter: 2**128 for a UUID domain, the 21-to-22
# character step where a Feistel half stops fitting 128 bits, MAXIMUM_KEY_LENGTH
# at 256, and the byte edges where a half outgrows its serialised width.
def corpus():
    random.seed(20260917)
    values = [None, True, False, 0, 1, -1, 9, 10, -10, 99, -99, 100]

    for length in (1, 2, 3, 5, 6, 7, 11, 16, 19, 20, 21, 22, 31, 32, 33, 40, 63, 64, 65, 255, 256, 257):
        values.append(''.join(random.choice(ALPHANUMERIC) for _ in range(length)))
        values.append(''.join(random.choice('0123456789') for _ in range(length)))
        values.append(''.join(random.choice('0123456789abcdef') for _ in range(length)))
        values.append(''.join(random.choice('0123456789ABCDEF') for _ in range(length)))
        values.append(''.join(random.choice('0123456789aBcDeF') for _ in range(length)))

    for power in (1, 2, 6, 7, 9, 11, 15, 16, 18, 19, 20, 38, 39, 40, 128, 255, 256, 257):
        values.append(10 ** power)
        values.append(10 ** power - 1)
        values.append(-(10 ** power))

    # Either side of where an integer stops crossing into the extension as an
    # i64 and crosses as a BigInt instead, both ways.
    values += [2 ** 63 - 1, 2 ** 63, -(2 ** 63), -(2 ** 63) - 1]

    values += [
        uuid.uuid4(), uuid.UUID(int=0), uuid.UUID(int=(1 << 128) - 1),
        decimal.Decimal('42'), decimal.Decimal('-42'), decimal.Decimal('42.5'), decimal.Decimal('4.2E+3'),
        42.0, 42.5, datetime.date(2020, 2, 29), datetime.datetime(2020, 1, 1, 12, 30),
        'héllo', 'Ωmega', '٣٤٥', 'user@example.com', 'Alice.Smith@Corp.COM ', '  spaced  ',
        'alice@corp.com\x1c', '+1 (555) 010-9999', '', ' ', 'x', b'bytes',
        # What `number` treats specially: signed zeros, and scales a Decimal
        # compares equal across but keeps.
        0.0, -0.0, 1e22, decimal.Decimal('12.30'), decimal.Decimal('-0.00'), decimal.Decimal('1.005'),
        ]

    return values


VALUES = corpus()

COMBINATIONS = [
    ('key', {}), ('key', {'charset': 'hex'}), ('key', {'charset': 'digits'}),
    ('fpe', {}), ('fpe', {'charset': 'hex'}), ('fpe', {'charset': 'digits'}), ('fpe', {'strict': True}),
    ('hash', {}), ('hash', {'length': 12}), ('hash', {'length': 64, 'prefix': 'cust_'}),
    ('email', {}), ('email', {'keepDomain': True}), ('email', {'length': 8}),
    ('email', {'mailDomain': 'masked.invalid'}),
    ('digits', {}), ('digits', {'keepLeading': 2}), ('digits', {'keepTrailing': 4}),
    ('digits', {'keepLeading': 1, 'keepTrailing': 1}),
    ('number', {}), ('number', {'decimals': 2}), ('number', {'min': '-5.5', 'max': '1E+3'}),
    ] + [(name, options) for name in ('fakeFirstName', 'fakeLastName', 'fakeName', 'fakeCity', 'fakeCompany', 'fakeStreetAddress')
         for options in ({}, {'maxLength': 3}, {'locale': 'de_DE'}, {'locale': 'pt_BR'})]


def exactly(masked):
    """A mask as Python writes it: type and repr, so that Decimal('12.30') is
    not Decimal('12.3') and -0.0 is not 0.0, though each pair compares equal."""

    return [(type(value).__name__, repr(value)) for value in masked]


def outcome(built, values):
    """Each value's mask, exactly, or the error it raised -- both have to agree."""

    results = []
    for value in values:
        try:
            results.append(('ok',) + exactly(built.maskColumn([value], 0))[0])
        except MaskingError as error:
            results.append(('error', str(error)))

    return results


@native
@pytest.mark.parametrize('name,options', COMBINATIONS, ids=lambda item: str(item))
def test_native_and_python_agree(name, options):
    validated = STRATEGIES[name].validateOptions(options)

    asNative = STRATEGIES[name](KeyedHash(KEY, 'agree'), validated)
    assert asNative._native is not None, '{} should have a native masker'.format(name)

    asPython = STRATEGIES[name](KeyedHash(KEY, 'agree'), validated)
    asPython._native = None

    fromNative = outcome(asNative, VALUES)
    fromPython = outcome(asPython, VALUES)

    for value, left, right in zip(VALUES, fromNative, fromPython):
        assert left == right, 'native and python disagree on {!r}: {!r} vs {!r}'.format(value, left, right)


@native
@pytest.mark.parametrize('name,options', COMBINATIONS, ids=lambda item: str(item))
def test_a_whole_column_agrees_with_one_value_at_a_time(name, options):
    """Batching must not change an answer: the extension deduplicates within a
    column, so a repeated value has to mask as it would on its own.
    """

    validated = STRATEGIES[name].validateOptions(options)
    built = STRATEGIES[name](KeyedHash(KEY, 'agree'), validated)

    maskable = [value for value in VALUES if outcome(built, [value])[0][0] == 'ok']
    repeated = maskable + maskable

    assert exactly(built.maskColumn(repeated, 0)) == exactly([built.maskColumn([value], 0)[0] for value in repeated])


@native
def test_the_extension_can_be_turned_off(monkeypatch):
    import bauta.masking.core as masking

    monkeypatch.setenv('BAUTA_NATIVE', '0')
    masking._nativeModule.cache_clear()

    try:
        assert masking.nativeVersion() is None
        assert STRATEGIES['key'](KeyedHash(KEY, 'off'), {})._native is None
    finally:
        # Or every later test would find the extension off.
        masking._nativeModule.cache_clear()


@pytest.fixture
def standInExtension(monkeypatch):
    """A module in bauta_rs's place, whatever is installed, and the warnings
    the package logs while it's there. The package's logger doesn't propagate,
    so caplog wouldn't see them.
    """
    import bauta.masking.core as masking

    extension = types.ModuleType('bauta_rs')
    monkeypatch.setitem(sys.modules, 'bauta_rs', extension)
    monkeypatch.delenv('BAUTA_NATIVE', raising=False)

    warnings = []
    handler = logging.Handler(level=logging.WARNING)
    handler.emit = lambda record: warnings.append(record.getMessage())
    logging.getLogger('bauta').addHandler(handler)
    masking._nativeModule.cache_clear()

    yield extension, warnings

    logging.getLogger('bauta').removeHandler(handler)
    masking._nativeModule.cache_clear()


def test_an_extension_of_the_same_version_is_used(standInExtension):
    import bauta.masking.core as masking

    extension, warnings = standInExtension
    extension.__version__ = importlib.metadata.version('bauta')

    assert masking._nativeModule() is extension
    assert warnings == []


@pytest.mark.parametrize('installed', ['0.0.1', None], ids=['another version', 'no version'])
def test_an_extension_of_another_version_is_ignored_with_a_warning(standInExtension, installed):
    """The two install separately, so a mismatched pair is one pip command
    away, and nothing but this check stops it masking differently.
    """
    import bauta.masking.core as masking

    extension, warnings = standInExtension
    if installed is not None:
        extension.__version__ = installed

    assert masking._nativeModule() is None
    assert masking.maskingImplementation() == 'python'
    (warning,) = warnings
    assert 'bauta-rs {} does not match bauta {}'.format(installed, importlib.metadata.version('bauta')) in warning


@native
def test_the_masking_key_never_reaches_the_extension():
    """The extension is built from the per-domain subkey, which is already one
    HMAC away from the key. security.md is deliberate that the key stays out of
    anything that could end up in a repr or a traceback.
    """

    keyedHash = KeyedHash(KEY, 'secrecy')
    built = STRATEGIES['key'](keyedHash, {})

    assert KEY.encode() not in keyedHash.subkey
    assert KEY not in repr(built._native)


# Threads and the cross-chunk cache ---------------------------------------------

def _wideColumn(built):
    """Well past the size a call is split across threads at, with every value
    of the corpus the strategy masks -- the ones Python finishes included --
    and repeats both within and across chunks. Refusals are left to their own
    test: one would fail its whole chunk, and hide the masks beside it.
    """
    random.seed(20260918)
    distinct = ['C{:07d}'.format(number) for number in range(3000)] + list(range(10 ** 9, 10 ** 9 + 1500))
    # For number, which takes these and refuses the text.
    distinct += [number / 8 for number in range(1, 1500)] + [decimal.Decimal(number).scaleb(-2) for number in range(1, 1500)]

    def masks(value):
        return outcome(built, [value])[0][0] == 'ok'

    distinct = [value for value in distinct if masks(value)]
    repeated = [value for value in VALUES if masks(value)] * 3
    # Four whole chunks of 2,000, so no chunk is too small to be split.
    column = [random.choice(distinct) for _ in range(8000 - len(repeated))] + repeated
    random.shuffle(column)

    return column


@pytest.fixture
def maskingThreads():
    """Sets the extension's threads, and puts them back to one after."""
    import bauta_rs

    yield bauta_rs.setThreads
    bauta_rs.setThreads(1)


@native
@pytest.mark.parametrize('name,options', [('key', {}), ('fpe', {}), ('hash', {}), ('digits', {'keepTrailing': 2}), ('email', {}),
                                          ('fakeName', {'locale': 'es_ES'}), ('fakeStreetAddress', {}), ('number', {'decimals': 2})],
                         ids=lambda item: str(item))
def test_threads_and_the_cache_change_no_answer(name, options, maskingThreads):
    """Every mask depends on its value alone, so masking a column on eight
    threads, chunk after chunk with the cache warm, must give exactly what one
    thread and pure Python give -- errors included, in the same place.
    """
    validated = STRATEGIES[name].validateOptions(options)
    column = _wideColumn(STRATEGIES[name](KeyedHash(KEY, 'threads'), validated))
    chunks = [column[start:start + 2000] for start in range(0, len(column), 2000)]

    def maskAll(threads, native=True):
        maskingThreads(threads)
        built = STRATEGIES[name](KeyedHash(KEY, 'threads'), validated)
        if not native:
            built._native = None
        return [_chunkOutcome(built, chunk, index) for index, chunk in enumerate(chunks)]

    oneThread = maskAll(1)

    assert all(result == 'ok' for result, _ in oneThread)
    # Enough distinct values in each call that it's split across the threads.
    assert min(len({repr(value) for value in chunk}) for chunk in chunks) > 256
    assert maskAll(8) == oneThread
    assert maskAll(8) == maskAll(1, native=False)


def _chunkOutcome(built, chunk, index):
    """A chunk's masks, or the first error it raised."""
    try:
        return ('ok', exactly(built.maskColumn(chunk, index)))
    except MaskingError as error:
        return ('error', str(error))


@native
def test_a_refusal_is_not_remembered(maskingThreads):
    """Only answers are cached, so a value refused in one chunk is refused
    again in the next rather than answered from the cache.
    """
    maskingThreads(8)
    built = STRATEGIES['key'](KeyedHash(KEY, 'refusals'), STRATEGIES['key'].validateOptions({}))

    for index in range(2):
        with pytest.raises(MaskingError):
            built.maskColumn(['fine'] * 300 + [True], index)


@native
def test_the_cache_is_bounded_and_keeps_answering_past_its_bound(maskingThreads):
    maskingThreads(4)
    built = STRATEGIES['key'](KeyedHash(KEY, 'bounded'), STRATEGIES['key'].validateOptions({}))
    values = ['V{:06d}'.format(number) for number in range(70_000)]

    first = built.maskColumn(values, 0)

    assert built.maskColumn(values[::-1], 1) == first[::-1]
    assert len(set(first)) == len(values)


# A chunk in one call -------------------------------------------------------------

# One column of each kind a chunk mixes: the extension's, one kept, and one
# Python masks.
CHUNK_POLICY = {'id': 'keep', 'reference': 'key', 'hexReference': {'strategy': 'key', 'charset': 'hex'}, 'token': 'fpe', 'secret': 'hash',
                'email': 'email', 'phone': {'strategy': 'digits', 'keepTrailing': 2}, 'amount': {'strategy': 'number', 'decimals': 2},
                'name': 'fakeName', 'day': 'dateShift'}


def _bound(key=KEY, policy=None, native=True):
    """The policy bound to its columns; masked by the extension a chunk at a
    time, or with native False as before: by it column by column, or with
    native None in Python alone.
    """
    from bauta.masking import MaskingPlan

    policy = policy or CHUNK_POLICY
    bound = MaskingPlan(key=key, columns=policy).bind(list(policy))
    if native is not True:
        bound._chunkNatively = False
    if native is None:
        for strategy in bound.strategies:
            strategy._native = None

    return bound


def _chunkRows():
    """Rows past the size a call is split across threads at, each column's
    values those its strategy masks -- the ones Python finishes included."""
    random.seed(20260927)
    bound = _bound()
    columns = []
    for index, strategy in enumerate(bound.strategies):
        if index == 0:
            columns.append(list(range(3000)))
        elif strategy.NAME == 'dateShift':
            columns.append([None, datetime.date(2020, 2, 29), datetime.datetime(2021, 6, 1, 8, 30), '2019-12-31'])
        else:
            columns.append([value for value in VALUES if outcome(strategy, [value])[0][0] == 'ok'])

    return [(number,) + tuple(random.choice(column) for column in columns[1:]) for number in range(3000)]


def _applied(bound, rows):
    try:
        return ('ok', [exactly(row) for row in bound.apply(rows, chunkIndex=0)])
    except MaskingError as error:
        return ('error', str(error))


@native
def test_a_chunk_masks_as_its_columns_do(maskingThreads):
    """One call for the chunk must give what the extension gives column by
    column and what Python gives, whatever the rows are and however many
    threads mask them.
    """
    rows = _chunkRows()
    columnByColumn = _applied(_bound(native=False), rows)

    assert columnByColumn[0] == 'ok'
    assert _applied(_bound(native=None), rows) == columnByColumn
    assert _applied(_bound(), rows) == columnByColumn
    assert _applied(_bound(), [list(row) for row in rows]) == columnByColumn
    assert _applied(_bound(), tuple(rows)) == columnByColumn
    maskingThreads(8)
    assert _applied(_bound(), rows) == columnByColumn


@native
@pytest.mark.parametrize('bad', [{'reference': [5]}, {'day': [1]}, {'reference': [5], 'day': [1]}, {'phone': [7], 'reference': [9]},
                                 {'email': [4], 'amount': [2]}], ids=str)
def test_the_first_bad_value_raises_as_it_would_column_by_column(bad):
    """Columns are finished in read order, each in row order, so the error a
    chunk raises is the one it raised column by column -- here refusals by
    the extension (a bool for key and digits), by Python for a value the
    extension hands back (an integer for email, text for number), and by a
    column Python masks (dateShift), in columns before and after one another.
    """
    refused = {'reference': True, 'phone': True, 'email': 17, 'amount': 'x', 'day': 17}
    policy = list(CHUNK_POLICY)
    rows = [list(row) for row in _chunkRows()[:400]]
    for column, positions in bad.items():
        for position in positions:
            rows[position][policy.index(column)] = refused[column]

    expected = _applied(_bound(native=False), rows)

    assert expected[0] == 'error'
    assert _applied(_bound(), rows) == expected
    assert _applied(_bound(native=None), rows) == expected


def test_auto_shares_half_the_cores_between_the_jobs_that_run_at_once(monkeypatch):
    import bauta.masking.core as masking

    monkeypatch.delenv(masking.MASKING_THREADS_VARIABLE, raising=False)
    monkeypatch.setattr(masking, 'availableCores', lambda: 8)

    assert masking.maskingThreadsFor('auto', 1) == 4
    assert masking.maskingThreadsFor('auto', 3) == 1
    assert masking.maskingThreadsFor('auto', 16) == 1
    assert masking.maskingThreadsFor(3, 4) == 3

    monkeypatch.setattr(masking, 'availableCores', lambda: 1)
    assert masking.maskingThreadsFor('auto', 1) == 1
    monkeypatch.setattr(masking, 'availableCores', lambda: 8)

    monkeypatch.setenv(masking.MASKING_THREADS_VARIABLE, '5')
    assert masking.maskingThreadsFor('auto', 1) == 5
    monkeypatch.setenv(masking.MASKING_THREADS_VARIABLE, 'auto')
    assert masking.maskingThreadsFor(2, 2) == 2


@pytest.mark.parametrize('setting,valid', [('auto', True), (1, True), (12, True), (0, False), (-1, False), ('many', False)])
def test_masking_threads_is_auto_or_at_least_one(setting, valid):
    from bauta.configuration import Configuration, ConfigurationError, DataJobsFile

    raw = {'workers': 1, 'maskingThreads': setting, 'jobs': {}}
    if valid:
        assert Configuration.validateJobConfiguration(raw, DataJobsFile).maskingThreads == setting
    else:
        with pytest.raises(ConfigurationError, match='maskingThreads'):
            Configuration.validateJobConfiguration(raw, DataJobsFile)


# number: Python's decimal arithmetic reproduced in Rust ----------------------

def _numbers():
    """Integers, floats and Decimals shaped to reach what decimal arithmetic
    gets wrong: precision edges, exponent spellings, signed zeros, ties."""
    random.seed(20260921)
    numbers = [0, 1, -1, 7, 10 ** 17, -(10 ** 17), 10 ** 37, 10 ** 38, 10 ** 45, 2 ** 64, -(2 ** 70)]
    numbers += [random.randint(-10 ** digits, 10 ** digits) for digits in (1, 2, 4, 6, 9, 12, 15, 18, 25, 30, 38) for _ in range(40)]

    numbers += [0.0, -0.0, float('inf'), float('-inf'), float('nan'), 1e22, -1e22, 1e-7, 1.5e+16, 5e-324, 1.7976931348623157e308,
                0.1, 0.5, 2.5, 100.0, 123456.789, -0.001]
    numbers += [random.uniform(-1e6, 1e6) for _ in range(300)] + [random.random() * 10 ** random.randint(-20, 20) for _ in range(300)]

    numbers += [decimal.Decimal(text) for text in (
        '0', '0.00', '-0.00', '0E-7', 'NaN', 'Infinity', '-Infinity', '12.30', '-12.3400', '1.2E+5', '1E+30', '4.2E+3',
        '0.0000123', '1.2345678901234567890123456789', '9' * 38, '9' * 39, '0.5', '2.5', '-2.5', '1.005', '0.125')]
    for _ in range(600):
        digits = ''.join(random.choice('0123456789') for _ in range(random.randint(1, 40)))
        exponent = random.randint(-30, 10)
        numbers.append(decimal.Decimal('{}{}E{}'.format(random.choice(['', '-']), digits, exponent)))

    return numbers


NUMBERS = _numbers()

NUMBER_OPTIONS = [
    {}, {'variance': 0.5}, {'variance': 1}, {'variance': '0.001'}, {'min': 0, 'max': 100}, {'min': '-5.5', 'max': '1E+3'},
    {'decimals': 2}, {'decimals': 0}, {'min': 1, 'max': 2, 'decimals': 3}, {'variance': 0.05, 'decimals': 4},
    ]


@native
@pytest.mark.parametrize('options', NUMBER_OPTIONS, ids=str)
def test_number_masks_the_same_digits_natively(options):
    """Every value, byte for byte: a masked amount must join with the same
    amount masked by the other implementation, and keep the scale it had."""
    validated = STRATEGIES['number'].validateOptions(options)

    asNative = STRATEGIES['number'](KeyedHash(KEY, 'number'), validated)
    assert asNative._native is not None
    asPython = STRATEGIES['number'](KeyedHash(KEY, 'number'), validated)
    asPython._native = None

    fromPython = outcome(asPython, NUMBERS)
    for value, left, right in zip(NUMBERS, outcome(asNative, NUMBERS), fromPython):
        assert left == right, 'number {} disagrees on {!r}: {!r} vs {!r}'.format(options, value, left, right)

    # The fast path is the point: a finite, non-zero value of ordinary size
    # that Python masks to a non-zero number must be answered natively. Zeros,
    # whose sign Python tracks, and refusals are handed back by design.
    def ordinary(value, result):
        if result[0] != 'ok' or not value or not math.isfinite(value):
            return False
        if isinstance(value, int):
            fits = abs(value) < 10 ** 38
        elif isinstance(value, float):
            fits = 1e-300 < abs(value) < 1e300
        else:
            fits = len(value.as_tuple().digits) <= 28
        return fits and asPython.maskColumn([value], 0)[0] != 0

    values = [value for value, result in zip(NUMBERS, fromPython) if ordinary(value, result)]
    _, problems = asNative._native.maskColumn(values)
    assert len(values) > 500 and len(problems) <= len(values) // 33, 'number {}: {} of {} ordinary values went back to Python'.format(
        options, len(problems), len(values))


def test_a_run_requiring_the_native_masker_is_told_why_it_cannot_have_it(standInExtension, monkeypatch):
    import bauta.masking.core as masking

    extension, _ = standInExtension
    extension.__version__ = '0.0.1'

    problem = masking.requireNativeProblem(True)
    assert 'bauta-rs 0.0.1 is installed, which does not match' in problem and 'ten times slower' in problem
    assert masking.requireNativeProblem(False) is None

    monkeypatch.setenv('BAUTA_REQUIRE_NATIVE', '1')
    assert masking.requireNativeProblem(False) == problem

    extension.__version__ = importlib.metadata.version('bauta')
    masking._nativeModule.cache_clear()
    assert masking.requireNativeProblem(True) is None


def test_a_run_requiring_the_native_masker_stops_before_any_job_runs(monkeypatch, tmp_path):
    import bauta.masking.core as masking
    from bauta.configuration import Configuration, ConfigurationError, DataJobsFile
    from bauta.jobs.memory import FileMemory
    from bauta.jobs.runner import runDataJobs
    from tests.jobConfigs import dataJobFields

    monkeypatch.setenv('BAUTA_NATIVE', '0')
    masking._nativeModule.cache_clear()
    jobs = {'workers': 1, 'requireNative': True,
            'jobs': {'masked': dataJobFields(masking={'key': KEY, 'columns': {'id': 'key'}})}}
    try:
        with pytest.raises(ConfigurationError, match='BAUTA_NATIVE=0 turns it off'):
            runDataJobs(jobsFile=Configuration.validateJobConfiguration(jobs, DataJobsFile), connectionConfiguration={},
                        memory=FileMemory(tmp_path / 'memory.yaml'))
    finally:
        masking._nativeModule.cache_clear()


def test_by_default_a_run_stops_where_the_extension_is_installed_but_another_version(standInExtension, monkeypatch):
    """The upgrade that leaves bauta-rs behind: every masked job ten times
    slower, said once in a log nobody reads until the window is missed.
    Nobody chooses that, so by default it stops the run, saying how to fix
    it and how to go on in Python.
    """
    import bauta.masking.core as masking

    extension, _ = standInExtension
    extension.__version__ = '0.0.1'
    monkeypatch.delenv('BAUTA_REQUIRE_NATIVE', raising=False)

    problem = masking.requireNativeProblem(None)

    assert 'bauta-rs 0.0.1 is installed, which does not match' in problem
    assert 'requireNative: false' in problem and 'BAUTA_REQUIRE_NATIVE=0' in problem


def test_requiring_it_false_or_variable_zero_masks_in_python_past_a_mismatch(standInExtension, monkeypatch):
    import bauta.masking.core as masking

    extension, _ = standInExtension
    extension.__version__ = '0.0.1'

    assert masking.requireNativeProblem(False) is None

    monkeypatch.setenv('BAUTA_REQUIRE_NATIVE', '0')
    assert masking.requireNativeProblem(None) is None
    # The variable is over the file, as BAUTA_MASKING_THREADS is.
    assert masking.requireNativeProblem(True) is None


def test_by_default_a_run_goes_on_in_python_where_the_extension_was_never_installed_or_is_turned_off(monkeypatch):
    """Both are someone's choice -- the native masker is an extra, and
    BAUTA_NATIVE=0 is asked for -- so neither stops a run unless
    requireNative: true says it must.
    """
    import bauta.masking.core as masking

    monkeypatch.delenv('BAUTA_REQUIRE_NATIVE', raising=False)
    monkeypatch.delenv('BAUTA_NATIVE', raising=False)
    monkeypatch.setitem(sys.modules, 'bauta_rs', None)
    masking._nativeModule.cache_clear()
    try:
        assert masking.requireNativeProblem(None) is None
        assert 'not installed' in masking.requireNativeProblem(True)
        assert 'not installed' in masking.nativeUnavailableReason()

        monkeypatch.setenv('BAUTA_NATIVE', '0')
        masking._nativeModule.cache_clear()
        assert masking.requireNativeProblem(None) is None
        assert 'BAUTA_NATIVE=0' in masking.requireNativeProblem(True)
    finally:
        masking._nativeModule.cache_clear()


def test_a_run_says_once_as_it_starts_that_it_masks_in_python(monkeypatch, tmp_path):
    import bauta.masking.core as masking
    from bauta.configuration import Configuration, DataJobsFile
    from bauta.jobs.memory import FileMemory
    from bauta.jobs.runner import runDataJobs
    from tests.jobConfigs import dataJobFields

    monkeypatch.delenv('BAUTA_NATIVE', raising=False)
    monkeypatch.delenv('BAUTA_REQUIRE_NATIVE', raising=False)
    monkeypatch.setitem(sys.modules, 'bauta_rs', None)
    masking._nativeModule.cache_clear()
    warnings = []
    handler = logging.Handler(level=logging.WARNING)
    handler.emit = lambda record: warnings.append(record.getMessage())
    jobs = {'workers': 1, 'jobs': {'masked': dataJobFields(active=False, masking={'key': KEY, 'columns': {'id': 'key'}}),
                                   'other': dataJobFields(masking={'key': KEY, 'columns': {'id': 'key'}}, refresh=10 ** 9)}}
    try:
        memory = FileMemory(tmp_path / 'memory.yaml')
        memory.recordRun(job='other')
        logging.getLogger('bauta').addHandler(handler)
        runDataJobs(jobsFile=Configuration.validateJobConfiguration(jobs, DataJobsFile), connectionConfiguration={}, memory=memory)
    finally:
        logging.getLogger('bauta').removeHandler(handler)
        masking._nativeModule.cache_clear()

    assert [warning for warning in warnings if 'Masking in Python' in warning] == [
        'Masking in Python, about ten times slower than the native masker: bauta-rs is not installed; pip install "bauta[native]" installs it']

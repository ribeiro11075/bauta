"""Synthetic rows, for tables that can't be copied at all -- `bauta synthesize`.

Fills existing tables from nothing but the target's catalog. Integer keys
continue past the current maximum; foreign keys are drawn from parent rows,
so parents go first; columns named like personal data get what discovery's
proposed strategy makes of a placeholder; the rest is random within its type.
Deterministic for a seed and starting state.
"""
from __future__ import annotations

import datetime
import decimal
import hashlib
import logging
import math
import re
import uuid
from typing import AbstractSet, Any, Callable, Dict, Iterator, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

from ..database.dialects import ColumnDefinition, ForeignKey, quoteIdentifier
from .checks import ColumnCheck, columnChecks
from .discovery import BUILTIN_RULES, DiscoveryRules, nameWords, personalDataHint
from ..log import LOGGER_NAME
from ..masking import STRATEGIES, KeyedHash, changesValues
from .schema import INTEGER_BOOLEAN_NOTE, PortableType, portableType, tableKey

logger = logging.getLogger(LOGGER_NAME)

DEFAULT_NULL_SHARE = 0.1

# How many parent keys are read to draw foreign-key values from.
PARENT_SAMPLE_SIZE = 100_000

_WORDS = ('alpha', 'bravo', 'delta', 'harbor', 'maple', 'orbit', 'quartz', 'river', 'summit', 'timber', 'velvet', 'willow',
          'amber', 'cobalt', 'ember', 'falcon', 'granite', 'juniper', 'lumen', 'meadow', 'nectar', 'pebble', 'saffron', 'tundra')

_EPOCH = datetime.date(2015, 1, 1)


def _recentDays() -> int:
    """Days from _EPOCH to the end of this year, the span generated dates fall
    in. It ended at 2026-12-31, fixed, so from 2027 "recent" dates never
    reached the present. The end of the year rather than today, so a seed
    gives the same rows all year.
    """

    return (datetime.date(datetime.date.today().year, 12, 31) - _EPOCH).days


# How a column's values are spread in a source, for `--profile`: where
# synthetic rows take their shape from production without taking anyone's
# values. Labels are read only from a column with at most this many values,
# and only those this many rows share, so a value rare enough to point at
# someone is never copied.
PROFILE_MOST_LABELS = 20
PROFILE_FEWEST_ROWS_PER_LABEL = 5


class ColumnProfile(NamedTuple):
    """One column as a source holds it: the share of rows NULL, and either
    its labels with their counts, or its smallest and largest value.
    """

    nullShare: float
    labels: Tuple[Tuple[Any, int], ...] = ()
    low: Any = None
    high: Any = None


def profileTable(database: Any, table: str, rules: DiscoveryRules = BUILTIN_RULES) -> Dict[str, ColumnProfile]:
    """How each of `table`'s columns is spread in `database`, by upper-cased
    name, for synthesize to generate rows shaped like them.

    Never a column whose name suggests personal data, nor a key, which
    synthesize makes its own way; and of the rest only aggregates: the share
    NULL, the range of a number or a date, and the labels of a column of few
    values that enough rows share. Read one column at a time, so a type a
    database can't take MIN of is passed over rather than failing the rest.
    """

    definitions = database.getColumnDefinitions(table)
    if not definitions:
        raise SynthesisError('table {} was not found where it is profiled from'.format(table))

    keys = {column.upper() for column in database.getPrimaryColumnNames(table)}
    keys.update(column.upper() for foreignKey in database.getForeignKeysFor([table]) if tableKey(foreignKey.table) == tableKey(table)
                for column in foreignKey.columns)
    total = int(database.query('SELECT count(*) FROM {}'.format(table))[0][0])
    profiles: Dict[str, ColumnProfile] = {}
    if not total:
        return profiles

    for definition in definitions:
        name = definition.name
        if name.upper() in keys or personalDataHint(name, rules) is not None:
            continue
        kind = portableType(database.type, definition).kind
        column = quoteIdentifier(database.type, name)
        try:
            if kind in ('text', 'fixedText'):
                counted = database.query('SELECT {0}, count(*) FROM {1} WHERE {0} IS NOT NULL GROUP BY {0} ORDER BY 2 DESC'.format(column, table))
                present = sum(int(count) for _, count in counted)
                labels = tuple((value, int(count)) for value, count in counted if int(count) >= PROFILE_FEWEST_ROWS_PER_LABEL)
                if len(counted) > PROFILE_MOST_LABELS or not labels:
                    labels = ()
                profiles[name.upper()] = ColumnProfile(nullShare=1 - present / total, labels=labels)
            elif kind in ('smallint', 'integer', 'bigint', 'decimal', 'float', 'date', 'timestamp', 'timestampTz'):
                count, low, high = database.query('SELECT count({0}), min({0}), max({0}) FROM {1}'.format(column, table))[0]
                profiles[name.upper()] = ColumnProfile(nullShare=1 - int(count) / total, low=low, high=high)
        except Exception:
            database.rollback()

    return profiles


class SynthesisError(Exception):
    """A table that can't be filled as asked."""


class ColumnPlan(NamedTuple):
    """How one column is filled, and why -- what `--dry-run` prints."""

    column: str
    source: str
    description: str


Generator = Callable[[int], Any]


_FAKE_DESCRIPTIONS = {
    'fakeFirstName': 'a first name', 'fakeLastName': 'a last name', 'fakeName': 'a full name', 'fakeCity': 'a city',
    'fakeCompany': 'a company name', 'fakeStreetAddress': 'a street address',
    }


def _templateFor(words: AbstractSet[str]) -> str:

    if words & {'creditcard', 'cardnumber', 'ccnumber', 'pan'}:
        return '4000 0000 0000 0000'
    if words & {'zip', 'zipcode', 'postal', 'postalcode', 'postcode'}:
        return '00000'
    if words & {'ssn', 'socialsecurity', 'socialsecuritynumber'}:
        return '000-00-0000'
    if words & {'phone', 'phonenumber', 'mobile', 'cell', 'fax', 'telephone', 'tel'}:
        return '+1 555 010 0000'

    return '000000000000'


class _Synthesizer:
    """Builds one table's column generators."""

    def __init__(self, table: str, seed: int, nullShare: float, rules: DiscoveryRules = BUILTIN_RULES) -> None:
        self.salt = 'bauta synthetic data|{}|{}'.format(seed, table.lower())
        self.keyedHash = KeyedHash(self.salt, table.lower())
        self.nullShare = nullShare
        self.rules = rules


    def _unit(self, rowNumber: int, column: str) -> float:
        """A number in [0, 1) that depends only on the seed, table, column and row,
        so the same seed makes the same rows.
        """

        digest = hashlib.sha256('{}|{}|{}'.format(self.salt, column.lower(), rowNumber).encode()).digest()

        return int.from_bytes(digest[:7], 'big') / float(1 << 56)


    def byName(self, column: ColumnDefinition, portable: PortableType) -> Optional[Tuple[Generator, str]]:
        """A realistic generator where the column's name suggests personal data,
        by the same rules discovery uses. A rule of your own that says `keep`
        leaves the column to its type.
        """

        words = nameWords(column.name)
        textual = portable.kind in ('text', 'fixedText')
        numeric = portable.kind in ('smallint', 'integer', 'bigint', 'decimal', 'float')
        dated = portable.kind in ('date', 'timestamp', 'timestampTz')

        for rule in self.rules.names:
            if not words & rule.words:
                continue
            if not changesValues(rule.policy):
                return None
            strategy = rule.policy['strategy']
            name = column.name

            if strategy == 'email' and textual:
                email = STRATEGIES['email'](self.keyedHash, {})
                return (lambda row: email.mask('synthetic-{}@example.test'.format(row))), 'an email address at example.test'
            if strategy.startswith('fake') and textual:
                fake = STRATEGIES[strategy](self.keyedHash, {})
                return (lambda row: fake.mask(row)), _FAKE_DESCRIPTIONS[strategy]
            if strategy in ('digits', 'key') and (textual or numeric):
                if strategy == 'key' and not words & {'ssn', 'socialsecurity', 'socialsecuritynumber'}:
                    return (lambda row: 'user{}'.format(row)), 'a user name'
                template = _templateFor(words)
                return self._digitsLike(template, numeric), 'digits shaped like {}'.format(template)
            if strategy == 'hash' and textual:
                hashed = STRATEGIES['hash'](self.keyedHash, {})
                return (lambda row: hashed.mask('synthetic {} {}'.format(name, row))), 'an opaque token'
            if strategy == 'dateShift' and dated:
                return (lambda row: datetime.date(1940, 1, 1) + datetime.timedelta(days=int(self._unit(row, name) * 23725))), \
                    'a birth date between 1940 and 2004'
            if strategy in ('number', 'coordinate') and numeric:
                if words & {'latitude', 'lat'}:
                    return (lambda row: round(self._unit(row, name) * 180 - 90, 5)), 'a latitude'
                if words & {'longitude', 'lng', 'lon'}:
                    return (lambda row: round(self._unit(row, name) * 360 - 180, 5)), 'a longitude'
                return (lambda row: 30000 + int(self._unit(row, name) * 170000)), 'an amount between 30,000 and 200,000'
            # By the rule, not its strategy: a sensitive attribute is
            # masked as null, and generated as one of a few codes.
            if rule.name == 'sensitiveAttribute' and textual:
                choices = ('F', 'M', 'X') if words & {'gender', 'sex'} else ('A', 'B', 'C', 'D')
                return (lambda row: choices[int(self._unit(row, name) * len(choices))]), 'one of {}'.format(', '.join(choices))
            if strategy == 'null' and textual:
                return (lambda row: self._sentence(row, name, portable.length)), 'words'

        return None


    def _digitsLike(self, template: str, numeric: bool) -> Generator:
        """Values shaped like `template` -- a phone number, a postal code --
        with its digits generated; as an integer for a numeric column.
        """

        digits = STRATEGIES['digits'](self.keyedHash, {})
        count = sum(character.isdigit() for character in template)

        def generate(row: int) -> Any:
            # The row number, in the template's digits, keyed into other digits.
            seeded = iter(str(row).rjust(count, '0')[-count:])
            text = digits.mask(''.join(next(seeded) if character.isdigit() else character for character in template))
            if not numeric:
                return text
            number = ''.join(character for character in text if character.isdigit())[:9]
            return int(number.lstrip('0') or '0')

        return generate


    def _sentence(self, row: int, column: str, limit: Optional[int] = None) -> str:
        """Words ending in the row's own number, within `limit` characters.

        The number is what keeps two rows apart: cut to a short column, the
        words alone repeat within a few hundred rows, and a UNIQUE column
        refuses the second of them.
        """

        count = 3 + int(self._unit(row, column + '#count') * 8)
        words = ' '.join(_WORDS[int(self._unit(row, '{}#{}'.format(column, index)) * len(_WORDS))] for index in range(count)).capitalize()
        tail = ' {}.'.format(row + 1)

        if limit is None:
            return words + tail
        if limit < len(tail):
            # Too narrow even for the sentence's shape; the number alone has to keep the rows apart.
            return str(row + 1)[-limit:]

        return words[:limit - len(tail)] + tail


    def byType(self, column: ColumnDefinition, portable: PortableType) -> Tuple[Generator, str]:
        """A random value of the column's type, within its size."""

        name = column.name
        unit = self._unit
        kind = portable.kind

        if portable.note == INTEGER_BOOLEAN_NOTE:
            return (lambda row: int(unit(row, name) < 0.5)), '0 or 1, a boolean stored as an integer'
        if kind in ('smallint', 'integer', 'bigint'):
            # Within the declared precision where there is one: Oracle's
            # NUMBER(1) takes 0-9. Catalogs that count precision in bits give
            # a looser bound, never a wrong one.
            ceiling = {'smallint': 100, 'integer': 100_000, 'bigint': 1_000_000_000}[kind]
            if column.precision:
                ceiling = min(ceiling, 10 ** column.precision)
            return (lambda row: int(unit(row, name) * ceiling)), 'an integer below {:,}'.format(ceiling)
        if kind == 'decimal':
            scale = portable.scale or 0
            wholeDigits = min(6, (portable.precision or 12) - scale)
            step = decimal.Decimal(1).scaleb(-scale)
            ceiling = 10 ** max(0, wholeDigits)
            return (lambda row: (decimal.Decimal(repr(unit(row, name))) * ceiling).quantize(step, rounding=decimal.ROUND_DOWN)), \
                'a decimal with {} places'.format(scale)
        if kind == 'float':
            return (lambda row: round(unit(row, name) * 1000, 3)), 'a number'
        if kind == 'boolean':
            return (lambda row: unit(row, name) < 0.5), 'true or false'
        if kind == 'fixedText':
            width = portable.length or 1
            return (lambda row: ''.join(chr(65 + int(unit(row, '{}#{}'.format(name, index)) * 26)) for index in range(width))), \
                '{} letters'.format(width)
        if kind == 'text':
            limit = portable.length
            return (lambda row: self._sentence(row, name, limit)), 'words' + (', at most {} characters'.format(limit) if limit else '')
        if kind == 'date':
            days = _recentDays()
            return (lambda row: _EPOCH + datetime.timedelta(days=int(unit(row, name) * days))), 'a date since 2015'
        if kind in ('timestamp', 'timestampTz'):
            zone = datetime.timezone.utc if kind == 'timestampTz' else None
            start, days = datetime.datetime.combine(_EPOCH, datetime.time(), zone), _recentDays()
            return (lambda row: start + datetime.timedelta(seconds=int(unit(row, name) * days * 86400))), 'a timestamp since 2015'
        if kind == 'time':
            return (lambda row: (datetime.datetime.min + datetime.timedelta(seconds=int(unit(row, name) * 86400))).time()), 'a time of day'
        if kind == 'binary':
            size = min(16, portable.length or 16)
            return (lambda row: hashlib.sha256('{}|{}'.format(name, row).encode()).digest()[:size]), '{} random bytes'.format(size)
        if kind == 'uuid':
            return (lambda row: str(uuid.UUID(bytes=hashlib.sha256('{}|{}'.format(name, row).encode()).digest()[:16], version=4))), 'a UUID'
        if kind == 'json':
            return (lambda row: '{{"synthetic": true, "row": {}}}'.format(row)), 'a small JSON document'

        return (lambda row: self._sentence(row, name)), 'words'


    def byCheck(self, column: ColumnDefinition, portable: PortableType, check: ColumnCheck) -> Optional[Tuple[Generator, str]]:
        """Values the table's CHECK constraints allow: one of the values they
        list, or a number within their bounds at the column's own type and
        scale. A range with one side open reaches 10,000 past the other.
        None where the checks give nothing this column's type can follow.
        """

        if check.choices:
            return self._pick(column.name, check.choices), 'one of the {} values its CHECK allows'.format(len(check.choices))
        if check.low is None and check.high is None:
            return None

        low = decimal.Decimal(check.low if check.low is not None else check.high - 10000)  # type: ignore[operator]
        high = decimal.Decimal(check.high if check.high is not None else low + 10000)
        ranged = self._withinRange(column.name, portable, low, high, check.lowIncluded, check.highIncluded)

        return None if ranged is None else (ranged[0], ranged[1] + ', as its CHECK allows')


    def byProfile(self, column: ColumnDefinition, portable: PortableType, profile: ColumnProfile) -> Optional[Tuple[Generator, str]]:
        """Values spread as the profiled source's are: its labels, each as
        often as there, or a value between its smallest and largest. None
        where the profile gives neither.
        """

        name, unit = column.name, self._unit

        if profile.labels:
            return self._pick(name, [value for value, _ in profile.labels], [count for _, count in profile.labels]), \
                'one of {} labels, as often as in the profiled source'.format(len(profile.labels))

        low, high = profile.low, profile.high
        if low is None or high is None:
            return None
        if isinstance(low, float) and portable.kind == 'decimal':
            # SQLite keeps a DECIMAL as a float, by its shortest text.
            low, high = decimal.Decimal(repr(low)), decimal.Decimal(repr(high))
        asText = isinstance(low, str)
        if asText and portable.kind in ('date', 'timestamp', 'timestampTz'):
            # SQLite keeps dates as ISO text, and is given it back.
            try:
                low, high = (datetime.datetime.fromisoformat(value) if 'T' in value or ':' in value else datetime.date.fromisoformat(value)
                             for value in (low, high))
            except ValueError:
                return None
        if portable.kind in ('date', 'timestamp', 'timestampTz') and isinstance(low, datetime.date):
            span = high - low
            moment = (lambda row: low + span * unit(row, name)) if isinstance(low, datetime.datetime) else \
                (lambda row: low + datetime.timedelta(days=int(span.days * unit(row, name))))
            return ((lambda row: moment(row).isoformat()) if asText else moment), 'a date between the profiled source\'s first and last'
        if isinstance(low, (int, float, decimal.Decimal)) and not isinstance(low, bool):
            ranged = self._withinRange(name, portable, decimal.Decimal(repr(low) if isinstance(low, float) else low),
                                       decimal.Decimal(repr(high) if isinstance(high, float) else high))
            return None if ranged is None else (ranged[0], ranged[1] + ', as in the profiled source')

        return None


    def _pick(self, name: str, values: Sequence[Any], weights: Optional[Sequence[int]] = None) -> Generator:
        """One of `values` for each row, each as often as its weight says, or
        all alike.
        """

        total = sum(weights) if weights else len(values)
        edges: List[float] = []
        running = 0
        for weight in (weights or [1] * len(values)):
            running += weight
            edges.append(running / total)

        def pick(row: int) -> Any:
            drawn = self._unit(row, name + '#pick')
            return values[next((index for index, edge in enumerate(edges) if drawn < edge), len(values) - 1)]

        return pick


    def _withinRange(self, name: str, portable: PortableType, low: decimal.Decimal, high: decimal.Decimal, lowIncluded: bool = True,
                     highIncluded: bool = True) -> Optional[Tuple[Generator, str]]:
        """Numbers from `low` to `high`, each bound included or not, at the
        column's own type and scale; None for another type, or a range no
        value of it fits in.
        """

        unit = self._unit

        if _integerKind(portable):
            first = math.ceil(low) + (1 if not lowIncluded and low == math.ceil(low) else 0)
            last = math.floor(high) - (1 if not highIncluded and high == math.floor(high) else 0)
            if last < first:
                return None
            return (lambda row: first + int(unit(row, name) * (last - first + 1))), 'an integer from {} to {}'.format(first, last)
        if portable.kind == 'decimal':
            step = decimal.Decimal(1).scaleb(-(portable.scale or 0))
            smallest = low.quantize(step, rounding=decimal.ROUND_CEILING)
            smallest += step if not lowIncluded and smallest == low else 0
            largest = high.quantize(step, rounding=decimal.ROUND_FLOOR)
            largest -= step if not highIncluded and largest == high else 0
            if largest < smallest:
                return None
            span = largest - smallest
            return (lambda row: min(largest, smallest + (span * decimal.Decimal(repr(unit(row, name)))).quantize(step, rounding=decimal.ROUND_FLOOR))), \
                'a decimal from {} to {}'.format(smallest, largest)
        if portable.kind == 'float':
            lowest, highest = float(low), float(high)
            # Strictly inside: unit is below 1, and the nudge keeps it above an excluded low.
            return (lambda row: lowest + (highest - lowest) * (0.000001 + 0.999998 * unit(row, name))), \
                'a number from {:g} to {:g}'.format(lowest, highest)

        return None


    def orNull(self, generator: Generator, column: ColumnDefinition, share: Optional[float] = None) -> Generator:
        """NULL for a share of rows, where the column allows it: `share`, a
        profiled source's, or the run's.
        """

        share = self.nullShare if share is None else share
        if not column.nullable or share <= 0:
            return generator

        return lambda row: None if self._unit(row, column.name + '#null') < share else generator(row)


def _sequential(start: int) -> Generator:

    return lambda row: start + row


def _offset(generator: Generator, offset: int) -> Generator:

    return lambda row: generator(offset + row)


def _textKeys(existing: int, width: int, fixed: bool) -> Generator:
    """S1, S2, ... -- or, for a fixed-width column, S0001, S0002, ..., padded
    between the S and the number, so every key is distinct at full width.
    """

    if fixed:
        return lambda row: 'S' + str(existing + row + 1).zfill(width - 1)

    return lambda row: 'S{}'.format(existing + row + 1)


def _integerKind(portable: PortableType) -> bool:

    return portable.kind in ('smallint', 'integer', 'bigint') or (portable.kind == 'decimal' and not portable.scale)


def planTable(database: Any, table: str, rows: int, seed: int = 0, foreignKeys: Optional[Sequence[ForeignKey]] = None,
              nullShare: float = DEFAULT_NULL_SHARE, rules: DiscoveryRules = BUILTIN_RULES,
              profile: Optional[Mapping[str, ColumnProfile]] = None) -> Tuple[List[str], Callable[[int], Tuple[Any, ...]], List[ColumnPlan], int]:
    """How `table` would be filled: its columns, a row generator, what each
    column gets, and how many rows can be made.

    Reads the target's catalog and, for foreign keys, the parent tables' keys;
    writes nothing.
    """

    definitions = database.getColumnDefinitions(table)
    if not definitions:
        raise SynthesisError('table {} was not found'.format(table))

    # Every generator is indexed by the row's number within the run, so a
    # second run starts where the first stopped: from row 0 again it would
    # regenerate the first run's values, which any UNIQUE column then refuses.
    existing = int(database.query('SELECT count(*) FROM {}'.format(table))[0][0])
    primaryKey = {column.upper() for column in database.getPrimaryColumnNames(table)}
    # Matched schema and all: `app.orders` has the keys `app` declares, not
    # those of an `orders` in the connection's own schema.
    foreignKeys = [foreignKey for foreignKey in (foreignKeys if foreignKeys is not None else database.getForeignKeysFor([table]))
                   if tableKey(foreignKey.table) == tableKey(table)]
    synthesizer = _Synthesizer(table, seed, nullShare, rules)
    checks, unread = columnChecks(database.getCheckConstraints(table), [definition.name for definition in definitions])
    for definition in unread:
        # A definition is schema, not data: it may be logged.
        logger.warning('{}: CHECK {} is not one synthesize can follow -- it lists values or bounds a number on one column -- so rows '
                       'breaking it will be refused'.format(table, definition))
    generators: Dict[str, Generator] = {}
    plans: Dict[str, ColumnPlan] = {}
    available = rows

    _planForeignKeys(database, table, definitions, foreignKeys, synthesizer, existing, generators, plans)

    keyColumns = [definition for definition in definitions if definition.name.upper() in primaryKey]
    _planPrimaryKey(database, table, [definition for definition in keyColumns if definition.name.upper() not in generators], synthesizer, existing,
                    rows, generators, plans)
    if keyColumns and all(plans[definition.name.upper()].source == 'foreign key' for definition in keyColumns):
        # Every key column is a foreign key -- a bridge table. Only as many
        # distinct combinations exist as the parents allow.
        available = min(rows, _combinations(generators, [definition.name.upper() for definition in keyColumns]))

    for definition in definitions:
        name = definition.name.upper()
        if name not in generators:
            columnProfile = (profile or {}).get(name)
            generator, plans[name] = _planValues(synthesizer, definition, portableType(database.type, definition), checks.get(name), columnProfile)
            share = columnProfile.nullShare if columnProfile is not None else None
            generators[name] = _offset(synthesizer.orNull(_fitting(generator, definition.length), definition, share), existing)

    columns = [definition.name for definition in definitions]
    ordered = [generators[column.upper()] for column in columns]

    def makeRow(row: int) -> Tuple[Any, ...]:
        return tuple(generator(row) for generator in ordered)

    return columns, makeRow, [plans[column.upper()] for column in columns], available


def _planForeignKeys(database: Any, table: str, definitions: Sequence[ColumnDefinition], foreignKeys: Sequence[ForeignKey],
                     synthesizer: '_Synthesizer', existing: int, generators: Dict[str, Generator], plans: Dict[str, ColumnPlan]) -> None:
    """Each foreign-key column a value its parent holds, drawn from up to
    PARENT_SAMPLE_SIZE of its keys; NULL for a reference to the table
    itself, or to a parent with no rows where the column allows it.
    """

    spelled = {definition.name.upper(): definition.name for definition in definitions}

    for foreignKey in foreignKeys:
        columns = [column.upper() for column in foreignKey.columns]
        selfReference = foreignKey.referencedTable.upper() == foreignKey.table.upper()
        nullable = all(definition.nullable for definition in definitions if definition.name.upper() in columns)
        parentKeys: List[Tuple[Any, ...]] = []

        if not selfReference:
            query = 'SELECT DISTINCT {} FROM {} WHERE {}'.format(
                ', '.join(foreignKey.referencedColumns), foreignKey.referencedTable,
                ' AND '.join('{} IS NOT NULL'.format(column) for column in foreignKey.referencedColumns))
            _, parentKeys = database.sample(query, PARENT_SAMPLE_SIZE)
            parentKeys = [tuple(key) for key in parentKeys]
            if not parentKeys and not nullable:
                raise SynthesisError('{} references {}, which has no rows; fill {} first'.format(table, foreignKey.referencedTable,
                                                                                               foreignKey.referencedTable))

        for position, column in enumerate(columns):
            def pick(row: int, keys: List[Tuple[Any, ...]] = parentKeys, index: int = position, name: str = foreignKey.name) -> Any:
                if not keys:
                    return None
                return keys[int(synthesizer._unit(row, name) * len(keys))][index]
            generators[column] = _offset(pick, existing)
            description = 'NULL, as {} has no rows'.format(foreignKey.referencedTable) if not parentKeys and not selfReference \
                else 'an existing {} key'.format(foreignKey.referencedTable)
            if selfReference:
                description = 'NULL (references its own table)'
                generators[column] = lambda row: None
            plans[column] = ColumnPlan(spelled.get(column, column), 'foreign key', description)

        if selfReference and not nullable:
            raise SynthesisError('{} references itself through NOT NULL column(s) {}; synthesize can only leave such references NULL'.format(
                table, ', '.join(foreignKey.columns)))


def _planPrimaryKey(database: Any, table: str, keyParts: Sequence[ColumnDefinition], synthesizer: '_Synthesizer', existing: int, rows: int,
                    generators: Dict[str, Generator], plans: Dict[str, ColumnPlan]) -> None:
    """The primary key's columns that aren't foreign keys, unique: integers
    after the table's largest, UUIDs, or text numbered past the rows already there.
    """

    for definition in keyParts:
        portable = portableType(database.type, definition)
        name = definition.name
        if _integerKind(portable):
            current = database.query('SELECT max({}) FROM {}'.format(quoteIdentifier(database.type, name), table))[0][0]
            start = int(current or 0) + 1
            generators[name.upper()] = _sequential(start)
            plans[name.upper()] = ColumnPlan(name, 'primary key', 'sequential, from {}'.format(start))
        elif portable.kind == 'uuid':
            generator, _ = synthesizer.byType(definition, portable)
            generators[name.upper()] = _offset(generator, existing)
            plans[name.upper()] = ColumnPlan(name, 'primary key', 'a unique UUID')
        elif portable.kind in ('text', 'fixedText'):
            width = portable.length or 12
            if len('S{}'.format(existing + rows)) > width:
                raise SynthesisError('{}.{} holds only {} characters, too few for {} unique keys'.format(table, name, width, rows))
            generators[name.upper()] = _textKeys(existing, width, portable.kind == 'fixedText')
            plans[name.upper()] = ColumnPlan(name, 'primary key', 'unique text, {} onwards'.format(generators[name.upper()](0)))
        else:
            raise SynthesisError('{}.{} is a {} primary key, which synthesize can\'t make unique'.format(table, name, portable.kind))


def _planValues(synthesizer: '_Synthesizer', definition: ColumnDefinition, portable: PortableType, check: Optional[ColumnCheck],
                profile: Optional[ColumnProfile]) -> Tuple[Generator, ColumnPlan]:
    """Any other column's values, from the first of these that can say what
    they are: its CHECK constraints, the profiled source, its name, its type.
    """

    nullable = definition.nullable
    for source, follow, suffix in (('check', lambda: synthesizer.byCheck(definition, portable, check) if check else None, ', sometimes NULL'),
                                   ('profile', lambda: synthesizer.byProfile(definition, portable, profile) if profile else None, ', NULL as often'),
                                   ('name', lambda: synthesizer.byName(definition, portable), ''),
                                   ('type', lambda: synthesizer.byType(definition, portable), ', sometimes NULL')):
        found = follow()
        if found is not None:
            generator, reason = found
            return generator, ColumnPlan(definition.name, source, reason + (suffix if nullable else ''))

    raise AssertionError('unreachable: byType always answers')


def _fitting(generator: Generator, length: Optional[int]) -> Generator:
    """Text cut to the column's length; other values as they are."""

    if not length or length < 0:
        return generator

    def fitted(row: int) -> Any:
        value = generator(row)
        return value[:length] if isinstance(value, str) else value

    return fitted


def _combinations(generators: Dict[str, Generator], keyColumns: List[str]) -> int:
    """How many distinct key tuples the foreign-key generators can make, by trying."""

    seen = {tuple(generators[column](row) for column in keyColumns) for row in range(PARENT_SAMPLE_SIZE)}

    return len(seen)


def synthesizeTable(database: Any, table: str, rows: int, seed: int = 0, foreignKeys: Optional[Sequence[ForeignKey]] = None,
                    chunkSize: int = 1000, nullShare: float = DEFAULT_NULL_SHARE, rules: DiscoveryRules = BUILTIN_RULES,
                    profile: Optional[Mapping[str, ColumnProfile]] = None) -> int:
    """Inserts up to `rows` generated rows into `table`, and returns how many.

    Fewer than asked only for a table whose primary key is made entirely of
    foreign keys, which can't have more distinct rows than its parents allow.
    """

    columns, makeRow, plans, available = planTable(database, table, rows, seed=seed, foreignKeys=foreignKeys, nullShare=nullShare, rules=rules,
                                                   profile=profile)
    keyColumns = {column.upper() for column in database.getPrimaryColumnNames(table)}
    keyIndexes = [index for index, column in enumerate(columns) if column.upper() in keyColumns]
    # Only a key made wholly of foreign keys can repeat: any other has a part
    # generated unique, and remembering its keys would cost memory to find
    # nothing.
    if not all(plans[index].source == 'foreign key' for index in keyIndexes):
        keyIndexes = []
    seen: Set[Tuple[Any, ...]] = set()
    inserted = 0

    for chunk in _chunks(makeRow, rows, available, keyIndexes, seen, chunkSize):
        try:
            database.insert(table=table, data=chunk, chunkSize=chunkSize, columns=columns)
        except Exception as error:
            # Each chunk commits, so what came before is already in the table.
            if re.search(r'check', str(error), re.IGNORECASE):
                why = ('Values are kept to a CHECK that lists them or bounds a number on one column; this one is another kind, '
                       'which synthesize said it could not follow')
            else:
                why = ('Primary keys are made unique, and generated text ends in the row\'s number, but a UNIQUE constraint on a column '
                       'with too few distinct values -- a short column, a name, a number -- cannot be satisfied')
            raise SynthesisError('{} refused a generated row after {} inserted ({}). {}'.format(table, inserted, error, why)) from error
        inserted += len(chunk)

    return inserted


def _chunks(makeRow: Callable[[int], Tuple[Any, ...]], rows: int, available: int, keyIndexes: List[int], seen: Set[Tuple[Any, ...]],
            chunkSize: int) -> Iterator[List[Tuple[Any, ...]]]:
    """Generated rows, a chunk at a time, skipping repeated keys -- which only a
    key made wholly of foreign keys can produce, so `keyIndexes` is empty for
    any other and nothing is remembered.
    """

    chunk: List[Tuple[Any, ...]] = []
    produced = 0
    row = 0
    attempts = 0

    while produced < available and attempts < max(rows, available) * 20:
        values = makeRow(row)
        row += 1
        attempts += 1
        if keyIndexes:
            key = tuple(values[index] for index in keyIndexes)
            if key in seen:
                continue
            seen.add(key)
        chunk.append(values)
        produced += 1
        if len(chunk) == chunkSize:
            yield chunk
            chunk = []

    if chunk:
        yield chunk

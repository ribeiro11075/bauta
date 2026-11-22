"""dateShift: dates and times moved by a keyed number of days.

Part of the strategies package; see its __init__.
"""
from __future__ import annotations

import datetime
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..core import MASK_CACHE_SIZE, KeyedHash, MaskingError, Strategy, canonical
from .common import _integerOption, _textOption, _typeName


# _CALENDAR_ENDS, _CALENDAR_INSIDE: the first and last days Python's calendar
#   holds, by ordinal, and the days between them.
# _ISO_TEXT: ISO 8601 text as it is usually written: a date, then perhaps a
#   time to the hour, minute, second or fraction of one, then perhaps Z or an
#   offset.
_CALENDAR_ENDS = frozenset({datetime.date.min.toordinal(), datetime.date.max.toordinal()})
_CALENDAR_INSIDE = range(datetime.date.min.toordinal() + 1, datetime.date.max.toordinal())
_ISO_TEXT = re.compile(r'(\d{4}-\d{2}-\d{2})(?:[T ](\d{2})(?::(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?)?(Z|[+-]\d{2}(?::?\d{2})?)?)?')


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
        """Adds two caches of shifts already derived: _offsets by the day's
        ordinal, _subjectOffsets by the shiftBy value. Strategy's own cache
        can't hold them, since dates and timestamps are not among the types it
        remembers (equal values can differ in time zone), yet a shift depends
        on nothing but the day or the subject.
        """

        super().__init__(keyedHash, options)
        self._offsets: Dict[int, datetime.timedelta] = {}
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
        """The column shifted by each row's shiftBy value rather than its own
        day, so all of one subject's dates move together.
        """

        return [None if value is None else self._maskShifted(value, subject) for value, subject in zip(values, context)]


    def mask(self, value: Any) -> Any:
        """The value shifted by its own day's offset."""

        return self._maskShifted(value, None)


    def _maskShifted(self, value: Any, subject: Any) -> Any:
        """A date, timestamp or ISO 8601 text moved by the offset for
        `subject`, or for its own day when that is None, and returned in the
        form it came in.

        Text holding a plain ISO date has only its date rewritten: the shift is
        whole days, so the time, its fraction of a second and its zone stay as
        written, `.5` and `Z` included, where isoformat would write `.500000`
        and `+00:00`.
        """

        if isinstance(value, datetime.date):
            return self._shift(value, subject)

        if not isinstance(value, str):
            raise MaskingError('the dateShift strategy needs a date, a timestamp or ISO 8601 text, got {}'.format(_typeName(value)))

        text = value.strip()
        day = _isoDay(text)
        if day is not None:
            return self._shift(day, subject).isoformat() + text[10:]

        try:
            parsedTimestamp = datetime.datetime.fromisoformat(text)
        except ValueError:
            raise MaskingError('the dateShift strategy could not read a text value as an ISO 8601 date') from None

        shifted = self._shift(parsedTimestamp, subject)
        separator = 'T' if 'T' in text else ' '
        timespec = 'microseconds' if '.' in text else ('seconds' if text.count(':') >= 2 else 'minutes')

        return shifted.isoformat(sep=separator, timespec=timespec)


def _isoDay(text: str) -> Optional[datetime.date]:
    """The day of ISO 8601 text in the usual shape (see _ISO_TEXT), its time
    and zone checked as real ones, or None for text in another shape --
    which Python 3.11 reads more of than 3.10, as datetime.fromisoformat.
    Raises MaskingError for text in this shape naming no real day or time.
    """

    match = _ISO_TEXT.fullmatch(text)
    if match is None:
        return None

    _, hour, minute, second, _, zone = match.groups()
    try:
        day = datetime.date.fromisoformat(match.group(1))
        if hour is not None:
            datetime.time(int(hour), int(minute or 0), int(second or 0))
        if zone is not None and zone != 'Z':
            digits = zone[1:].replace(':', '')
            if int(digits[:2]) > 23 or int(digits[2:] or 0) > 59:
                raise ValueError
    except ValueError:
        raise MaskingError('the dateShift strategy could not read a text value as an ISO 8601 date') from None

    return day

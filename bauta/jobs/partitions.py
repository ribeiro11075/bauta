"""`partitions`: one job's rows read, masked and written as several slices at
once, each a range of a numeric column. See "Partitions" in
docs/concepts/how-it-works.md.

Ranges rather than a modulo of the column. `id >= 1000 AND id < 2000` is a
comparison every one of the seven databases can answer from an index on the
column, so each slice reads only its own rows; `MOD(id, 8) = 3` can use no
index, so every slice would scan the whole table, and no one spelling of it
works everywhere -- Oracle has no `%`, SQL Server no MOD. The price is one
query first, for the column's smallest and largest value, and slices only as
even as the values are spread.

The bounds go into the statement as integer literals rather than bound
parameters: they are integers this module computed, so there is nothing to
escape, and binding them would oblige a query without a watermark to double
every literal % on the %s dialects.
"""
from __future__ import annotations

import decimal
import math
from typing import Any, List, NamedTuple, Optional, Sequence, Tuple

from ..configuration import ConfigurationError

# The fewest rows a slice is worth, as `count: auto` judges it from the span of
# the column: below it, opening another pair of connections, and the slices'
# chunks finishing at different times, cost more than reading at once gains.
# A table of under twice this is read as one stream.
MINIMUM_ROWS_PER_SLICE = 250_000


class CoreBudget(NamedTuple):
    """What the run gave a job as it started, for `count: auto` to fit into.

    `share` is its share of the cores (masking.coreShare): what
    `maskingThreads: auto` masks with, and the most slices an automatic count
    chooses. `maskingThreads` is what the job masks with, and
    `automaticThreads` whether that was `auto`, which slices may then take
    over. `places` is the most slices its connections have room for, or None
    where they are not limited.
    """

    share: int
    maskingThreads: int
    automaticThreads: bool
    places: Optional[int]


def automaticCount(span: int, budget: CoreBudget, masksInPython: bool) -> Tuple[int, str]:
    """How many slices `count: auto` reads a job in, and why that many, given
    the `span` of its partition column -- how many integers lie between its
    smallest and largest value, which stands in for its rows.

    The fewest of: the job's share of the cores; the places its connections
    have free; one slice per MINIMUM_ROWS_PER_SLICE rows; and one, for a job
    masking in Python, whose slices would only take turns at the interpreter
    lock. Below two, the job is read as one stream.
    """

    if masksInPython:
        return 1, 'it masks in Python, whose slices would take turns at the interpreter lock'

    limits = [(budget.share, 'its share of the cores ({})'.format(budget.share)),
              (max(1, span // MINIMUM_ROWS_PER_SLICE), 'its size (one slice per {:,} rows)'.format(MINIMUM_ROWS_PER_SLICE))]
    if budget.places is not None:
        limits.append((budget.places, 'the places its connections have free ({})'.format(budget.places)))

    count, reason = min(limits, key=lambda limit: limit[0])

    return count, 'set by {}'.format(reason)


def maskingThreadsWith(slices: int, budget: CoreBudget) -> int:
    """The masking threads a job reading `slices` slices at once masks with.

    One masking thread masks on the calling thread, each slice's its own, all
    at once; more share one pool, which the slices then queue for. So where
    `maskingThreads: auto` gave the job no more threads than it has slices,
    each slice masks on its own thread instead: as many maskers, without the
    queue, and the job's share of the cores spent once rather than twice. A
    number set for maskingThreads is kept: the slices share that pool.
    """

    if budget.automaticThreads and slices > 1 and slices >= budget.maskingThreads:
        return 1

    return budget.maskingThreads


# The derived table a partitioned query is read through. A name, since MySQL,
# SQL Server and Oracle require one; no AS, which Oracle refuses there.
PARTITION_ALIAS = 'bauta_partition'


def _inner(sourceQuery: str) -> str:
    """The query as a derived table can hold it: without the semicolon a
    statement may end with, which would end the outer one there.
    """

    return sourceQuery.strip().rstrip(';').rstrip()


def wrappedQuery(sourceQuery: str, predicate: Optional[str] = None) -> str:
    """`sourceQuery` read through a derived table, so a predicate on one of
    the columns it returns applies whatever the query is: a join, a
    function's result, an alias.
    """

    query = 'SELECT * FROM ({}) {}'.format(_inner(sourceQuery), PARTITION_ALIAS)

    return query if predicate is None else '{} WHERE {}'.format(query, predicate)


def boundsQuery(sourceQuery: str, column: str) -> str:
    """The smallest and largest value of `column`, already quoted, over all
    the rows `sourceQuery` returns.
    """

    return 'SELECT MIN({0}), MAX({0}) FROM ({1}) {2}'.format(column, _inner(sourceQuery), PARTITION_ALIAS)


def resolveColumn(column: str, columns: Sequence[str]) -> str:
    """`column` as the query spells it -- an exact match, else the one match
    ignoring case, since Oracle returns an unquoted name in capitals -- or a
    ConfigurationError naming what it does return.
    """

    if column in columns:
        return column

    matches = [returned for returned in columns if returned.upper() == column.upper()]
    if len(matches) != 1:
        raise ConfigurationError('partitions.column "{}" is {} the columns sourceQuery returns ({}), so the job cannot slice its rows by '
                                 'it'.format(column, 'more than one of' if matches else 'not among', ', '.join(columns)))

    return matches[0]


def integerBound(value: Any, column: str) -> Optional[int]:
    """A bound from MIN or MAX, as an integer to slice at: None where the query
    returned no rows, or none with a value in the column.

    A fraction is rounded down, which moves where one slice ends and the next
    begins and nothing else: the first and last slices are open-ended, so
    every value falls in one of them. Anything not a number can't be divided
    into ranges, and says so by its type -- never its value, which is data.
    """

    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, (int, float, decimal.Decimal)):
        raise ConfigurationError('partitions.column "{}" holds {} values, and a partition is a range of numbers: slice on a numeric '
                                 'column, such as an integer key'.format(column, type(value).__name__))

    if isinstance(value, (float, decimal.Decimal)) and not math.isfinite(value):
        raise ConfigurationError('partitions.column "{}" holds a value that is not a finite number, which no range can '
                                 'bound'.format(column))

    return int(math.floor(value))


def splitPoints(lowest: int, highest: int, count: int) -> List[int]:
    """Where each slice after the first begins, so `count` slices divide the
    values from `lowest` to `highest` as evenly as integers can. Fewer where
    there are fewer distinct integers than slices.
    """

    span = highest - lowest + 1
    points = sorted({lowest + span * index // count for index in range(1, count)})

    return [point for point in points if lowest < point <= highest]


def slicePredicates(column: str, points: Sequence[int]) -> List[Optional[str]]:
    """One predicate per slice, on `column`, already quoted, so that every row
    is in exactly one.

    The first slice has no lower bound and the last no upper, so a row added
    beyond the bounds since they were read is still read once, and the first
    takes the rows where the column is null, which no comparison selects.
    None, for a single slice: the query as it is.
    """

    if not points:
        return [None]

    predicates: List[Optional[str]] = ['({0} < {1} OR {0} IS NULL)'.format(column, points[0])]
    predicates.extend('{0} >= {1} AND {0} < {2}'.format(column, start, end) for start, end in zip(points, points[1:]))
    predicates.append('{} >= {}'.format(column, points[-1]))

    return predicates

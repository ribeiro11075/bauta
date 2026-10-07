"""One data job, run to completion in its own process: rows streamed from
the source, transformed, masked and loaded a chunk at a time, retried on a
database error, and its success recorded in the order that keeps a crash
safe. See "How a data job moves rows" in docs/concepts/how-it-works.md.
"""
from __future__ import annotations

import collections
import contextlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any, Callable, Deque, Dict, Generator, Iterable, List, Optional, Sequence, Tuple

from ..configuration import ConfigurationError, ConnectionConfig, DatabaseType, DataJobConfig, FilesConnection, IcebergConnection, targetProblems
from ..database import Database, UnloadableValueError
from ..database.values import jsonColumnIndexes
from ..log import ATTEMPT_FAILED, LOGGER_NAME
from .fullRefresh import isFullRefresh
from ..log.scrubbing import describeError
from ..masking import BoundMasking, MaskingError, MaskingPlan, maskingIdentity
from ..masking import core as maskingModule
from ..transform import Transform, Transformer, TransformError, TransformResolutionError, resolveTransformer
from .dependencyGraph import JobOutcome, JobStatus
from .memory import MemoryBackend
from .partitions import (CoreBudget, Slice, automaticCount, boundsQuery, integerBound, isNumberBound, maskingThreadsWith, quantileQuery,
                         quantileSlices, resolveColumn, slicePredicates, splitPoints, wrappedQuery)
from .targets import LoadTarget, PostLoadError, TableTarget
from .throttle import StageTimes, readLimitFor, timedChunks

logger = logging.getLogger(LOGGER_NAME)


# How many chunks may be masked ahead of the one being written. One keeps the
# reader, masker and writer all busy on average, holding three chunks at once;
# a second absorbs a slow chunk on either side when masking and the database
# take about as long, for one more chunk of memory. More buys nothing further.
PIPELINE_DEPTH = 2


# Caps the doubling backoff, which would otherwise wait 5.7 hours in all over
# `retries: 12`.
MAXIMUM_RETRY_DELAY_SECONDS = 300.0


def _bindMasking(job: str, jobConfig: DataJobConfig, columns: List[str], log: bool = True) -> Optional[BoundMasking]:
    """Binds the job's masking policy to the columns its query returned, raising
    MaskingError before anything is written if it doesn't cover them all.
    `log` says what it binds, which a job's partitions, each binding it
    again, leave to the job.
    """

    if jobConfig.masking is None:
        return None

    plan = MaskingPlan(key=jobConfig.masking.key.get_secret_value(), columns=jobConfig.masking.columns,
                       defaultStrategy=jobConfig.masking.defaultStrategy)
    bound = plan.bind(columns)

    if log:
        logger.info('Masking {} column(s) under key {}'.format(len(columns), plan.fingerprint), extra={'job': job, 'keyFingerprint': plan.fingerprint})
        for entry in bound.manifest:
            logger.debug('Masking {} with {}{}'.format(entry.column, entry.strategy, ' in domain {}'.format(entry.domain) if entry.domain else ''))

    return bound


@contextlib.contextmanager
def _openTarget(job: str, jobConfig: DataJobConfig, settings: ConnectionConfig) -> Generator[LoadTarget, None, None]:
    """The job's target, open for as long as the job runs. On the way out of a
    failure abort() is called first, then whatever the target holds open --
    a database connection, an Iceberg catalog -- is closed.
    """

    problems = targetProblems(jobConfig, settings)
    if problems:
        # Validation says so against connections.yaml; asked again here for a
        # caller that built the job without it.
        raise ConfigurationError('targetConnection "{}" {}'.format(jobConfig.targetConnection, '; '.join(problems)))

    with contextlib.ExitStack() as held:
        target: LoadTarget
        if isinstance(settings, FilesConnection):
            from ..lake import FileTarget

            target = FileTarget(job, jobConfig, settings)
            held.callback(target.close)
        elif isinstance(settings, IcebergConnection):
            from ..lake.iceberg import IcebergTarget

            target = IcebergTarget(job, jobConfig, settings)
            held.callback(target.close)
        else:
            target = TableTarget(held.enter_context(Database(connectionSettings=settings, create=True)), jobConfig)

        try:
            yield target
        except BaseException:
            target.abort()
            raise


def _executeDataJob(job: str, jobConfig: DataJobConfig, connectionConfiguration: Dict[str, ConnectionConfig], watermark: Any = None) -> JobOutcome:
    """Runs one data job to completion, raising on failure. See "How a data job
    moves rows" in docs/concepts/how-it-works.md, and "Partitions" there for a
    job read as several slices at once.

    Transforms and the masking policy are both checked against the query's
    columns before the first write, so a misconfigured job fails with nothing
    loaded. Masking runs after transforms, so values are normalized before
    they are keyed.
    """

    columnTransforms: Dict[str, List[Transformer]] = {
        column: [resolveTransformer(reference) for reference in references] for column, references in jobConfig.sourceQueryColumnTransforms.items()
        }
    times = StageTimes()

    with Database(connectionSettings=connectionConfiguration[jobConfig.sourceConnection]) as sourceConnection, \
         _openTarget(job, jobConfig, connectionConfiguration[jobConfig.targetConnection]) as target:

        sourceQuery = jobConfig.sourceQuery
        parameters = None

        if jobConfig.watermarkColumn:
            sourceQuery, parameters = sourceConnection.bindWatermark(sourceQuery, watermark)
            if isFullRefresh(jobConfig):
                logger.info('Extracting all of {} for a full refresh, from watermark {!r}, to replace {}'.format(
                    jobConfig.sourceConnection, watermark, jobConfig.targetTableFinal))
            else:
                logger.info('Extracting {} incrementally, from watermark {!r}'.format(jobConfig.sourceConnection, watermark))

        # Partitions with a count to choose start as one stream: whether to
        # slice is decided once the query's columns are known, and reading
        # the query as it is keeps a job that can't be sliced working.
        explicit = jobConfig.partitions is not None and not jobConfig.partitions.automatic
        chunks: Optional[Iterable[List[Tuple[Any, ...]]]] = None
        if not explicit:
            logger.debug('Streaming sourceQuery against {} in chunks of {}'.format(jobConfig.sourceConnection, jobConfig.chunkSize))
            with _timed(times, 'read'):
                # The query runs, and its first chunk is fetched, here.
                sourceQueryColumns, chunks = sourceConnection.stream(query=sourceQuery, chunkSize=jobConfig.chunkSize, parameters=parameters)
            description = getattr(chunks, 'description', None)
        else:
            # Described without reading a row: the partitions read them.
            sourceQueryColumns, described = sourceConnection.stream(query=wrappedQuery(sourceQuery, '1=0'), chunkSize=1, parameters=parameters)
            with described:
                description = described.description
        logger.debug('sourceQuery returned columns: {}'.format(sourceQueryColumns))

        watermarkIndex = None

        if jobConfig.watermarkColumn:
            # Ignoring case where the match is unambiguous, as partitions.column
            # is: Oracle returns an unquoted name in capitals, so `updated_at`
            # was refused as missing from UPDATED_AT.
            matches = [column for column in sourceQueryColumns if column == jobConfig.watermarkColumn] or \
                [column for column in sourceQueryColumns if column.upper() == jobConfig.watermarkColumn.upper()]
            if len(matches) != 1:
                raise ConfigurationError(
                    'watermarkColumn "{}" is {} the columns sourceQuery returns {} -- '
                    'the job cannot tell how far it got'.format(jobConfig.watermarkColumn, 'more than one of' if matches else 'not among',
                                                                sourceQueryColumns))
            watermarkIndex = sourceQueryColumns.index(matches[0])

        transform = Transform(columns=sourceQueryColumns, columnTransforms=columnTransforms)
        transform.validate()
        if columnTransforms:
            logger.debug('Applying transforms to column(s): {}'.format(', '.join(columnTransforms)))

        masking = _bindMasking(job, jobConfig, sourceQueryColumns)
        jsonColumns = _decodedJsonColumns(masking, description, getattr(connectionConfiguration[jobConfig.sourceConnection], 'type', None),
                                          sourceQueryColumns)

        if masking is not None and watermarkIndex is not None and not masking.strategies[watermarkIndex].PASSTHROUGH:
            # Validation refuses this too; asked again here, before a row is
            # read, since a configuration copied with model_copy skips it.
            raise MaskingError('watermarkColumn "{}" is masked with {}, and the watermark is read before masking and kept in run state '
                               'and logs, so it would leak the unmasked value'.format(
                                   jobConfig.watermarkColumn, masking.manifest[watermarkIndex].strategy))

        predicates: List[Slice] = []
        if explicit:
            # Before begin(), so a column that can't be sliced fails the job
            # with its target as it was.
            predicates = _partitionPredicates(job, jobConfig, sourceConnection, sourceQuery, parameters, sourceQueryColumns)
        elif jobConfig.partitions is not None:
            predicates = _automaticPredicates(job, jobConfig, connectionConfiguration, sourceConnection, target, sourceQuery, parameters,
                                              sourceQueryColumns, masking)
            if predicates:
                assert chunks is not None
                getattr(chunks, 'close', lambda: None)()
                chunks = None
        if len(predicates) > 1:
            threads = maskingThreadsWith(len(predicates), _coreBudget())
            if threads != _coreBudget().maskingThreads:
                maskingModule.setMaskingThreads(threads)
                logger.debug('Masking each of the {} partitions on its own thread'.format(len(predicates)), extra={'job': job})

        with _timed(times, 'write'):
            target.begin(sourceQueryColumns, description)
        target.holdJson(jsonColumnIndexes(getattr(connectionConfiguration[jobConfig.sourceConnection], 'type', None), description))

        logger.info('Loading into {} a chunk at a time'.format(target.loadName))

        try:
            if chunks is not None:
                limit = readLimitFor(jobConfig.sourceConnection, connectionConfiguration[jobConfig.sourceConnection])
                rowCount, highWatermark = _loadChunks(timedChunks(chunks, times, limit), _preparer(transform, masking, times=times,
                                                                                                    jsonColumns=jsonColumns),
                                                      target.write, watermarkIndex, target.loadName, times=times)
            else:
                rowCount, highWatermark = _loadPartitions(job, jobConfig, connectionConfiguration, target, sourceQuery, parameters,
                                                          sourceQueryColumns, predicates, columnTransforms, watermarkIndex, times, jsonColumns)
        except Exception as error:
            if masking is not None:
                _noteIfMaskedValueDoesNotFit(error, job, target.loadName)
            raise

        logger.info('Streamed {} row(s) from {} into {}'.format(rowCount, jobConfig.sourceConnection, target.loadName))

        with _timed(times, 'write'):
            target.finish(rowCount)

    maskingApplied = None
    if masking is not None:
        maskingApplied = {'columns': [entry._asdict() for entry in masking.manifest]}

    stages = times.asDict()
    logger.info('{}: busy reading {read:.1f}s, masking {mask:.1f}s, writing {write:.1f}s, waiting on the read limit {throttled:.1f}s'.format(
        job, **stages), extra={'job': job, 'stages': stages})

    return JobOutcome(job=job, status=JobStatus.COMPLETED, rowCount=rowCount, watermark=highWatermark, masking=maskingApplied, stages=stages)


@contextlib.contextmanager
def _timed(times: Optional[StageTimes], stage: str) -> Generator[None, None, None]:
    """Adds the time the block takes to `stage`, whether or not it raises."""

    started = time.perf_counter()
    try:
        yield
    finally:
        if times is not None:
            times.add(stage, time.perf_counter() - started)


# The policies that take a JSON column's text as it is: `json`, which reads
# JSON text, `null`, and `keep` with anything else that passes values
# through (Strategy.PASSTHROUGH).
_TAKES_JSON_TEXT = frozenset({'json', 'null'})


def _decodedJsonColumns(masking: Optional[BoundMasking], description: Any, databaseType: Any, columns: Sequence[str]) -> List[Tuple[int, str]]:
    """The JSON columns -- PostgreSQL's json and jsonb, MySQL's and DuckDB's
    JSON, which arrive as their text -- whose policy needs the value the
    JSON holds rather than its text: every policy but those above.

    Given the text, `email` keyed on "ana@corp.example", quotes and all,
    rather than the address, so it masked differently from the same address
    in a text column, or before the text arrived; and what it returned,
    u3050fb9483cd@example.test, was no longer JSON, which a jsonb column
    refused and JSON Lines wrote as an invalid line. See _decodeJson.
    """

    if masking is None:
        return []

    return [(index, columns[index]) for index in jsonColumnIndexes(databaseType, description)
            if not (masking.strategies[index].PASSTHROUGH or masking.manifest[index].strategy in _TAKES_JSON_TEXT)]


def _decodeJson(rows: Sequence[Sequence[Any]], jsonColumns: Sequence[Tuple[int, str]]) -> List[Tuple[Any, ...]]:
    """Each of `jsonColumns`' JSON text as the value it holds -- a string, a
    number, true or false, an object or an array, or None for JSON null --
    which is what transforms and strategies were given for it before JSON
    arrived as text, so they mask as they did.
    """

    decoded = []
    for row in rows:
        values = list(row)
        for index, column in jsonColumns:
            if isinstance(values[index], str):
                try:
                    values[index] = json.loads(values[index])
                except ValueError:
                    raise MaskingError('{} is a JSON column, but holds text that is not JSON'.format(column)) from None
        decoded.append(tuple(values))

    return decoded


def _encodeJson(rows: Sequence[Sequence[Any]], jsonColumns: Sequence[Tuple[int, str]], masking: BoundMasking) -> List[Tuple[Any, ...]]:
    """Each of `jsonColumns`' masked values back as JSON text, so a JSON
    column, or a JSON Lines file, gets JSON: the string a strategy returns as
    a JSON string, a number as a number. NULL stays NULL. A value JSON can't
    hold -- NaN, an infinity -- fails the job, naming the column, never the
    value.
    """

    encoded = []
    for row in rows:
        values = list(row)
        for index, column in jsonColumns:
            if values[index] is not None:
                try:
                    values[index] = json.dumps(values[index], ensure_ascii=False, allow_nan=False, default=str)
                except ValueError:
                    raise MaskingError('{} is a JSON column, and {} masked a value in it to a number JSON cannot hold'.format(
                        column, masking.manifest[index].strategy)) from None
        encoded.append(tuple(values))

    return encoded


def _preparer(transform: Transform, masking: Optional[BoundMasking], partition: int = 0,
              partitions: int = 1, times: Optional[StageTimes] = None,
              jsonColumns: Sequence[Tuple[int, str]] = ()) -> Callable[[int, List[Tuple[Any, ...]]], List[Any]]:
    """Transforms and masks one chunk, given its position in what is read.

    `shuffle` keys on that position, so a job's partitions number their
    chunks apart: the n-th chunk of partition p is chunk n * partitions + p,
    which no other chunk of the job is. Without partitions, it is n.

    `jsonColumns`, from _decodedJsonColumns, are decoded before transforms
    and masking and encoded as JSON after.
    """

    def prepare(chunkIndex: int, chunk: List[Tuple[Any, ...]]) -> List[Any]:
        with _timed(times, 'mask'):
            rows = _decodeJson(chunk, jsonColumns) if jsonColumns else chunk
            rows = transform.apply(rows)

            if masking is not None:
                # By read order, not masking order: `shuffle` keys on it.
                rows = masking.apply(rows, chunkIndex=chunkIndex * partitions + partition)
                if jsonColumns:
                    rows = _encodeJson(rows, jsonColumns, masking)

        return rows

    return prepare


def _loadChunks(chunks: Iterable[List[Tuple[Any, ...]]], prepare: Callable[[int, List[Tuple[Any, ...]]], List[Any]],
                write: Callable[[List[Any]], None], watermarkIndex: Optional[int], loadName: str,
                stopped: Optional[threading.Event] = None, times: Optional[StageTimes] = None) -> Tuple[int, Any]:
    """Prepares and writes every chunk, returning the rows written and the
    highest watermark among them. `stopped`, once set, ends the load before
    its next write: another of the job's partitions has failed. Writing is
    timed into `times`; reading and preparing time themselves.
    """

    rowCount = 0
    highWatermark = None

    def writeChunk(rows: List[Any], watermark: Any) -> None:
        nonlocal rowCount, highWatermark

        if stopped is not None and stopped.is_set():
            raise _PartitionStopped()

        with _timed(times, 'write'):
            write(rows)

        rowCount += len(rows)
        # Only once the rows have landed, or a failed job's next run would
        # start past them.
        if watermark is not None and (highWatermark is None or watermark > highWatermark):
            highWatermark = watermark

        logger.debug('Loaded {} row(s) into {} ({} so far)'.format(len(rows), loadName, rowCount))

    _streamChunks(chunks, prepare, writeChunk, watermarkIndex, _pipelineDepth())

    return rowCount, highWatermark


class _PartitionStopped(Exception):
    """Ends a partition's load once another partition of its job has failed.
    Never the error a job reports: that is the failure that stopped it.
    """


def _quantileSlices(database: Database, sourceQuery: str, parameters: Optional[Sequence[Any]], quotedColumn: str, count: int) -> List[Slice]:
    """Slices of a column that isn't a number -- a UUID key, say -- at the
    values the database deals it into `count` even shares at; see
    partitions.quantileSlices. Read in one ordered pass over the column.
    """

    _, rows = database.stream(query=quantileQuery(sourceQuery, quotedColumn, count), chunkSize=count + 1, parameters=parameters)
    with rows:
        maxima = [row[0] for chunk in rows for row in chunk]

    return quantileSlices(quotedColumn, maxima, database.dialect.placeholders(1)[0])


def _partitionPredicates(job: str, jobConfig: DataJobConfig, sourceConnection: Database, sourceQuery: str,
                         parameters: Optional[Sequence[Any]], columns: List[str]) -> List[Slice]:
    """The predicate each of the job's partitions reads its slice with: ranges
    of the partition column, between the smallest and largest value the
    query returns in it, or, for a column that isn't a number, between the
    values it divides evenly at. Fewer than `count` where the values are
    fewer, and one, reading everything, where there are none.

    The bounds are data, so they are never logged.
    """

    assert jobConfig.partitions is not None and jobConfig.partitions.column is not None and not jobConfig.partitions.automatic
    column = resolveColumn(jobConfig.partitions.column, columns)
    quotedColumn = sourceConnection.quoted([column])[0]

    try:
        _, rows = sourceConnection.stream(query=boundsQuery(sourceQuery, quotedColumn), chunkSize=1, parameters=parameters)
        with rows:
            first = next(rows, [])
        bounds = first[0] if first else (None, None)
    except Exception:
        # No MIN or MAX for the column's type -- PostgreSQL's uuid -- which
        # the even shares don't need. Rolled back, as a failed statement
        # leaves PostgreSQL's transaction refusing the next.
        sourceConnection.rollback()
        bounds = ('', '')
    if all(isNumberBound(value) for value in bounds):
        lowest, highest = (integerBound(value, column) for value in bounds)
        points = [] if lowest is None or highest is None else splitPoints(lowest, highest, int(jobConfig.partitions.count))
        predicates = [Slice(predicate) for predicate in slicePredicates(quotedColumn, points)]
    else:
        predicates = _quantileSlices(sourceConnection, sourceQuery, parameters, quotedColumn, int(jobConfig.partitions.count))

    logger.info('Reading {} as {} partition(s) of {}, at once'.format(jobConfig.sourceConnection, len(predicates), column),
                extra={'job': job, 'partitions': len(predicates)})

    return predicates


_budget: Optional[CoreBudget] = None


def setCoreBudget(budget: CoreBudget) -> None:
    """What the run gave this job's process as it started; see CoreBudget."""

    global _budget
    _budget = budget


def _coreBudget() -> CoreBudget:
    """This process's budget, or, for a job run outside a run -- from Python, a
    test, a benchmark -- a share as the only job running, on one masking thread.
    """

    return _budget if _budget is not None else CoreBudget(share=maskingModule.coreShare(1), maskingThreads=1, automaticThreads=False, places=None)


def _automaticPredicates(job: str, jobConfig: DataJobConfig, connectionConfiguration: Dict[str, ConnectionConfig], sourceConnection: Database,
                         target: LoadTarget, sourceQuery: str, parameters: Optional[Sequence[Any]], columns: List[str],
                         masking: Optional[BoundMasking]) -> List[Slice]:
    """The predicates for `partitions: auto`, or `count: auto`, or none to read
    the job as one stream, the query as it is. See automaticCount.

    A column the configuration names that can't be sliced fails the job, as
    it would with a count; one chosen here -- the target's primary key -- is
    passed over with a line saying why, as is a query the database won't read
    as a derived table. Automatic partitions never stop a job that runs
    without them.
    """

    assert jobConfig.partitions is not None
    named = jobConfig.partitions.column

    def oneStream(why: str) -> List[Slice]:
        logger.info('partitions: reading {} as one stream: {}'.format(jobConfig.targetTableFinal, why), extra={'job': job})
        return []

    # A named column is checked first: a misspelling fails wherever it is.
    column = resolveColumn(named, columns) if named is not None else None

    if not isinstance(target, TableTarget):
        return oneStream('it writes files or an Iceberg table, which one writer publishes')
    if getattr(connectionConfiguration[jobConfig.targetConnection], 'type', None) == DatabaseType.SQLITE:
        return oneStream('SQLite commits one write at a time, so slices would only wait for each other')

    if column is None:
        try:
            primaryKey = target.database.getPrimaryColumnNames(table=jobConfig.targetTableFinal)
        except Exception as error:
            return oneStream('its target\'s primary key could not be read -- {}'.format(describeError(error)))
        if len(primaryKey) != 1:
            return oneStream('its target\'s primary key is {} columns, and a partition is a range of one'.format(len(primaryKey)) if primaryKey
                             else 'its target has no primary key to slice by')
        try:
            column = resolveColumn(primaryKey[0], columns)
        except ConfigurationError:
            return oneStream('sourceQuery does not return its target\'s primary key, {}'.format(primaryKey[0]))

    budget = _coreBudget()
    masksInPython = masking is not None and bool(masking._maskedIndexes) and maskingModule.nativeVersion() is None
    upper, why = automaticCount(2 ** 62, budget, masksInPython)
    if upper < 2:
        return oneStream(why)

    quotedColumn = sourceConnection.quoted([column])[0]
    try:
        # On a connection of its own: a query the database refuses leaves
        # the job's own, already reading, as it was.
        with Database(connectionSettings=connectionConfiguration[jobConfig.sourceConnection]) as probe:
            try:
                _, rows = probe.stream(query=boundsQuery(sourceQuery, quotedColumn), chunkSize=1, parameters=parameters)
                with rows:
                    first = next(rows, [])
                bounds = first[0] if first else (None, None)
            except Exception:
                # No MIN or MAX for its type -- PostgreSQL's uuid -- so not a
                # number; the shares are read without them.
                probe.rollback()
                bounds = ('', '')
            numeric = all(isNumberBound(value) for value in bounds)
            # A column that isn't a number is sliced where its values divide
            # evenly, read now, as many as the cores allow.
            quantiles = None if numeric or None in bounds else _quantileSlices(probe, sourceQuery, parameters, quotedColumn, upper)
    except Exception as error:
        return oneStream('its range could not be read -- {}'.format(describeError(error)))

    if quantiles is not None:
        predicates, why = quantiles, 'one per {} of the cores this job may use, at the values its rows divide evenly at'.format(len(quantiles))
        if len(predicates) < 2:
            return oneStream('{} holds too few values to slice'.format(column))
    else:
        try:
            lowest, highest = (integerBound(value, column) for value in bounds)
        except ConfigurationError as error:
            if named is not None:
                raise
            return oneStream(str(error))
        if lowest is None or highest is None:
            return oneStream('sourceQuery returns no rows with a value in {}'.format(column))

        count, why = automaticCount(highest - lowest + 1, budget, masksInPython)
        if count < 2:
            return oneStream(why)

        predicates = [Slice(predicate) for predicate in slicePredicates(quotedColumn, splitPoints(lowest, highest, count))]
        if len(predicates) < 2:
            return oneStream('{} holds too few values to slice'.format(column))

    logger.info('partitions: reading {} as {} slices of {} at once, {}'.format(jobConfig.targetTableFinal, len(predicates), column, why),
                extra={'job': job, 'partitions': len(predicates)})

    return predicates


def _sliceQuery(database: Database, sourceQuery: str, parameters: Optional[Sequence[Any]], piece: Slice) -> Tuple[str, Optional[Tuple[Any, ...]]]:
    """The query one slice reads, and what it binds: the watermark's, then
    the slice's own bounds. On the %s dialects, a query that bound nothing
    before has its literal % doubled once it binds, as the driver then wants:
    `LIKE 'a%'` would otherwise be read as a placeholder.
    """

    query = sourceQuery
    if piece.parameters and not parameters and database.dialect.placeholders(1)[0] == '%s':
        query = sourceQuery.replace('%', '%%')
    bound = tuple(parameters or ()) + piece.parameters

    return wrappedQuery(query, piece.predicate), (bound or None)


def _loadPartitions(job: str, jobConfig: DataJobConfig, connectionConfiguration: Dict[str, ConnectionConfig], target: LoadTarget,
                    sourceQuery: str, parameters: Optional[Sequence[Any]], columns: List[str], predicates: List[Slice],
                    columnTransforms: Dict[str, List[Transformer]], watermarkIndex: Optional[int],
                    times: Optional[StageTimes] = None, jsonColumns: Sequence[Tuple[int, str]] = ()) -> Tuple[int, Any]:
    """Reads, masks and writes the job's rows as slices, one per predicate, all
    at once, each on threads and connections of its own; see "Partitions" in
    docs/concepts/how-it-works.md. Returns the rows written and the highest
    watermark among them, once every slice has loaded.

    The first slice to fail stops the others before their next write, and its
    error is the job's: a job whose slices didn't all load has failed, so it
    neither swaps nor upserts from its stage, and nothing is recorded.
    """

    count = len(predicates)
    sourceSettings = connectionConfiguration[jobConfig.sourceConnection]
    # One for all the slices: the connection's limit is on all its reading.
    limit = readLimitFor(jobConfig.sourceConnection, sourceSettings)
    stopped = threading.Event()

    def loadSlice(index: int, piece: Slice) -> Tuple[int, Any]:
        # Everything a slice uses is its own: its connections, since one
        # connection serves one thread, and its transform and masking.
        with Database(connectionSettings=sourceSettings) as source, target.openWriter() as writer:
            with _timed(times, 'read'):
                query, bound = _sliceQuery(source, sourceQuery, parameters, piece)
                _, chunks = source.stream(query=query, chunkSize=jobConfig.chunkSize, parameters=bound)
            prepare = _preparer(Transform(columns=columns, columnTransforms=columnTransforms), _bindMasking(job, jobConfig, columns, log=False),
                                index, count, times, jsonColumns)
            loaded = _loadChunks(timedChunks(chunks, times, limit), prepare, writer.write, watermarkIndex,
                                 '{} (partition {} of {})'.format(target.loadName, index + 1, count), stopped, times)

        logger.info('Partition {} of {} loaded {} row(s)'.format(index + 1, count, loaded[0]), extra={'job': job, 'partition': index + 1})

        return loaded

    results: List[Tuple[int, Any]] = []
    failure: Optional[Exception] = None

    with ThreadPoolExecutor(max_workers=count, thread_name_prefix='bauta-partition') as executor:
        futures = [executor.submit(loadSlice, index, piece) for index, piece in enumerate(predicates)]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except _PartitionStopped:
                continue
            except Exception as error:
                if failure is None:
                    failure = error
                    stopped.set()

    if failure is not None:
        raise failure

    watermarks = [watermark for _, watermark in results if watermark is not None]

    return sum(rows for rows, _ in results), (max(watermarks) if watermarks else None)


def _pipelineDepth() -> int:
    """PIPELINE_DEPTH, or 0 to read, mask and write strictly in turn.

    On by default only with the native masker. Masking in Python takes long
    enough that overlapping it with the database gains nothing; natively it
    cuts a masked job's time by about a quarter. BAUTA_PIPELINE=1 or =0
    overrides the default.
    """

    setting = os.environ.get('BAUTA_PIPELINE')

    if setting is not None:
        return PIPELINE_DEPTH if setting == '1' else 0

    return PIPELINE_DEPTH if maskingModule.nativeVersion() is not None else 0


def _highestWatermark(chunk: Sequence[Sequence[Any]], index: Optional[int]) -> Any:
    """The largest value of the watermark column in one chunk, or None. Read
    from the raw rows, since a transform may reformat the column.
    """

    if index is None:
        return None

    highest = None
    for row in chunk:
        value = row[index]
        if value is not None and (highest is None or value > highest):
            highest = value

    return highest


def _streamChunks(chunks: Iterable[List[Tuple[Any, ...]]], prepare: Callable[[int, List[Tuple[Any, ...]]], List[Any]],
                  write: Callable[[List[Any], Any], None], watermarkIndex: Optional[int], depth: int) -> None:
    """Prepares (transforms and masks) each chunk and writes it, in source
    order. With `depth` above 0, preparing runs on one worker thread up to
    `depth` chunks ahead, overlapping the masker with the database.

    Both connections stay on the calling thread: mysqlclient, PyMySQL and
    sqlite3 refuse use from any other.
    """

    if depth == 0:
        for chunkIndex, chunk in enumerate(chunks):
            write(prepare(chunkIndex, chunk), _highestWatermark(chunk, watermarkIndex))
        return

    pending: Deque[Tuple['Future[List[Any]]', Any]] = collections.deque()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='bauta-rs') as executor:
        try:
            for chunkIndex, chunk in enumerate(chunks):
                pending.append((executor.submit(prepare, chunkIndex, chunk), _highestWatermark(chunk, watermarkIndex)))
                while len(pending) > depth:
                    future, watermark = pending.popleft()
                    write(future.result(), watermark)

            while pending:
                future, watermark = pending.popleft()
                write(future.result(), watermark)
        except BaseException:
            # Whatever is queued behind the failure is no longer wanted.
            executor.shutdown(wait=False, cancel_futures=True)
            raise


# What each driver says when a value doesn't fit the column it is written to.
# A masked value is the usual cause in a job that masks: `key` keeps an
# integer's digit count, which an INT column's range cuts across, and `number`
# varies a value that may already be at its column's limit.
_OUT_OF_RANGE = re.compile(r'out of range|overflow|too large|ORA-01438|ORA-01426', re.IGNORECASE)


def _noteIfMaskedValueDoesNotFit(error: Exception, job: str, table: str) -> None:
    """Says why a masked load overflowed a column, which the driver's own
    message doesn't: it names the column, not the mask that widened the value.
    """

    if _OUT_OF_RANGE.search(str(error)) is None:
        return

    logger.warning('{}: {} refused a value as out of its range, and this job masks. A mask can be wider than what it replaced: '
                   '`key` keeps an integer\'s digit count, so a 10-digit value can leave an INT column\'s range, and `number` '
                   'varies a value that may already be at its column\'s limit. Bound `number` with min and max, or widen the '
                   'column'.format(job, table), extra={'job': job})


# What each database says when statementTimeoutSeconds stops a statement:
# PostgreSQL, MySQL, MariaDB, oracledb -- DPY-4024 for a query, and for a
# PL/SQL call the line below, not DPY-4011 alone, which a dropped network
# raises too -- and SQL Server's cost limit. Not retried: the same query
# would run as long again, against the same source.
_STATEMENT_TIMEOUT = re.compile(r'canceling statement due to statement timeout|maximum statement execution time exceeded|'
                                r'max_statement_time exceeded|DPY-4024|socket timed out while recovering from previous socket timeout|'
                                r'estimated cost of this query .* exceeds the configured threshold')


# Deterministic errors, raised by this package, that a retry can't fix.
# Everything else is retried; see "Retries" in docs/concepts/how-it-works.md.
PERMANENT_ERRORS = (ConfigurationError, TransformError, TransformResolutionError, MaskingError, UnloadableValueError)


def _executeWithRetries(jobConfig: DataJobConfig, job: str, attempt: Callable[[], JobOutcome]) -> JobOutcome:
    """Runs `attempt` up to 1 + jobConfig.retries times with doubling backoff,
    returning its outcome, or a FAILED one carrying the last error.
    """

    for attemptNumber in range(1, jobConfig.retries + 2):

        try:
            return attempt()._replace(attempts=attemptNumber)

        except Exception as error:

            if isinstance(error, PERMANENT_ERRORS) or _STATEMENT_TIMEOUT.search(str(error)) or attemptNumber > jobConfig.retries:
                logger.error('Failed to complete {} due to error {}'.format(job, error), exc_info=error,
                             extra={'job': job, 'event': ATTEMPT_FAILED})
                loaded = error.rowCount if isinstance(error, PostLoadError) else 0
                return JobOutcome(job=job, status=JobStatus.FAILED, error=describeError(error), attempts=attemptNumber, rowCount=loaded)

            delay = min(MAXIMUM_RETRY_DELAY_SECONDS, jobConfig.retryDelaySeconds * (2 ** min(attemptNumber - 1, 32)))
            logger.warning(
                'Attempt {} of {} for {} failed ({}); retrying in {:.1f}s'.format(
                    attemptNumber, jobConfig.retries + 1, job, describeError(error), delay),
                extra={'job': job, 'attempt': attemptNumber, 'retryDelaySeconds': delay})
            time.sleep(delay)

    raise AssertionError('unreachable: the last attempt always returns')


def _runDataJob(job: str, jobConfig: DataJobConfig, connectionConfiguration: Dict[str, ConnectionConfig], memory: MemoryBackend) -> JobOutcome:
    """Runs one data job in a worker process, and records its success. The
    order of the records is what makes a crash safe; see "Crash safety" in
    docs/concepts/how-it-works.md.

    Nothing is recorded for a failed job. A failure to record is logged, not
    raised: the data landed, and the cost is an earlier re-run.
    """

    logger.info('Starting {}'.format(job))
    startedAt = time.time()
    watermark = None

    def attempt() -> JobOutcome:
        nonlocal watermark
        if isFullRefresh(jobConfig):
            # From the start, so the query returns every row; what it reads
            # up to is recorded below as for any run.
            watermark = jobConfig.watermarkInitial
        elif jobConfig.watermarkColumn:
            watermark = memory.readWatermarks().get(job, jobConfig.watermarkInitial)
        return _executeDataJob(job, jobConfig, connectionConfiguration, watermark=watermark)

    outcome = _executeWithRetries(jobConfig, job, attempt)

    if outcome.status == JobStatus.COMPLETED:

        if jobConfig.watermarkColumn and outcome.watermark is not None:
            try:
                memory.recordWatermark(job=job, value=outcome.watermark)
                logger.info('Advanced {} watermark to {!r}'.format(job, outcome.watermark))
            except Exception as error:
                logger.error('Completed {} but could not record its watermark -- the next run will re-extract from {!r}'.format(job, watermark),
                             exc_info=error)

        if jobConfig.masking is not None:
            try:
                memory.recordKeyFingerprint(job, maskingIdentity(jobConfig.masking.key.get_secret_value()))
            except Exception as error:
                logger.error('Completed {} but could not record its masking key fingerprint'.format(job), exc_info=error)

        try:
            memory.recordRun(job=job)
        except Exception as error:
            logger.error('Completed {} but could not record its run -- it will re-run before its refresh window is up'.format(job), exc_info=error)

        logger.info('Completed {} ({} row(s))'.format(job, outcome.rowCount),
                    extra={'job': job, 'status': outcome.status.value, 'rowCount': outcome.rowCount, 'attempts': outcome.attempts})

    return outcome._replace(startedAt=startedAt, finishedAt=time.time())

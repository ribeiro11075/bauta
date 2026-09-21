"""One data job, run to completion in its own process: rows streamed from
the source, transformed, masked and loaded a chunk at a time, retried on a
database error, and its success recorded in the order that keeps a crash
safe. See "How a data job moves rows" in docs/design.md.
"""
from __future__ import annotations

import collections
import logging
import os
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from ..configuration import ConfigurationError, DatabaseConnectionConfig, DataJobConfig, InsertStrategy
from ..database import Database
from ..log import LOGGER_NAME
from ..log.scrubbing import describeError
from ..masking import BoundMasking, MaskingError, MaskingPlan, maskingIdentity
from ..masking import core as maskingModule
from ..transform import Transform, Transformer, TransformError, TransformResolutionError, resolveTransformer
from .dependencyGraph import JobOutcome, JobStatus
from .memory import MemoryBackend

logger = logging.getLogger(LOGGER_NAME)


# How many chunks may be masked ahead of the one being written. One already
# keeps the reader, masker and writer all busy, holding three chunks at once;
# more buys no overlap and costs a chunk of memory each. (It also beats larger
# chunks: at 10 ms a round trip, three chunks of 5,000 rows took 2.24s against
# 2.63s for one of 20,000.)
PIPELINE_DEPTH = 1


# Caps the doubling backoff, which would otherwise wait 5.7 hours in all over
# `retries: 12`.
MAXIMUM_RETRY_DELAY_SECONDS = 300.0


def _bindMasking(job: str, jobConfig: DataJobConfig, columns: List[str]) -> Optional[BoundMasking]:
    """Binds the job's masking policy to the columns its query returned, raising
    MaskingError before anything is written if it doesn't cover them all.
    """

    if jobConfig.masking is None:
        return None

    plan = MaskingPlan(key=jobConfig.masking.key.get_secret_value(), columns=jobConfig.masking.columns,
                       defaultStrategy=jobConfig.masking.defaultStrategy)
    bound = plan.bind(columns)

    logger.info('Masking {} column(s) under key {}'.format(len(columns), plan.fingerprint), extra={'job': job, 'keyFingerprint': plan.fingerprint})
    for entry in bound.manifest:
        logger.debug('Masking {} with {}{}'.format(entry.column, entry.strategy, ' in domain {}'.format(entry.domain) if entry.domain else ''))

    return bound


def _executeDataJob(job: str, jobConfig: DataJobConfig, databaseConfiguration: Dict[str, DatabaseConnectionConfig], watermark: Any = None) -> JobOutcome:
    """Runs one data job to completion, raising on failure. See "How a data job
    moves rows" in docs/design.md.

    Transforms and the masking policy are both checked against the query's
    columns before the first write, so a misconfigured job fails with nothing
    loaded. Masking runs after transforms, so values are normalized before
    they are keyed.
    """

    columnTransforms: Dict[str, List[Transformer]] = {
        column: [resolveTransformer(reference) for reference in references] for column, references in jobConfig.sourceQueryColumnTransforms.items()
        }

    with Database(connectionSettings=databaseConfiguration[jobConfig.sourceDatabase]) as sourceDatabase, \
         Database(connectionSettings=databaseConfiguration[jobConfig.targetDatabase]) as targetDatabase:

        sourceQuery = jobConfig.sourceQuery
        parameters = None

        if jobConfig.watermarkColumn:
            sourceQuery = sourceDatabase.substituteWatermarkPlaceholder(sourceQuery)
            parameters = (watermark,)
            logger.info('Extracting {} incrementally, from watermark {!r}'.format(jobConfig.sourceDatabase, watermark))

        logger.debug('Streaming sourceQuery against {} in chunks of {}'.format(jobConfig.sourceDatabase, jobConfig.chunkSize))
        sourceQueryColumns, chunks = sourceDatabase.stream(query=sourceQuery, chunkSize=jobConfig.chunkSize, parameters=parameters)
        logger.debug('sourceQuery returned columns: {}'.format(sourceQueryColumns))

        watermarkIndex = None

        if jobConfig.watermarkColumn:
            if jobConfig.watermarkColumn not in sourceQueryColumns:
                raise ConfigurationError(
                    'watermarkColumn "{}" is not among the columns sourceQuery returns {} -- '
                    'the job cannot tell how far it got'.format(jobConfig.watermarkColumn, sourceQueryColumns))
            watermarkIndex = sourceQueryColumns.index(jobConfig.watermarkColumn)

        transform = Transform(columns=sourceQueryColumns, columnTransforms=columnTransforms)
        transform.validate()
        if columnTransforms:
            logger.debug('Applying transforms to column(s): {}'.format(', '.join(columnTransforms)))

        masking = _bindMasking(job, jobConfig, sourceQueryColumns)

        if masking is not None and watermarkIndex is not None and not masking.strategies[watermarkIndex].PASSTHROUGH:
            # Validation refuses this too; asked again here, before a row is
            # read, since a configuration copied with model_copy skips it.
            raise MaskingError('watermarkColumn "{}" is masked with {}, and the watermark is read before masking and kept in run state '
                               'and logs, so it would leak the unmasked value'.format(
                                   jobConfig.watermarkColumn, masking.manifest[watermarkIndex].strategy))

        columns = jobConfig.targetColumns or targetDatabase.getAllColumnNames(table=jobConfig.targetTableFinal)
        logger.debug('Resolved target columns for {}: {}'.format(jobConfig.targetTableFinal, columns))

        if not jobConfig.targetColumns and len(columns) != len(sourceQueryColumns):
            # The load binds by position, so the two lists must line up. The
            # driver's own complaint names neither the table nor the columns:
            # "the current statement uses 5, and there are 3 supplied".
            raise ConfigurationError(
                'sourceQuery returns {} column(s) {} and {} has {} ({}). List the ones the query fills in targetColumns, in the '
                'query\'s order'.format(len(sourceQueryColumns), sourceQueryColumns, jobConfig.targetTableFinal, len(columns),
                                        ', '.join(columns)))

        if jobConfig.insertStrategy == InsertStrategy.UPSERT and not targetDatabase.getPrimaryColumnNames(table=jobConfig.targetTableFinal):
            # Asked before anything is written, not when the first chunk is
            # upserted: the job used to run its preTargetAdhocQueries and load
            # every row into the stage table before finding this out.
            raise ConfigurationError('{} has no primary key, so an upsert cannot match its rows -- add one, or use '
                                     'insertStrategy: swap'.format(jobConfig.targetTableFinal))

        for preTargetAdhocQuery in jobConfig.preTargetAdhocQueries:
            logger.debug('Running preTargetAdhocQuery: {}'.format(preTargetAdhocQuery))
            targetDatabase.alter(preTargetAdhocQuery)

        if jobConfig.targetTableStage:
            logger.debug('Truncating stage table {}'.format(jobConfig.targetTableStage))
            targetDatabase.truncate(table=jobConfig.targetTableStage)

        loadTable = jobConfig.targetTableStage or jobConfig.targetTableFinal
        streamsDirectlyIntoTarget = jobConfig.insertStrategy == InsertStrategy.UPSERT and not jobConfig.targetTableStage

        logger.info('Loading into {} a chunk at a time'.format(loadTable))

        rowCount = 0
        highWatermark = None

        def writeChunk(rows: List[Any], watermark: Any) -> None:
            nonlocal rowCount, highWatermark

            if streamsDirectlyIntoTarget:
                targetDatabase.upsert(table=loadTable, data=rows, chunkSize=jobConfig.chunkSize, columns=columns)
            else:
                targetDatabase.insert(table=loadTable, data=rows, chunkSize=jobConfig.chunkSize, columns=columns)

            rowCount += len(rows)
            # Only once the rows have landed, or a failed job's next run would
            # start past them.
            if watermark is not None and (highWatermark is None or watermark > highWatermark):
                highWatermark = watermark

            logger.debug('Loaded {} row(s) into {} ({} so far)'.format(len(rows), loadTable, rowCount))

        def prepareChunk(chunkIndex: int, chunk: List[Tuple[Any, ...]]) -> List[Any]:
            rows = transform.apply(chunk)

            if masking is not None:
                # By read order, not masking order: `shuffle` keys on it.
                rows = masking.apply(rows, chunkIndex=chunkIndex)

            return rows

        try:
            _streamChunks(chunks, prepareChunk, writeChunk, watermarkIndex, _pipelineDepth())
        except Exception as error:
            if masking is not None:
                _noteIfMaskedValueDoesNotFit(error, job, loadTable)
            raise

        logger.info('Streamed {} row(s) from {} into {}'.format(rowCount, jobConfig.sourceDatabase, loadTable))

        if jobConfig.insertStrategy == InsertStrategy.SWAP:
            assert jobConfig.targetTableStage is not None
            logger.info('Swapping {} with stage table {}'.format(jobConfig.targetTableFinal, jobConfig.targetTableStage))
            targetDatabase.swap(targetTable=jobConfig.targetTableFinal, stageTable=jobConfig.targetTableStage)

            if masking is not None:
                # The swap moved what the target held into the stage. For a job
                # masking in place that is the unmasked original, which must not
                # stay readable beside the masked copy.
                logger.info('Emptying stage table {}, which now holds what {} held before the swap'.format(
                    jobConfig.targetTableStage, jobConfig.targetTableFinal))
                targetDatabase.truncate(table=jobConfig.targetTableStage)

        if jobConfig.insertStrategy == InsertStrategy.UPSERT and jobConfig.targetTableStage:
            logger.info('Upserting {} from stage table {}'.format(jobConfig.targetTableFinal, jobConfig.targetTableStage))
            targetDatabase.upsertFromStage(targetTable=jobConfig.targetTableFinal, stageTable=jobConfig.targetTableStage, columns=columns)

        for postTargetAdhocQuery in jobConfig.postTargetAdhocQueries:
            logger.debug('Running postTargetAdhocQuery: {}'.format(postTargetAdhocQuery))
            try:
                targetDatabase.alter(postTargetAdhocQuery)
            except Exception as error:
                raise PostLoadError(postTargetAdhocQuery, error, rowCount, jobConfig.targetTableFinal) from error

    maskingApplied = None
    if masking is not None:
        maskingApplied = {'columns': [entry._asdict() for entry in masking.manifest]}

    return JobOutcome(job=job, status=JobStatus.COMPLETED, rowCount=rowCount, watermark=highWatermark, masking=maskingApplied)


def _pipelineDepth() -> int:
    """PIPELINE_DEPTH, or 0 to read, mask and write strictly in turn.

    On by default only with the native masker, the only place it pays:

        200,000 rows, 6 masked columns, 5 ms round trip each way

        Python masking, in turn      11.57s
        Python masking, overlapped   11.79s     0.98x
        native masking, in turn       2.92s     3.96x
        native masking, overlapped    2.16s     5.35x

    BAUTA_PIPELINE=1 or =0 overrides the default.
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


class PostLoadError(Exception):
    """A postTargetAdhocQuery that failed after the rows were already in place.

    Carries the row count so the failure says how many rows the target holds,
    rather than the nothing a failure usually loaded: a swap that has happened
    has already replaced the target, and reporting 0 rows against a copy that
    had just been rebuilt sent people looking in the wrong place.
    """

    def __init__(self, query: str, error: Exception, rowCount: int, targetTable: str) -> None:
        super().__init__('the load finished and {} holds its {} row(s), but a postTargetAdhocQuery failed -- {}: {}'.format(
            targetTable, rowCount, query, describeError(error)))
        self.rowCount = rowCount


# Deterministic errors, raised by this package, that a retry can't fix.
# Everything else is retried; see "Retries" in docs/design.md.
PERMANENT_ERRORS = (ConfigurationError, TransformError, TransformResolutionError, MaskingError)


def _executeWithRetries(jobConfig: DataJobConfig, job: str, attempt: Callable[[], JobOutcome]) -> JobOutcome:
    """Runs `attempt` up to 1 + jobConfig.retries times with doubling backoff,
    returning its outcome, or a FAILED one carrying the last error.
    """

    for attemptNumber in range(1, jobConfig.retries + 2):

        try:
            return attempt()._replace(attempts=attemptNumber)

        except Exception as error:

            if isinstance(error, PERMANENT_ERRORS) or attemptNumber > jobConfig.retries:
                logger.error('Failed to complete {} due to error {}'.format(job, error), exc_info=error)
                loaded = error.rowCount if isinstance(error, PostLoadError) else 0
                return JobOutcome(job=job, status=JobStatus.FAILED, error=describeError(error), attempts=attemptNumber, rowCount=loaded)

            delay = min(MAXIMUM_RETRY_DELAY_SECONDS, jobConfig.retryDelaySeconds * (2 ** min(attemptNumber - 1, 32)))
            logger.warning(
                'Attempt {} of {} for {} failed ({}); retrying in {:.1f}s'.format(
                    attemptNumber, jobConfig.retries + 1, job, describeError(error), delay),
                extra={'job': job, 'attempt': attemptNumber, 'retryDelaySeconds': delay})
            time.sleep(delay)

    raise AssertionError('unreachable: the last attempt always returns')


def _runDataJob(job: str, jobConfig: DataJobConfig, databaseConfiguration: Dict[str, DatabaseConnectionConfig], memory: MemoryBackend) -> JobOutcome:
    """Runs one data job in a worker process, and records its success. The
    order of the records is what makes a crash safe; see "Crash safety" in
    docs/design.md.

    Nothing is recorded for a failed job. A failure to record is logged, not
    raised: the data landed, and the cost is an earlier re-run.
    """

    logger.info('Starting {}'.format(job))
    startedAt = time.time()
    watermark = None

    def attempt() -> JobOutcome:
        nonlocal watermark
        if jobConfig.watermarkColumn:
            watermark = memory.readWatermarks().get(job, jobConfig.watermarkInitial)
        return _executeDataJob(job, jobConfig, databaseConfiguration, watermark=watermark)

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

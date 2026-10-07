"""Where a data job's rows land: what the pipeline asks of a target, and the
target that is a table in a database. Files are the other kind; see
bauta.lake.

The pipeline reads, transforms and masks; a target decides what loading
means. A table loads each chunk in a transaction of its own and swaps or
upserts at the end; a file target writes parts nobody can see until it
publishes them.
"""
from __future__ import annotations

import contextlib
import logging
import os
import signal
import threading
from typing import Any, ContextManager, Generator, List, Optional, Sequence

from ..configuration import ConfigurationError, DataJobConfig, InsertStrategy
from ..database import Database
from ..log import LOGGER_NAME
from ..log.scrubbing import describeError

logger = logging.getLogger(LOGGER_NAME)


class PostLoadError(Exception):
    """A postTargetAdhocQuery that failed after the rows were already in place.

    Carries the row count, so the failure says how many rows the target holds:
    a swap that has happened has already replaced the target, which a failure
    reporting no rows would hide.
    """

    def __init__(self, query: str, error: Exception, rowCount: int, targetTable: str) -> None:
        super().__init__('the load finished and {} holds its {} row(s), but a postTargetAdhocQuery failed -- {}: {}'.format(
            targetTable, rowCount, query, describeError(error)))
        self.rowCount = rowCount


@contextlib.contextmanager
def _terminationDeferred() -> Generator[None, None, None]:
    """SIGTERM held until the block ends, then acted on as it would have
    been. A swap's renames commit one at a time on Oracle, and a job stopped
    for exceeding timeoutSeconds is sent SIGTERM: arriving between two of
    them, it left the target missing. The runner kills a job that hasn't
    exited within its grace period, so a swap still running then can be cut
    short, which recoverInterruptedSwap puts right on the next run.

    Only on the main thread, where Python lets a handler be set; a job runs
    its target there.
    """

    if threading.current_thread() is not threading.main_thread():
        yield
        return

    received: List[int] = []
    previous = signal.signal(signal.SIGTERM, lambda number, frame: received.append(number))
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
        if received:
            logger.warning('Stopping now that the swap has finished, as asked while it ran')
            os.kill(os.getpid(), signal.SIGTERM)


class LoadTarget:
    """What the pipeline asks of wherever a job's rows go, in this order:
    begin() once the source's columns are known, write() for each prepared
    chunk in source order, and finish() after the last. abort() is called
    instead of finish() when the job fails part-way.

    `loadName` names where the chunks are written, for logs and errors.
    """

    loadName: str

    def begin(self, sourceColumns: List[str], description: Optional[Sequence[Sequence[Any]]] = None) -> None:
        """Checks the target against the columns the query returns, and
        prepares it, raising ConfigurationError before anything is written.
        `description` is the query's cursor.description, where the source has
        one.
        """

        raise NotImplementedError

    def holdJson(self, indexes: Sequence[int]) -> None:
        """Says which of the columns begin() was given hold JSON text --
        a PostgreSQL, MySQL or DuckDB JSON column's, kept as it came or
        masked and encoded as JSON (see pipeline._encodeJson) -- for a target
        that writes JSON differently from text. Nothing, for a database.
        """

    def write(self, rows: List[Any]) -> None:

        raise NotImplementedError

    def finish(self, rowCount: int) -> None:
        """Makes what was written the target's, once every row is written."""

        raise NotImplementedError

    def abort(self) -> None:
        """Best-effort cleanup after a failure; must not raise."""

    def openWriter(self) -> ContextManager['ChunkWriter']:
        """Another writer into what begin() prepared, with a connection of its
        own, for one of a job's partitions to write() through while the rest
        write through theirs. Called after begin() and closed before finish().
        """

        raise ConfigurationError('partitions is for a table in a database; {} is written by one writer'.format(self.loadName))

    def close(self) -> None:
        """Lets go of what the target holds open, after finish() or abort()."""


class ChunkWriter:
    """Writes prepared chunks into a table on one connection: upserted, for a
    stage-less upsert straight into the target, and otherwise inserted.
    """

    def __init__(self, database: Database, table: str, columns: List[str], chunkSize: int, upserts: bool) -> None:
        self.database = database
        self.table = table
        self.columns = columns
        self.chunkSize = chunkSize
        self.upserts = upserts


    def write(self, rows: List[Any]) -> None:

        if self.upserts:
            self.database.upsert(table=self.table, data=rows, chunkSize=self.chunkSize, columns=self.columns)
        else:
            self.database.insert(table=self.table, data=rows, chunkSize=self.chunkSize, columns=self.columns)


class TableTarget(LoadTarget):
    """A table in a database, the Database already open. Each chunk commits on
    its own, so abort() has nothing to undo: what a stage-less upsert wrote
    stays written, as "How a data job moves rows" in docs/concepts/how-it-works.md says.
    """

    def __init__(self, database: Database, jobConfig: DataJobConfig) -> None:
        self.database = database
        self.jobConfig = jobConfig
        self.loadName = jobConfig.targetTableStage or jobConfig.targetTableFinal
        self.columns: List[str] = []
        self._streamsDirectlyIntoTarget = jobConfig.insertStrategy == InsertStrategy.UPSERT and not jobConfig.targetTableStage


    def begin(self, sourceColumns: List[str], description: Optional[Sequence[Sequence[Any]]] = None) -> None:

        jobConfig = self.jobConfig

        if jobConfig.insertStrategy == InsertStrategy.SWAP and not jobConfig.targetTableStage:
            # Validation says so against connections.yaml; asked again here for
            # a caller that built the job without it.
            raise ConfigurationError('targetTableStage is required when insertStrategy is swap')

        if jobConfig.insertStrategy == InsertStrategy.SWAP:
            # First, since a swap stopped after its second rename left no
            # target for the columns below to be read from.
            assert jobConfig.targetTableStage is not None
            recovered = self.database.recoverInterruptedSwap(jobConfig.targetTableFinal, jobConfig.targetTableStage)
            if recovered is not None:
                logger.warning(recovered)

        columns = jobConfig.targetColumns or self.database.getAllColumnNames(table=jobConfig.targetTableFinal)
        logger.debug('Resolved target columns for {}: {}'.format(jobConfig.targetTableFinal, columns))

        if not jobConfig.targetColumns and len(columns) != len(sourceColumns):
            # The load binds by position, so the two lists must line up, and
            # the driver's own complaint names neither the table nor the columns.
            raise ConfigurationError(
                'sourceQuery returns {} column(s) {} and {} has {} ({}). List the ones the query fills in targetColumns, in the '
                'query\'s order'.format(len(sourceColumns), sourceColumns, jobConfig.targetTableFinal, len(columns),
                                        ', '.join(columns)))

        if jobConfig.insertStrategy == InsertStrategy.UPSERT and not self.database.getPrimaryColumnNames(table=jobConfig.targetTableFinal):
            # Asked before anything is written -- preTargetAdhocQueries, the
            # stage table -- rather than when the first chunk is upserted.
            raise ConfigurationError('{} has no primary key, so an upsert cannot match its rows -- add one, or use '
                                     'insertStrategy: swap'.format(jobConfig.targetTableFinal))

        self.columns = columns

        for preTargetAdhocQuery in jobConfig.preTargetAdhocQueries:
            logger.debug('Running preTargetAdhocQuery: {}'.format(preTargetAdhocQuery))
            self.database.alter(preTargetAdhocQuery)

        if jobConfig.targetTableStage:
            logger.debug('Truncating stage table {}'.format(jobConfig.targetTableStage))
            self.database.truncate(table=jobConfig.targetTableStage)

        if jobConfig.insertStrategy == InsertStrategy.SWAP:
            assert jobConfig.targetTableStage is not None
            self._giveStageTheTargetsKeys(jobConfig.targetTableFinal, jobConfig.targetTableStage)
            self._giveStageTheTargetsAccess(jobConfig.targetTableFinal, jobConfig.targetTableStage)


    def _giveStageTheTargetsKeys(self, final: str, stage: str) -> None:
        """The swap makes the stage table the target, so it is given the
        target's primary key and unique keys first, while it is empty: then
        the live table has them after every swap, not every other one, and a
        repeated key fails the load rather than reaching the target.

        A stage that already has them -- every stage after the first swap,
        which is the previous target -- is left as it is. A key that can't be
        added is a warning, not a failure: the swap still works as it did
        before stages were given keys.
        """

        try:
            added = self.database.copyKeys(fromTable=final, toTable=stage)
        except Exception as error:
            logger.warning('Could not give stage table {} the keys of {}, so after this swap {} lacks them until the next one: {}'.format(
                stage, final, final, describeError(error)))
            return

        if added:
            logger.info('Gave stage table {} the {} of {}, so the swap keeps them'.format(stage, ' and '.join(added), final))


    def _giveStageTheTargetsAccess(self, final: str, stage: str) -> None:
        """The target's plain indexes and table grants, given to the stage as
        its keys are, for the same reason: what only the target had was there
        after every other swap. Who could read the copy changed from one run
        to the next. One that can't be given is a warning: the swap goes on as
        it did before.
        """

        try:
            given, problems = self.database.copyAccess(fromTable=final, toTable=stage)
        except Exception as error:
            logger.warning('Could not read the indexes and grants of {} to give stage table {}, so after this swap {} may lack them '
                           'until the next one: {}'.format(final, stage, final, describeError(error)))
            return

        if given:
            logger.info('Gave stage table {} the {} of {}, so the swap keeps them'.format(stage, ', '.join(given), final))
        for problem in problems:
            logger.warning('Could not give stage table {} the {} of {}, so after this swap {} lacks it until the next one'.format(
                stage, problem, final, final))


    def write(self, rows: List[Any]) -> None:

        ChunkWriter(self.database, self.loadName, self.columns, self.jobConfig.chunkSize, self._streamsDirectlyIntoTarget).write(rows)


    @contextlib.contextmanager
    def openWriter(self) -> Generator[ChunkWriter, None, None]:
        """A connection of its own, since a connection is used by one thread at
        a time -- SQLite and the MySQL drivers refuse any other -- and each
        partition commits its chunks as this target does.
        """

        with Database(connectionSettings=self.database.connectionSettings, create=True) as database:
            yield ChunkWriter(database, self.loadName, self.columns, self.jobConfig.chunkSize, self._streamsDirectlyIntoTarget)


    def finish(self, rowCount: int) -> None:

        jobConfig = self.jobConfig

        if jobConfig.insertStrategy == InsertStrategy.SWAP:
            assert jobConfig.targetTableStage is not None
            logger.info('Swapping {} with stage table {}'.format(jobConfig.targetTableFinal, jobConfig.targetTableStage))
            with _terminationDeferred():
                self.database.swap(targetTable=jobConfig.targetTableFinal, stageTable=jobConfig.targetTableStage)

            if jobConfig.masking is not None:
                # The swap moved what the target held into the stage. For a job
                # masking in place that is the unmasked original, which must not
                # stay readable beside the masked copy.
                logger.info('Emptying stage table {}, which now holds what {} held before the swap'.format(
                    jobConfig.targetTableStage, jobConfig.targetTableFinal))
                self.database.truncate(table=jobConfig.targetTableStage)

        if jobConfig.insertStrategy == InsertStrategy.UPSERT and jobConfig.targetTableStage:
            logger.info('Upserting {} from stage table {}'.format(jobConfig.targetTableFinal, jobConfig.targetTableStage))
            self.database.upsertFromStage(targetTable=jobConfig.targetTableFinal, stageTable=jobConfig.targetTableStage, columns=self.columns)

        for postTargetAdhocQuery in jobConfig.postTargetAdhocQueries:
            logger.debug('Running postTargetAdhocQuery: {}'.format(postTargetAdhocQuery))
            try:
                self.database.alter(postTargetAdhocQuery)
            except Exception as error:
                raise PostLoadError(postTargetAdhocQuery, error, rowCount, jobConfig.targetTableFinal) from error

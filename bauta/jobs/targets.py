"""Where a data job's rows land: what the pipeline asks of a target, and the
target that is a table in a database. Files are the other kind; see
bauta.files.

The pipeline reads, transforms and masks; a target decides what loading
means. A table loads each chunk in a transaction of its own and swaps or
upserts at the end; a file target writes parts nobody can see until it
publishes them.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence

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

    def write(self, rows: List[Any]) -> None:

        raise NotImplementedError

    def finish(self, rowCount: int) -> None:
        """Makes what was written the target's, once every row is written."""

        raise NotImplementedError

    def abort(self) -> None:
        """Best-effort cleanup after a failure; must not raise."""

    def close(self) -> None:
        """Lets go of what the target holds open, after finish() or abort()."""


class TableTarget(LoadTarget):
    """A table in a database, the Database already open. Each chunk commits on
    its own, so abort() has nothing to undo: what a stage-less upsert wrote
    stays written, as "How a data job moves rows" in docs/design.md says.
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


    def write(self, rows: List[Any]) -> None:

        if self._streamsDirectlyIntoTarget:
            self.database.upsert(table=self.loadName, data=rows, chunkSize=self.jobConfig.chunkSize, columns=self.columns)
        else:
            self.database.insert(table=self.loadName, data=rows, chunkSize=self.jobConfig.chunkSize, columns=self.columns)


    def finish(self, rowCount: int) -> None:

        jobConfig = self.jobConfig

        if jobConfig.insertStrategy == InsertStrategy.SWAP:
            assert jobConfig.targetTableStage is not None
            logger.info('Swapping {} with stage table {}'.format(jobConfig.targetTableFinal, jobConfig.targetTableStage))
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

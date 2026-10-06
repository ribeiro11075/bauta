"""A table of files -- Parquet, CSV or JSON Lines -- as a data job's target,
on this machine or in a cloud. See "Files as a target" in docs/concepts/how-it-works.md.

A job writes its parts where no reader looks -- `_bauta_staging/<run>/`
under the connection's root -- and publishes them once every row is
written: append moves them into the table's directory beside what is there,
overwrite into a new `snapshot=<run>/` directory, marked complete by a
`_SUCCESS` file, and singleFile over `<table>.<extension>`. A job that fails
leaves nothing a reader sees.

Staging matters more in a cloud than on disk. An upload pyarrow is made to
stop is completed, not abandoned -- it has no way to abort one -- so a part
written in place would appear, cut short, when its job failed.
"""
from __future__ import annotations

import datetime
import json
import logging
import posixpath
from typing import Any, Dict, List, Optional, Tuple

from ..configuration import DataJobConfig, FilesConnection, InsertStrategy
from ..log import LOGGER_NAME
from ..log.scrubbing import describeError
from .columnar import ColumnarTarget, newRunId
from .formats import Parts, PartSettings, extension
from .stores import Store

logger = logging.getLogger(LOGGER_NAME)

# Under the root, where a run's parts are written before they are published.
# Spark, Hive and pyarrow skip a name beginning with an underscore.
STAGING_DIRECTORY = '_bauta_staging'

SNAPSHOT_PREFIX = 'snapshot='

# In each complete snapshot: what the run wrote. Its presence is what marks
# the snapshot complete.
SUCCESS_FILE = '_SUCCESS'


class FileTarget(ColumnarTarget):
    """A table of files under a files connection's root; nothing is visible
    until finish() publishes the run's parts.
    """

    def __init__(self, job: str, jobConfig: DataJobConfig, settings: FilesConnection) -> None:
        super().__init__(job, jobConfig, settings.rowGroupSize)
        self.settings = settings
        self.store = Store(settings)
        self.partSettings = PartSettings(settings.format, settings.effectiveCompression(), settings.fileSize, settings.delimiter)
        self.tablePath = self.store.path(jobConfig.targetTableFinal)
        self.singleFilePath = self.tablePath + extension(self.partSettings)
        self.runId = newRunId()
        self.stagingPath = self.store.path(STAGING_DIRECTORY, self.runId)
        self.loadName = self.store.location(self.singleFilePath if jobConfig.singleFile else self.tablePath + '/')
        self._parts: Optional[Parts] = None


    def _prepare(self) -> None:

        self.store.ensureDirectory(self.stagingPath)
        logger.debug('Writing {} parts in {}'.format(self.job, self.store.location(self.stagingPath)))


    def _newParts(self) -> Parts:

        jsonColumns = {column.name for column in self.columns if column.documents}
        rawColumns = {column.name for column in self.columns if column.jsonText}

        return Parts(self.store, self.stagingPath, self.runId, self._settleSchema(), self.partSettings, jsonColumns, self.jobConfig.singleFile,
                     rawColumns)


    def _writeTable(self, table: Any) -> None:

        if self._parts is None:
            self._parts = self._newParts()
        self._parts.write(table)


    def finish(self, rowCount: int) -> None:

        self._flush()
        if self._parts is None:
            if self.jobConfig.insertStrategy == InsertStrategy.APPEND:
                logger.info('{}: no rows, so nothing to append to {}'.format(self.job, self.loadName))
                self.store.deleteDirectory(self.stagingPath)
                return
            self._parts = self._newParts()

        parts = self._parts.close()

        if self.jobConfig.singleFile:
            self._publishSingleFile(parts)
        elif self.jobConfig.insertStrategy == InsertStrategy.APPEND:
            self._publishInto(self.tablePath, parts)
            logger.info('Appended {} part(s), {} row(s), to {}'.format(len(parts), rowCount, self.loadName))
        else:
            self._publishSnapshot(parts, rowCount)

        self._removeStaging()


    def _publishInto(self, directory: str, parts: List[Tuple[str, int]]) -> List[Dict[str, Any]]:

        self.store.ensureDirectory(directory)
        published = []
        for path, rows in parts:
            destination = posixpath.join(directory, posixpath.basename(path))
            self.store.move(path, destination)
            published.append({'file': posixpath.basename(destination), 'rows': rows})

        return published


    def _publishSingleFile(self, parts: List[Tuple[str, int]]) -> None:

        (path, rows), = parts
        self.store.ensureDirectory(posixpath.dirname(self.singleFilePath))
        # A rename over the old file locally, and on S3 a copy that replaces
        # the object when it completes: a reader gets the old one or the new.
        self.store.move(path, self.singleFilePath)
        logger.info('Replaced {} with {} row(s)'.format(self.loadName, rows))


    def _publishSnapshot(self, parts: List[Tuple[str, int]], rowCount: int) -> None:

        snapshot = posixpath.join(self.tablePath, SNAPSHOT_PREFIX + self.runId)
        published = self._publishInto(snapshot, parts)

        success = {
            'job': self.job, 'run': self.runId, 'rows': rowCount, 'format': self.settings.format.value,
            'compression': self.settings.effectiveCompression().value, 'files': published,
            'columns': [{'name': column.name, 'type': str(column.type)} for column in self.columns],
            'publishedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
            }
        self.store.writeBytes(posixpath.join(snapshot, SUCCESS_FILE), (json.dumps(success, indent=2) + '\n').encode('utf-8'))

        logger.info('Published snapshot {} of {}: {} part(s), {} row(s)'.format(self.runId, self.loadName, len(parts), rowCount))
        self._pruneSnapshots()


    def snapshots(self) -> List[Tuple[str, bool]]:
        """The table's snapshot directories, oldest first, each with whether it
        is complete.
        """

        found = [name for name in self.store.directories(self.tablePath) if name.startswith(SNAPSHOT_PREFIX)]

        return [(name, self.store.exists(posixpath.join(self.tablePath, name, SUCCESS_FILE))) for name in found]


    def _pruneSnapshots(self) -> None:
        """Keeps the newest `keepSnapshots` complete snapshots, and removes the
        incomplete ones older than this run's: runs that failed while
        publishing. A newer incomplete one is another run's, still publishing.
        """

        current = SNAPSHOT_PREFIX + self.runId
        snapshots = self.snapshots()
        complete = [name for name, done in snapshots if done]
        stale = complete[:-self.settings.keepSnapshots] + [name for name, done in snapshots if not done and name < current]

        for name in stale:
            try:
                self.store.deleteDirectory(posixpath.join(self.tablePath, name))
                logger.debug('Removed snapshot {} of {}'.format(name, self.loadName))
            except Exception as error:
                # The new snapshot is published; an old one left is untidy, not wrong.
                logger.warning('{}: could not remove old snapshot {} of {} -- {}'.format(self.job, name, self.loadName, describeError(error)), extra={'job': self.job})


    def _removeStaging(self) -> None:

        try:
            self.store.deleteDirectory(self.stagingPath)
        except Exception as error:
            logger.warning('{}: could not remove its staging directory {} -- {}'.format(
                self.job, self.store.location(self.stagingPath), error), extra={'job': self.job})


    def abort(self) -> None:
        """What a failed run wrote is in staging only; remove it."""

        if self._parts is not None:
            self._parts.discard()
        self._buffered = []
        self._removeStaging()


def checkWritable(settings: FilesConnection) -> None:
    """Raises unless a job could write under `settings`' root: a file is
    written where a staging directory would go, listed, and removed. Locally
    the root is created if missing; on S3 the bucket must exist.
    """

    store = Store(settings)
    probeDirectory = store.path(STAGING_DIRECTORY)
    probe = posixpath.join(probeDirectory, 'probe-{}'.format(newRunId()))

    store.ensureDirectory(probeDirectory)
    store.writeBytes(probe, b'bauta')
    # Publishing an overwrite lists the table's snapshots, so listing is asked too.
    store.directories(store.root)
    store.deleteFile(probe)

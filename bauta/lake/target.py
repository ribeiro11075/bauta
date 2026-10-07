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

# In a run's staging directory while it appends: the table and every file it
# is moving into it, written before the first move and removed after the
# last. A run killed in between leaves it, and the next run of the table
# takes those files back out; see FileTarget._takeBackInterruptedAppends.
PUBLISHING_FILE = '_PUBLISHING'


class FileTarget(ColumnarTarget):
    """A table of files under a files connection's root; nothing is visible
    until finish() publishes the run's parts.

    An append moves its parts into the table one at a time, so a run failing
    among the moves has published some of them, and the next run, starting
    from the same watermark, would append those rows again. abort() takes
    back what this run moved, and a run killed outright is undone by the
    next run of the table from its PUBLISHING_FILE.
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
        # The files this run has moved into the table, for abort() to take back.
        self._published: List[str] = []


    def _prepare(self) -> None:

        self._takeBackInterruptedAppends()
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
            self._publishInto(self.tablePath, parts, intent=True)
            logger.info('Appended {} part(s), {} row(s), to {}'.format(len(parts), rowCount, self.loadName))
        else:
            self._publishSnapshot(parts, rowCount)

        self._removeStaging()


    def _publishInto(self, directory: str, parts: List[Tuple[str, int]], intent: bool = False) -> List[Dict[str, Any]]:
        """Moves the parts into `directory`. With `intent`, for an append, the
        files are first listed in PUBLISHING_FILE, which is removed once the
        last is moved. A snapshot needs none: it is invisible until its
        _SUCCESS is written, and an intent outliving that would take back a
        complete snapshot.
        """

        self.store.ensureDirectory(directory)
        destinations = [posixpath.join(directory, posixpath.basename(path)) for path, _ in parts]
        intentPath = posixpath.join(self.stagingPath, PUBLISHING_FILE)
        if intent:
            self.store.writeBytes(intentPath, json.dumps({'job': self.job, 'table': self.tablePath, 'files': destinations}).encode('utf-8'))

        published = []
        for (path, rows), destination in zip(parts, destinations):
            self.store.move(path, destination)
            self._published.append(destination)
            published.append({'file': posixpath.basename(destination), 'rows': rows})

        if intent:
            self.store.deleteFile(intentPath)

        return published


    def _takeBackInterruptedAppends(self) -> None:
        """Removes from the table what an earlier append moved into it before
        it was killed, which its PUBLISHING_FILE lists, and that run's staging
        directory with it. Only this job's: no two runs of one job overlap, so
        its intent is a dead run's, where another job appending to the same
        table may be publishing now -- taking that one's back deleted the parts
        it had just published.

        That run recorded nothing, so this one starts from the same watermark
        and appends those rows again; left in place, they would be there twice.
        """

        stagingRoot = self.store.path(STAGING_DIRECTORY)
        for run in self.store.directories(stagingRoot):
            intentPath = posixpath.join(stagingRoot, run, PUBLISHING_FILE)
            if run == self.runId or not self.store.exists(intentPath):
                continue
            try:
                intent = json.loads(self.store.readBytes(intentPath))
            except (OSError, ValueError):
                continue
            if intent.get('table') != self.tablePath or intent.get('job') != self.job:
                continue

            files = [path for path in intent.get('files', []) if isinstance(path, str) and posixpath.dirname(path) == self.tablePath]
            removed = 0
            for path in files:
                if self.store.exists(path):
                    self.store.deleteFile(path)
                    removed += 1
            self.store.deleteDirectory(posixpath.join(stagingRoot, run))
            logger.warning('{}: an earlier run was stopped while appending to {}; removed the {} part(s) it had published, whose rows '
                           'this run reads again'.format(self.job, self.loadName, removed), extra={'job': self.job})


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
        """Removes what a failed run wrote: its staging, and any part it had
        already moved into the table. A file it can't remove stays listed in
        PUBLISHING_FILE, with the staging, for the next run to take back.
        """

        if self._parts is not None:
            self._parts.discard()
        self._buffered = []

        left = []
        for path in self._published:
            try:
                self.store.deleteFile(path)
            except Exception as error:
                left.append(path)
                logger.warning('{}: could not take back {} after the run failed; the next run will -- {}'.format(
                    self.job, self.store.location(path), describeError(error)), extra={'job': self.job})
        if self._published:
            logger.info('{}: took back the {} part(s) it had published to {} before failing'.format(
                self.job, len(self._published) - len(left), self.loadName), extra={'job': self.job})
        self._published = []

        if not left:
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

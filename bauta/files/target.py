"""A table of files -- Parquet, CSV or JSON Lines -- as a data job's target,
on this machine or in S3. See "Files as a target" in docs/design.md.

A job writes its parts where no reader looks -- `_bauta_staging/<run>/`
under the connection's root -- and publishes them once every row is
written: append moves them into the table's directory beside what is there,
overwrite into a new `snapshot=<run>/` directory, marked complete by a
`_SUCCESS` file, and singleFile over `<table>.<extension>`. A job that fails
leaves nothing a reader sees.

Staging matters more on S3 than on disk. An upload pyarrow is made to stop
is completed, not abandoned -- it has no way to abort one -- so a part
written in place would appear, cut short, when its job failed.
"""
from __future__ import annotations

import datetime
import json
import logging
import posixpath
import secrets
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..configuration import ColumnType, ConfigurationError, DataJobConfig, FilesConnection, InsertStrategy, parseColumnType
from ..jobs.targets import LoadTarget
from ..log import LOGGER_NAME
from .columns import arrowType, inferType, normalized, reportedDecimal, toArrow
from .formats import PartWriter, extension, partWriter
from .stores import Store

logger = logging.getLogger(LOGGER_NAME)

# Under the root, where a run's parts are written before they are published.
# Spark, Hive and pyarrow skip a name beginning with an underscore.
STAGING_DIRECTORY = '_bauta_staging'

SNAPSHOT_PREFIX = 'snapshot='

# In each complete snapshot: what the run wrote. Its presence is what marks
# the snapshot complete.
SUCCESS_FILE = '_SUCCESS'


def newRunId() -> str:
    """Sorts by when the run began, to the second, and differs between two
    runs in the same second.
    """

    return '{}-{}'.format(datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'), secrets.token_hex(3))


class _Column:
    """One column of the table: its name in the files, its type once settled,
    whether a text column takes numbers and dates as their text, and whether
    it has held JSON documents, which JSON Lines writes nested.
    """

    def __init__(self, name: str, declared: Optional[ColumnType], reported: Optional[Tuple[int, int]]) -> None:
        self.name = name
        self.type = declared
        self.reported = reported
        # A declared text column takes numbers and dates as their text.
        self.lenient = declared is not None
        self.documents = False


class _Parts:
    """The parts of one run, written into its staging directory, each closed
    at `fileSize` and the next begun -- unless the table is one file.
    """

    def __init__(self, store: Store, directory: str, runId: str, schema: Any, jsonColumns: Set[str], singleFile: bool) -> None:
        self.store = store
        self.settings = store.settings
        self.directory = directory
        self.runId = runId
        self.schema = schema
        self.jsonColumns = jsonColumns
        self.singleFile = singleFile
        self.written: List[Tuple[str, int]] = []
        self._stream: Any = None
        self._writer: Optional[PartWriter] = None
        self._rows = 0


    def _open(self) -> None:

        name = 'part-{}-{:05d}{}'.format(self.runId, len(self.written) + 1, extension(self.settings))
        self._path = posixpath.join(self.directory, name)
        self._stream = self.store.openOutput(self._path)
        self._writer = partWriter(self._stream, self.schema, self.settings, self.jsonColumns)
        self._rows = 0


    def write(self, table: Any) -> None:
        """Writes `table` at once -- one Parquet row group -- and closes the
        part if that took it to its size. The size is what reached the
        stream: compressed, and for a text format short of what gzip still
        holds.
        """

        if self._writer is None:
            self._open()
        assert self._writer is not None

        self._writer.write(table)
        self._rows += table.num_rows
        written = self._stream.tell()

        largest = self.store.largestMove()
        if self.singleFile and largest is not None and written > largest:
            raise ConfigurationError('the table is past {} MiB, the largest file this store can publish, by copying it in one request; '
                                     'write it in parts, without singleFile'.format(largest // 2 ** 20))
        if not self.singleFile and written >= self.settings.fileSize:
            self._close()


    def _close(self) -> None:

        if self._writer is None:
            return
        self._writer.close()
        self.written.append((self._path, self._rows))
        self._writer = self._stream = None


    def close(self) -> List[Tuple[str, int]]:
        """Every part written, and its rows. An empty table still gets one part,
        so its columns are written down.
        """

        if self._writer is None and not self.written:
            self.write(self.schema.empty_table())
        self._close()

        return self.written


    def discard(self) -> None:
        """Closes what is open, best-effort. On S3 that completes the upload
        into staging, which is removed next.
        """

        for closing in (self._writer, self._stream):
            if closing is not None:
                try:
                    closing.close()
                except Exception:
                    pass
        self._writer = self._stream = None


class FileTarget(LoadTarget):
    """A table of files under a files connection's root. A job's rows are
    held until they reach `rowGroupSize`, then written at once; nothing is
    visible until finish() publishes the run's parts.
    """

    def __init__(self, job: str, jobConfig: DataJobConfig, settings: FilesConnection) -> None:
        self.job = job
        self.jobConfig = jobConfig
        self.settings = settings
        self.store = Store(settings)
        largest = self.store.largestMove()
        if largest is not None and settings.fileSize > largest:
            # Azure's, before pyarrow 19: S3's is checked with the settings.
            raise ConfigurationError('fileSize is past {} MiB, the largest file this store can publish with the pyarrow installed, which '
                                     'copies it in one request; lower fileSize, or install pyarrow 19 or newer'.format(largest // 2 ** 20))
        self.tablePath = self.store.path(jobConfig.targetTableFinal)
        self.singleFilePath = self.tablePath + extension(settings)
        self.runId = newRunId()
        self.stagingPath = self.store.path(STAGING_DIRECTORY, self.runId)
        self.loadName = self.store.location(self.singleFilePath if jobConfig.singleFile else self.tablePath + '/')
        self.columns: List[_Column] = []
        self._buffered: List[List[Any]] = []
        self._bufferedBytes = 0
        self._rowsSeen = 0
        self._parts: Optional[_Parts] = None


    def begin(self, sourceColumns: List[str], description: Optional[Sequence[Sequence[Any]]] = None) -> None:

        jobConfig = self.jobConfig
        names = jobConfig.targetColumns or sourceColumns

        if len(names) != len(sourceColumns):
            raise ConfigurationError('sourceQuery returns {} column(s) {} and targetColumns names {} ({}); a file target writes the '
                                     'query\'s columns, named in its order'.format(len(sourceColumns), sourceColumns, len(names), ', '.join(names)))

        folded: Dict[str, str] = {}
        for name in names:
            if name.upper() in folded:
                raise ConfigurationError('{} would hold two columns named {} and {}, which readers can\'t tell apart; name them apart in '
                                         'sourceQuery with AS, or in targetColumns'.format(self.loadName, folded[name.upper()], name))
            folded[name.upper()] = name

        declared = {column.upper(): parseColumnType(text) for column, text in jobConfig.targetColumnTypes.items()}
        unknown = sorted(column for column in jobConfig.targetColumnTypes if column.upper() not in folded)
        if unknown:
            raise ConfigurationError('targetColumnTypes names {} which the table has no column for (it has: {})'.format(
                ', '.join(unknown), ', '.join(names)))

        descriptions: Sequence[Optional[Sequence[Any]]] = list(description) if description else [None] * len(names)
        self.columns = [_Column(name, declared.get(name.upper()), reportedDecimal(descriptions[index]) if index < len(descriptions) else None)
                        for index, name in enumerate(names)]

        self.store.ensureDirectory(self.stagingPath)
        logger.debug('Writing {} parts in {}'.format(self.job, self.store.location(self.stagingPath)))


    def write(self, rows: List[Any]) -> None:

        import pyarrow

        if not rows:
            return

        arrays = []
        for index, column in enumerate(self.columns):
            raw = [row[index] for row in rows]
            if not column.documents and any(isinstance(value, (dict, list)) for value in raw):
                column.documents = True
            values = [normalized(value) for value in raw]
            if column.type is None:
                column.type = inferType(column.name, values, column.reported)
                # Settled as text by its values: SQLite's columns hold numbers
                # among text, so a later number is its text too.
                column.lenient = column.type is not None and column.type.kind == 'string'
            if column.type is None:
                arrays.append(pyarrow.nulls(len(values)))
            else:
                arrays.append(toArrow(column.name, values, column.type, column.lenient))

        self._buffered.append(arrays)
        self._bufferedBytes += sum(array.nbytes for array in arrays)
        self._rowsSeen += len(rows)

        if self._bufferedBytes >= self.settings.rowGroupSize:
            self._flush()


    def _schema(self) -> Any:
        """The files' schema, settling as text any column nothing has had a
        value in yet.
        """

        import pyarrow

        unsettled = [column.name for column in self.columns if column.type is None]
        if unsettled and self._rowsSeen:
            # Text alone: a later number there is a type nobody chose. Said
            # only where there were rows to go by; an empty table has none.
            logger.warning('{}: column(s) {} held no value in the first {} row(s), so {} written as string; declare the type in '
                           'targetColumnTypes if not'.format(self.job, ', '.join(unsettled), self._rowsSeen,
                                                            'it is' if len(unsettled) == 1 else 'they are'), extra={'job': self.job})
        for column in self.columns:
            if column.type is None:
                column.type = ColumnType('string')

        return pyarrow.schema([pyarrow.field(column.name, arrowType(column.type)) for column in self.columns if column.type is not None])


    def _newParts(self) -> _Parts:

        jsonColumns = {column.name for column in self.columns if column.documents}

        return _Parts(self.store, self.stagingPath, self.runId, self._schema(), jsonColumns, self.jobConfig.singleFile)


    def _flush(self) -> None:

        import pyarrow

        if not self._buffered:
            return

        if self._parts is None:
            self._parts = self._newParts()

        schema = self._parts.schema
        columns = []
        for index, field in enumerate(schema):
            # A column settled after the first of these chunks holds nulls of
            # no type in the earlier ones.
            columns.append(pyarrow.chunked_array([arrays[index] if arrays[index].type == field.type else arrays[index].cast(field.type)
                                                  for arrays in self._buffered], type=field.type))
        table = pyarrow.Table.from_arrays(columns, schema=schema)

        self._buffered = []
        self._bufferedBytes = 0
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

        assert self._parts is not None
        success = {
            'job': self.job, 'run': self.runId, 'rows': rowCount, 'format': self.settings.format.value,
            'compression': self.settings.effectiveCompression().value, 'files': published,
            'columns': [{'name': field.name, 'type': str(column.type)} for field, column in zip(self._parts.schema, self.columns)],
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
                logger.warning('{}: could not remove old snapshot {} of {} -- {}'.format(self.job, name, self.loadName, error), extra={'job': self.job})


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

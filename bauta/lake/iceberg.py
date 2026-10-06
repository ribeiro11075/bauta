"""Iceberg tables as a data job's target, through a Glue, REST or SQL
catalog, with their files in S3, Google Cloud Storage, Azure or a directory.
See "Iceberg tables" in docs/concepts/how-it-works.md.

A run is one commit, whatever its size: append and overwrite write their
Parquet parts into the table's data directory with the writer a files
connection uses, a row group at a time, and register them all at the end --
overwrite deleting the table's rows in the same commit. A reader sees the
run whole or not at all, and a file no commit names is never read, so a
failed run leaves the table as it was. Upsert is the exception: pyiceberg
merges a table held in memory, so it commits a row group at a time.

A table that isn't there is created by the run's commit, from the columns'
types. One that is keeps its own: the run writes each column as the table
has it, and refuses a column the table lacks unless the connection says
evolveSchema.
"""
from __future__ import annotations

import logging
import posixpath
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from ..configuration import ColumnType, ConfigurationError, DataJobConfig, FileFormat, FileStore, IcebergCatalog, IcebergConnection, InsertStrategy
from ..log import LOGGER_NAME
from ..log.scrubbing import describeError
from .columnar import Column, ColumnarTarget, newRunId
from .columns import FileTypeError
from .formats import Parts, PartSettings

try:
    from pyiceberg.io.pyarrow import PyArrowFileIO
except ImportError:
    # The extra isn't installed: requirePyiceberg() says so, with how to
    # install it, when a job first needs a catalog. The ignore is needed only
    # where pyiceberg is, whose class this replaces.
    PyArrowFileIO = object  # type: ignore[misc,assignment,unused-ignore]

logger = logging.getLogger(LOGGER_NAME)

# Kept of a table bauta creates: past this, each commit deletes the oldest
# metadata file, which pyiceberg otherwise keeps forever.
PREVIOUS_METADATA_VERSIONS = 20

# What Iceberg has no type for, as the nearest one it has.
_ICEBERG_SETTLED = {
    'int8': ColumnType('int32'), 'int16': ColumnType('int32'), 'uint8': ColumnType('int32'), 'uint16': ColumnType('int32'),
    'uint32': ColumnType('int64'), 'uint64': ColumnType('decimal', 20, 0),
    }


def requirePyiceberg() -> Any:

    try:
        import pyiceberg.catalog
    except ImportError as error:
        raise ConfigurationError('an Iceberg connection writes with pyiceberg, which is not installed: pip install "bauta[iceberg]"') from error

    return pyiceberg.catalog


def catalogProperties(settings: IcebergConnection) -> Dict[str, str]:
    """What pyiceberg's load_catalog takes: the catalog's own settings, the
    cloud's credentials under pyiceberg's names, and `properties` over both.
    """

    # A REST catalog's warehouse may be a name, which says nothing of the
    # cloud; its tables' files then take whichever credentials are given.
    properties: Dict[str, Optional[str]] = {'type': settings.catalog.value, 'uri': settings.uri, 'warehouse': settings.warehouseLocation(),
                                            # pyarrow's filesystems, as a files connection writes with: pyiceberg
                                            # would reach for fsspec's first for Azure, which needs adlfs. See
                                            # FileIO for the one location it spells for pyarrow.
                                            'py-io-impl': 'bauta.lake.iceberg.FileIO'}

    properties.update(credential=settings.plain('credential'), token=settings.plain('token'))

    store = settings.store()

    if store in (FileStore.S3, None) or settings.catalog == IcebergCatalog.GLUE:
        s3 = {'region': settings.region, 'endpoint': settings.endpoint, 'access-key-id': settings.accessKeyId,
              'secret-access-key': settings.plain('secretAccessKey'), 'session-token': settings.plain('sessionToken'), 'role-arn': settings.roleArn}
        properties.update({'s3.' + name: value for name, value in s3.items()})
        if settings.roleArn is not None:
            properties['s3.role-session-name'] = 'bauta'
        if settings.catalog == IcebergCatalog.GLUE:
            # Glue is reached with the same AWS credentials as the bucket.
            glue = {'region': settings.region, 'access-key-id': settings.accessKeyId, 'secret-access-key': settings.plain('secretAccessKey'),
                    'session-token': settings.plain('sessionToken')}
            properties.update({'glue.' + name: value for name, value in glue.items()})

    if store == FileStore.GCS and settings.endpoint is not None:
        properties['gcs.service.host'] = settings.endpoint

    if store in (FileStore.AZURE, None):
        adls = {'account-name': settings.azureAccount(), 'account-key': settings.plain('accountKey'), 'sas-token': settings.plain('sasToken'),
                'tenant-id': settings.tenantId, 'client-id': settings.clientId, 'client-secret': settings.plain('clientSecret')}
        properties.update({'adls.' + name: value for name, value in adls.items()})
        scheme, authority = settings.endpointParts()
        if store == FileStore.AZURE and authority is not None:
            properties.update({'adls.blob-storage-authority': authority, 'adls.dfs-storage-authority': authority,
                               'adls.blob-storage-scheme': scheme, 'adls.dfs-storage-scheme': scheme})

    properties.update({name: str(value) for name, value in settings.properties.items()})

    return {name: value for name, value in properties.items() if value is not None}


def loadCatalog(settings: IcebergConnection) -> Any:

    catalog = requirePyiceberg()

    return catalog.load_catalog('bauta', **catalogProperties(settings))


def columnTypeOf(icebergType: Any, column: str) -> ColumnType:
    """An Iceberg table's column type as bauta writes it; a ConfigurationError
    for one it can't write, naming the column.
    """

    from pyiceberg import types

    simple = {
        types.BooleanType: 'bool', types.IntegerType: 'int32', types.LongType: 'int64', types.FloatType: 'float32',
        types.DoubleType: 'float64', types.DateType: 'date', types.TimeType: 'time', types.TimestampType: 'timestamp',
        types.TimestamptzType: 'timestamptz', types.StringType: 'string', types.BinaryType: 'binary',
        }
    for kind, name in simple.items():
        if isinstance(icebergType, kind):
            return ColumnType(name)
    if isinstance(icebergType, types.DecimalType):
        return ColumnType('decimal', icebergType.precision, icebergType.scale)

    raise ConfigurationError('column {} is {} in the table, which bauta can\'t write; it writes flat tables of the types in '
                             'targetColumnTypes'.format(column, icebergType))


class _TableFiles:
    """Where Parts writes an Iceberg table's data files: through the table's
    own FileIO, with whatever credentials its catalog gave it.
    """

    def __init__(self, io: Any) -> None:
        self.io = io

    def openOutput(self, path: str) -> Any:

        return self.io.new_output(path).create(overwrite=False)

    def largestMove(self) -> Optional[int]:
        """None: a part is written where it stays, never moved."""

        return None


class IcebergTarget(ColumnarTarget):
    """One Iceberg table, written by one run in one commit -- or a commit a
    row group, for upsert.
    """

    def __init__(self, job: str, jobConfig: DataJobConfig, settings: IcebergConnection) -> None:
        super().__init__(job, jobConfig, settings.rowGroupSize)
        self.settings = settings
        self.identifier = settings.tableIdentifier(jobConfig.targetTableFinal)
        self.loadName = '.'.join(self.identifier)
        self.runId = newRunId()
        self.table: Any = None
        self.key: List[str] = []
        self._newColumns: List[str] = []
        self._transaction: Any = None
        self._parts: Optional[Parts] = None
        # Last: what raises before it leaves no catalog open.
        self.catalog = loadCatalog(settings)


    def _prepare(self) -> None:

        from pyiceberg.exceptions import NoSuchTableError

        # A declared type Iceberg lacks is written as the nearest it has, as a
        # settled one is: pyiceberg would make a uint64 a signed long.
        for column in self.columns:
            if column.type is not None:
                column.type = self._settled(column.type)

        try:
            self.table = self.catalog.load_table(self.identifier)
        except NoSuchTableError:
            self.table = None
            self.catalog.create_namespace_if_not_exists(self.identifier[0])

        tableKey = self._matchTable() if self.table is not None else []
        self.key = self._key(tableKey)

        if self.jobConfig.insertStrategy == InsertStrategy.UPSERT and not self.key:
            raise ConfigurationError('{} has no identifier fields, so an upsert cannot match its rows; name the key columns in '
                                     'targetKey'.format(self.loadName))

        keys = {name.upper() for name in self.key}
        for column in self.columns:
            if column.name.upper() in keys:
                column.required = True


    def _matchTable(self) -> List[str]:
        """Writes each column as the table has it, spelled as the table spells
        it, and checks the rest: a column the table lacks, one it requires and
        the run doesn't write, and a declared type the table disagrees with.
        Returns the table's identifier fields.
        """

        schema = self.table.schema()
        fields = {field.name.upper(): field for field in schema.fields}

        for column in self.columns:
            field = fields.get(column.name.upper())
            if field is None:
                self._newColumns.append(column.name)
                continue
            tableType = columnTypeOf(field.field_type, field.name)
            if column.type is not None and column.type != tableType:
                raise ConfigurationError('targetColumnTypes declares {} as {}, and the table has it as {}'.format(column.name, column.type, tableType))
            column.name, column.type, column.lenient, column.required = field.name, tableType, tableType.kind == 'string', field.required

        if self._newColumns and not self.settings.evolveSchema:
            them = 'it' if len(self._newColumns) == 1 else 'them'
            raise ConfigurationError('{} has no column {}; add {} to the table, leave {} out of sourceQuery, or set evolveSchema on the '
                                     'connection'.format(self.loadName, ', '.join(self._newColumns), them, them))

        written = {column.name.upper() for column in self.columns}
        unwritten = [field.name for field in schema.fields if field.required and field.name.upper() not in written]
        if unwritten:
            raise ConfigurationError('{} requires column {}, which sourceQuery doesn\'t return'.format(self.loadName, ', '.join(unwritten)))

        return [schema.find_column_name(fieldId) for fieldId in schema.identifier_field_ids]


    def _key(self, tableKey: List[str]) -> List[str]:
        """The columns rows are matched by: targetKey, as the query names its
        columns, or else the table's identifier fields. Both must agree.
        """

        if not self.jobConfig.targetKey:
            return tableKey

        byName = {column.name.upper(): column.name for column in self.columns}
        missing = [name for name in self.jobConfig.targetKey if name.upper() not in byName]
        if missing:
            raise ConfigurationError('targetKey names {}, which is not among the query\'s columns ({})'.format(
                ', '.join(missing), ', '.join(column.name for column in self.columns)))
        key = [byName[name.upper()] for name in self.jobConfig.targetKey]

        if tableKey and {name.upper() for name in key} != {name.upper() for name in tableKey}:
            raise ConfigurationError('targetKey is {}, and {} is keyed by {}'.format(', '.join(key), self.loadName, ', '.join(tableKey)))

        return key


    def _settled(self, columnType: ColumnType) -> ColumnType:

        return _ICEBERG_SETTLED.get(columnType.kind, columnType)


    def _field(self, column: Column) -> Any:

        return super()._field(column).with_nullable(not column.required)


    def _creationProperties(self) -> Dict[str, str]:

        return {
            'write.parquet.compression-codec': self.settings.effectiveCompression().value,
            'write.metadata.delete-after-commit.enabled': 'true',
            'write.metadata.previous-versions-max': str(PREVIOUS_METADATA_VERSIONS),
            }


    def _evolve(self, transaction: Any) -> None:
        """Adds the columns the table lacks, once."""

        if self._newColumns:
            logger.info('Adding column(s) {} to {}'.format(', '.join(self._newColumns), self.loadName), extra={'job': self.job})
            with transaction.update_schema() as update:
                update.union_by_name(self.schema)
            self._newColumns = []


    def _writeTable(self, table: Any) -> None:

        for column in self.columns:
            # Said here, with the column's name, rather than by pyiceberg as a
            # schema mismatch.
            if column.required and table.column(column.name).null_count:
                raise FileTypeError('column {} is {} and a row holds no value in it'.format(
                    column.name, 'a key' if column.name in self.key else 'required by the table'))

        if self.jobConfig.insertStrategy == InsertStrategy.UPSERT:
            self._upsert(table)
            return

        if self._parts is None:
            self._openCommit()
        assert self._parts is not None
        self._parts.write(table)


    def _openCommit(self) -> None:
        """The run's transaction -- creating the table, if it isn't there --
        and the parts it writes into the table's data directory.
        """

        if self.table is None:
            self._transaction = self.catalog.create_table_transaction(self.identifier, schema=self.schema, properties=self._creationProperties())
            if self.key:
                with self._transaction.update_schema() as update:
                    update.set_identifier_fields(*self.key)
        else:
            self._transaction = self.table.transaction()
            self._evolve(self._transaction)

        settings = PartSettings(FileFormat.PARQUET, self.settings.effectiveCompression(), self.settings.fileSize)
        self._parts = Parts(_TableFiles(self._io()), posixpath.join(self._transaction.table_metadata.location, 'data'), self.runId, self.schema,
                            settings)


    def _io(self) -> Any:
        """The FileIO of the table the transaction writes -- one the catalog
        hasn't created yet, too, which pyiceberg reaches only privately.
        """

        return self._transaction._table.io


    def _upsert(self, table: Any) -> None:
        """One row group merged into the table, as its own commit: the last row
        of each key, since one merge can't take a key twice.
        """

        import pyarrow

        if self.table is None:
            self.table = self.catalog.create_table(self.identifier, schema=self.schema, properties=self._creationProperties())
            with self.table.update_schema() as update:
                update.set_identifier_fields(*self.key)
        elif self._newColumns:
            with self.table.transaction() as transaction:
                self._evolve(transaction)

        keys = list(zip(*(table.column(name).to_pylist() for name in self.key)))
        last = {key: index for index, key in enumerate(keys)}
        if len(last) < len(keys):
            table = table.take(pyarrow.array(sorted(last.values())))

        result = self.table.upsert(table, join_cols=self.key)
        logger.debug('Upserted into {}: {} updated, {} inserted'.format(self.loadName, result.rows_updated, result.rows_inserted))


    def finish(self, rowCount: int) -> None:

        self._flush()
        strategy = self.jobConfig.insertStrategy

        if strategy == InsertStrategy.UPSERT:
            if self.table is None:
                # No rows, and no table: it is created, empty and keyed.
                self._upsert(self._settleSchema().empty_table())
            logger.info('Upserted {} row(s) into {}'.format(rowCount, self.loadName))
        elif strategy == InsertStrategy.APPEND and self._parts is None:
            logger.info('{}: no rows, so nothing to append to {}'.format(self.job, self.loadName))
            return
        else:
            self._commit(rowCount)

        self._expireSnapshots()


    def _commit(self, rowCount: int) -> None:
        """Registers the run's parts in one commit -- an overwrite deleting the
        table's rows in the same one, so no reader sees it empty between.
        """

        from pyiceberg.expressions import AlwaysTrue

        if self._parts is None:
            # An overwrite of no rows: the table is emptied, or created empty.
            self._settleSchema()
            self._openCommit()
        assert self._parts is not None

        paths = [path for path, _ in self._parts.close()] if rowCount else []
        if self.jobConfig.insertStrategy == InsertStrategy.OVERWRITE and self.table is not None:
            self._transaction.delete(AlwaysTrue())
        if paths:
            self._transaction.add_files(paths, snapshot_properties={'bauta.job': self.job, 'bauta.run': self.runId}, check_duplicate_files=False)
        self._transaction.commit_transaction()

        if self.jobConfig.insertStrategy == InsertStrategy.APPEND:
            logger.info('Appended {} row(s) to {} in one commit'.format(rowCount, self.loadName))
        else:
            logger.info('Replaced the rows of {} with {} in one commit'.format(self.loadName, rowCount))


    def _expireSnapshots(self) -> None:
        """Keeps the newest `keepSnapshots` of the table's snapshots, and deletes
        the files only the others referenced. pyiceberg forgets a snapshot and
        leaves its files, so a row deleted or rewritten -- a person's data, or
        a value masked under a key since rotated -- would stay readable in them.
        """

        def referenced(table: Any) -> Set[str]:
            return set(table.inspect.all_files().column('file_path').to_pylist())

        try:
            table = self.catalog.load_table(self.identifier)
            snapshots = sorted(table.snapshots(), key=lambda snapshot: snapshot.timestamp_ms)
            current = table.current_snapshot()
            stale = [snapshot.snapshot_id for snapshot in snapshots[:-self.settings.keepSnapshots]
                     if current is None or snapshot.snapshot_id != current.snapshot_id]
            if not stale:
                return

            before = referenced(table)
            table.maintenance.expire_snapshots().by_ids(stale).commit()
            table = self.catalog.load_table(self.identifier)
            unreferenced = sorted(path for path in before - referenced(table) if path.startswith(table.location()))
            for path in unreferenced:
                table.io.delete(path)
            logger.info('Expired {} snapshot(s) of {}, and deleted the {} file(s) only they referenced'.format(
                len(stale), self.loadName, len(unreferenced)), extra={'job': self.job})
        except Exception as error:
            # The run is committed; old snapshots left are untidy, not wrong.
            logger.warning('{}: could not expire old snapshots of {} -- {}'.format(self.job, self.loadName, describeError(error)), extra={'job': self.job})


    def abort(self) -> None:
        """Deletes the parts written, which no commit names: nothing reads
        them, but they take room. An upsert's committed row groups stay.
        """

        if self._parts is None:
            return
        for path in self._parts.discard():
            try:
                self._io().delete(path)
            except Exception:
                pass


    def close(self) -> None:
        """The catalog's connections: a SQL catalog's database, a REST one's
        HTTP session.
        """

        self.catalog.close()


def checkIcebergWritable(settings: IcebergConnection) -> None:
    """Raises unless the catalog answers and -- where the warehouse is a
    location -- a file can be written and deleted in it.
    """

    with loadCatalog(settings) as catalog:
        catalog.list_namespaces()

    warehouse = settings.warehouseLocation()
    if warehouse is not None and '://' in warehouse:
        from pyiceberg.io import load_file_io

        probe = posixpath.join(warehouse, '_bauta_probe-{}'.format(newRunId()))
        io = load_file_io(catalogProperties(settings), location=warehouse)
        with io.new_output(probe).create() as stream:
            stream.write(b'bauta')
        io.delete(probe)


# What pyiceberg calls an Azure location.
_AZURE_SCHEMES = frozenset({'abfs', 'abfss', 'wasb', 'wasbs'})


class FileIO(PyArrowFileIO):
    """The FileIO an Iceberg connection's tables are written through:
    pyiceberg's own, over pyarrow's filesystems, with one location spelled as
    pyarrow reads it. pyiceberg 0.12 hands pyarrow an Azure file as
    `container@account.dfs.core.windows.net/path`, and pyarrow reads all
    before the first slash as the container's name, which Azure refuses as
    invalid; here it is `container/path`, as pyarrow names an Azure file
    everywhere else. pyiceberg loads it by name, through the catalog's
    `py-io-impl` property.
    """

    @staticmethod
    def parse_location(location: str, properties: Optional[Dict[str, str]] = None) -> Tuple[str, str, str]:

        scheme, netloc, path = PyArrowFileIO.parse_location(location, properties or {})

        if scheme in _AZURE_SCHEMES and '@' in netloc:
            container = netloc.split('@', 1)[0]
            path = container + urlparse(location).path

        return scheme, netloc, path

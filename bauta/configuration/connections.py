"""The connections connections.yaml names, one pydantic model per type: the
seven databases, a files connection and an Iceberg one. Each takes only its
own settings, so one given to the wrong type is refused rather than ignored.
"""
from __future__ import annotations

import os
import re
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Mapping, Optional, Sequence, Tuple, Type, Union, cast

from pydantic import BaseModel, BeforeValidator, ByteSize, ConfigDict, Field, SecretStr, TypeAdapter, field_validator, model_validator

from .environment import ConfigurationError, runPasswordCommand, splitPasswordCommand

def _dropNoneListItems(value: Any) -> Any:
    """YAML's "key:\\n-\\n" idiom (an empty list item) parses to [None] -- treat
    that, and a bare `~`/omitted key, as an empty list rather than a validation error.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if item is not None]

    return value


def _dropNoneMappingEntries(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {key: item for key, item in value.items() if item is not None}

    return value


def _dropNoneMappingListItems(value: Any) -> Any:
    cleaned = _dropNoneMappingEntries(value)

    if isinstance(cleaned, dict):
        return {key: (_dropNoneListItems(item) if isinstance(item, list) else item) for key, item in cleaned.items()}

    return cleaned


CleanedStringList = Annotated[List[str], BeforeValidator(_dropNoneListItems)]
CleanedMapping = Annotated[Dict[str, Any], BeforeValidator(_dropNoneMappingEntries)]
CleanedListMapping = Annotated[Dict[str, List[str]], BeforeValidator(_dropNoneMappingListItems)]


class DatabaseType(str, Enum):
    ORACLE = 'oracle'
    MYSQL = 'mysql'
    POSTGRESQL = 'postgresql'
    MSSQL = 'mssql'
    SQLITE = 'sqlite'
    MARIADB = 'mariadb'
    DUCKDB = 'duckdb'


# The databases that run in this process, from a file, with no server or login.
EMBEDDED_TYPES = frozenset({DatabaseType.SQLITE, DatabaseType.DUCKDB})


class StoreType(str, Enum):
    """The connections that aren't databases."""

    FILES = 'files'
    ICEBERG = 'iceberg'


# Every value a connection's `type` may take.
CONNECTION_TYPES = tuple(connectionType.value for connectionType in DatabaseType) + tuple(storeType.value for storeType in StoreType)


# A plain SQL identifier -- what currentSchema is written into a session
# statement as, so nothing else may get through.
IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_$#]*$')



class _BaseConnection(BaseModel):
    """What every connection has, whatever it connects to.

    Each type is a model of its own, taking only the settings that type has,
    so a setting given to the wrong type is refused rather than ignored. See
    ConnectionConfig.
    """

    model_config = ConfigDict(extra='forbid')

    # No job may read from or write to this connection without masking. The
    # line a reviewer signs: "this copy can only ever hold masked data." Unlike
    # a job's own `unmasked`, nothing overrides it.
    requireMasking: bool = False
    # The most jobs that may use this connection at once, whatever `workers`
    # allows. None is no limit. See jobLimit.
    maxConcurrentJobs: Optional[int] = Field(default=None, ge=1)

    @model_validator(mode='before')
    @classmethod
    def _refuseAnotherTypesSetting(cls, value: Any) -> Any:
        """A setting that belongs to other types is named with the types it
        belongs to, rather than as an unknown setting.
        """

        if not isinstance(value, Mapping):
            return value

        connectionType = getattr(value.get('type'), 'value', value.get('type'))
        problems = []
        for name in sorted(set(value) - set(cls.model_fields)):
            owners = sorted(owner.value for owner, model in _CONNECTION_MODELS.items() if name in model.model_fields)
            if owners:
                hint = '; a {} connection names its file with path'.format(connectionType) if name == 'database' and 'path' in cls.model_fields else ''
                problems.append('{} is a setting of {} connections, not {}{}'.format(name, _listed(owners), connectionType, hint))

        if problems:
            raise ValueError('; '.join(problems))

        return value

    def jobLimit(self) -> Optional[int]:
        """How many jobs may use this connection at once, or None for as many
        as `workers` allows.
        """

        return self.maxConcurrentJobs

    def plainPassword(self) -> Optional[str]:
        """The password to connect with. Only a server connection has one."""

        return None

    def describeTarget(self) -> str:
        """What this connection points at, for an error that has to say so --
        a driver's own message names nothing. Never a password, and never
        `options`, which can carry one.
        """

        raise NotImplementedError


class _Connection(_BaseConnection):
    """A database. `options` -- extra driver arguments, TLS above all -- are
    left out of the repr, since they can hold secrets.
    """

    type: DatabaseType
    options: CleanedMapping = Field(default_factory=dict, repr=False)
    # The most rows a second jobs may read from this database, all of them
    # together, partitions and all, so a copy can't take more of production
    # than it was given. None is no limit. See jobs.throttle.ReadLimit.
    maxRowsReadPerSecond: Optional[float] = Field(default=None, gt=0)


class _CurrentSchema(BaseModel):
    """The schema unqualified names resolve in, for the types that can set
    one per session. Written into a session statement, so it must be a plain
    identifier.
    """

    currentSchema: Optional[str] = None

    @field_validator('currentSchema')
    @classmethod
    def _plainIdentifier(cls, currentSchema: Optional[str]) -> Optional[str]:

        if currentSchema is not None and not IDENTIFIER.match(currentSchema):
            raise ValueError('currentSchema must be a plain identifier, got {!r}'.format(currentSchema))

        return currentSchema


class _ServerConnection(_Connection):
    """A database on a server, reached with a login. `password` is a
    SecretStr; `passwordCommand` runs at every connect, for expiring
    credentials such as IAM tokens. Exactly one of the two.
    """

    host: str
    port: Optional[int] = None
    user: str
    password: Optional[SecretStr] = None
    passwordCommand: Optional[Union[str, List[str]]] = None

    @model_validator(mode='after')
    def _requireOnePassword(self) -> '_ServerConnection':

        if self.password is not None and self.passwordCommand is not None:
            raise ValueError('set password or passwordCommand, not both')
        if self.password is None and not self.passwordCommand:
            raise ValueError('{} connections need a password or a passwordCommand'.format(self.type.value))
        if self.passwordCommand is not None:
            splitPasswordCommand(self.passwordCommand)

        return self

    def plainPassword(self) -> Optional[str]:
        """Runs passwordCommand, if that's how it is configured, so call it
        only when about to connect.
        """

        if self.passwordCommand:
            return runPasswordCommand(self.passwordCommand)

        return None if self.password is None else self.password.get_secret_value()

    def _name(self) -> str:

        return getattr(self, 'database')

    def describeTarget(self) -> str:

        where = self.host if not self.port else '{}:{}'.format(self.host, self.port)

        return '{} {} on {}'.format(self.type.value, self._name(), where)


class PostgreSQLConnection(_ServerConnection, _CurrentSchema):

    type: Literal[DatabaseType.POSTGRESQL] = DatabaseType.POSTGRESQL
    database: str


class MySQLConnection(_ServerConnection):

    type: Literal[DatabaseType.MYSQL] = DatabaseType.MYSQL
    database: str


class MariaDBConnection(_ServerConnection):

    type: Literal[DatabaseType.MARIADB] = DatabaseType.MARIADB
    database: str


class MSSQLConnection(_ServerConnection):
    """SQL Server takes the default schema from the login, so it has no
    currentSchema; qualify names as schema.table instead.
    """

    type: Literal[DatabaseType.MSSQL] = DatabaseType.MSSQL
    database: str


class OracleConnection(_ServerConnection, _CurrentSchema):
    """Reached by service name or SID, exactly one of them; Oracle has no
    database name to give.
    """

    type: Literal[DatabaseType.ORACLE] = DatabaseType.ORACLE
    serviceName: Optional[str] = None
    sid: Optional[str] = None

    @model_validator(mode='after')
    def _requireOneIdentifier(self) -> 'OracleConnection':

        if not (bool(self.serviceName) ^ bool(self.sid)):
            raise ValueError('oracle connections require exactly one of serviceName or sid')

        return self

    def _name(self) -> str:

        return self.serviceName or self.sid or '?'


class _FileConnection(_Connection):
    """A database in a file this process opens, with no server or login.
    `path` is the file, or `:memory:`.
    """

    path: str

    def describeTarget(self) -> str:
        """The absolute path: a driver's own "unable to open database file"
        leaves a relative path and the directory it resolved against unsaid,
        which is the hard part of the failure.
        """

        if self.path == ':memory:':
            return '{} :memory:'.format(self.type.value)

        return '{} file {}'.format(self.type.value, os.path.abspath(self.path))


class SQLiteConnection(_FileConnection):
    """SQLite has no schema separate from the file, so no currentSchema."""

    type: Literal[DatabaseType.SQLITE] = DatabaseType.SQLITE


class DuckDBConnection(_FileConnection, _CurrentSchema):
    """DuckDB lets one process at a time open a file, and every job is a
    process of its own, so one job at a time uses it.
    """

    type: Literal[DatabaseType.DUCKDB] = DatabaseType.DUCKDB

    @model_validator(mode='after')
    def _refuseConcurrentJobs(self) -> 'DuckDBConnection':

        if self.maxConcurrentJobs is not None and self.maxConcurrentJobs > 1:
            raise ValueError('maxConcurrentJobs cannot be above 1 for duckdb: DuckDB lets one process at a time open a file, and every job '
                             'runs in a process of its own')

        return self

    def jobLimit(self) -> Optional[int]:

        return 1


# What a file target writes in, and how. Sizes are bytes, or text such as
# 256MB (decimal) or 256MiB (binary). 256MB is what Snowflake suggests a file
# to load be at most, and a size Athena and Spark split well.
DEFAULT_FILE_SIZE = 256 * 10 ** 6
DEFAULT_ROW_GROUP_SIZE = 128 * 10 ** 6
DEFAULT_KEEP_SNAPSHOTS = 2


class FileFormat(str, Enum):
    PARQUET = 'parquet'
    CSV = 'csv'
    # JSON Lines: one JSON object per line.
    NDJSON = 'ndjson'


class FileCompression(str, Enum):
    ZSTD = 'zstd'
    SNAPPY = 'snappy'
    GZIP = 'gzip'
    NONE = 'none'


# What each format may be compressed with, the default first. Parquet
# compresses inside the file; text is compressed whole, and gzip is what
# every engine reading text reads.
FORMAT_COMPRESSIONS = {
    FileFormat.PARQUET: (FileCompression.ZSTD, FileCompression.SNAPPY, FileCompression.GZIP, FileCompression.NONE),
    FileFormat.CSV: (FileCompression.GZIP, FileCompression.NONE),
    FileFormat.NDJSON: (FileCompression.GZIP, FileCompression.NONE),
    }

class FileStore(str, Enum):
    """Where a files connection's root is: told from its URL."""

    LOCAL = 'local'
    S3 = 's3'
    GCS = 'gcs'
    AZURE = 'azure'


S3_SCHEME = 's3://'

# Each URL a root may begin with. Azure's abfss:// is what Databricks and
# Synapse write, and names the account in the host.
ROOT_SCHEMES = {S3_SCHEME: FileStore.S3, 'gs://': FileStore.GCS, 'az://': FileStore.AZURE, 'abfss://': FileStore.AZURE, 'abfs://': FileStore.AZURE}

_ABFS = re.compile(r'^abfss?://([^@/]+)@([^./]+)\.dfs\.core\.windows\.net(/.*)?$')

# A part is published by copying it within the bucket, and S3 copies an
# object of at most 5 GiB in one request.
S3_LARGEST_COPY = 5 * 2 ** 30

# The settings only a root in each store takes; `endpoint` is every cloud's.
STORE_SETTINGS = {
    FileStore.S3: ('region', 'endpoint', 'accessKeyId', 'secretAccessKey', 'sessionToken', 'roleArn'),
    FileStore.GCS: ('endpoint', 'anonymous', 'serviceAccount'),
    FileStore.AZURE: ('endpoint', 'accountName', 'accountKey', 'sasToken', 'clientId', 'clientSecret', 'tenantId'),
    }

_STORE_NAMES = {FileStore.S3: 'S3', FileStore.GCS: 'Google Cloud Storage', FileStore.AZURE: 'Azure'}


class _Lake(_BaseConnection):
    """What a files connection and an Iceberg one share: how data files are
    written, and the credentials of the cloud they are written to.

    In each cloud, credentials come from its own default chain -- what its
    command-line tool would find: the environment, a profile or login, the
    machine's or the pod's identity -- unless settings give them. `endpoint`
    points at another service speaking the same API, such as MinIO or R2.
    """

    # None is the format's default: FORMAT_COMPRESSIONS.
    compression: Optional[FileCompression] = None
    fileSize: ByteSize = ByteSize(DEFAULT_FILE_SIZE)
    rowGroupSize: ByteSize = ByteSize(DEFAULT_ROW_GROUP_SIZE)
    endpoint: Optional[str] = None
    # S3.
    region: Optional[str] = None
    accessKeyId: Optional[str] = None
    secretAccessKey: Optional[SecretStr] = None
    sessionToken: Optional[SecretStr] = None
    roleArn: Optional[str] = None
    # Google Cloud Storage.
    anonymous: Optional[bool] = None
    serviceAccount: Optional[str] = None
    # Azure.
    accountName: Optional[str] = None
    accountKey: Optional[SecretStr] = None
    sasToken: Optional[SecretStr] = None
    clientId: Optional[str] = None
    clientSecret: Optional[SecretStr] = None
    tenantId: Optional[str] = None

    @field_validator('fileSize', 'rowGroupSize')
    @classmethod
    def _positiveSize(cls, size: ByteSize) -> ByteSize:
        """Checked here rather than with Field(gt=0): whether pydantic can apply
        a constraint to ByteSize has differed between the versions this allows.
        """

        if size <= 0:
            raise ValueError('must be more than 0 bytes')

        return size

    def _cloudLocation(self) -> Optional[str]:
        """The URL or directory that says which store this writes to, or None
        where settings can't tell -- an Iceberg REST catalog's warehouse."""

        raise NotImplementedError

    def store(self) -> Optional[FileStore]:

        location = self._cloudLocation()
        if location is None:
            return None

        return next((store for scheme, store in ROOT_SCHEMES.items() if location.startswith(scheme)), FileStore.LOCAL)

    def isObjectStore(self) -> bool:

        return self.store() not in (FileStore.LOCAL, None)

    def azureAccount(self) -> Optional[str]:

        match = _ABFS.match(self._cloudLocation() or '')

        return match.group(2) if match else self.accountName

    @model_validator(mode='after')
    def _coherentCloudSettings(self) -> '_Lake':

        store = self.store()
        if store is not None:
            for other, names in STORE_SETTINGS.items():
                given = [name for name in names if getattr(self, name) is not None and name not in STORE_SETTINGS.get(store, ())]
                if given:
                    where = 'a directory on this machine' if store == FileStore.LOCAL else 'on {}'.format(_STORE_NAMES[store])
                    raise ValueError('{} {} for {}, and {} is {}'.format(
                        _listed(given), 'is' if len(given) == 1 else 'are', _STORE_NAMES[other], self._locationName(), where))

        if (self.accessKeyId is None) != (self.secretAccessKey is None):
            raise ValueError('set accessKeyId and secretAccessKey together, or neither to use the AWS default credential chain')
        if self.sessionToken is not None and self.accessKeyId is None:
            raise ValueError('sessionToken goes with accessKeyId and secretAccessKey')

        if self.anonymous and self.serviceAccount is not None:
            raise ValueError('anonymous and serviceAccount say two different things; set one')

        match = _ABFS.match(self._cloudLocation() or '')
        if match and self.accountName is not None and self.accountName != match.group(2):
            raise ValueError('accountName {} is not the account {} names, {}'.format(self.accountName, self._locationName(), match.group(2)))
        if store == FileStore.AZURE and not match and self.accountName is None:
            raise ValueError('an az:// {} needs accountName, the storage account the container is in'.format(self._locationName()))
        principal = [name for name in ('clientId', 'clientSecret', 'tenantId') if getattr(self, name) is not None]
        if principal and len(principal) != 3:
            raise ValueError('a service principal needs clientId, clientSecret and tenantId together')
        ways = [name for name in ('accountKey', 'sasToken') if getattr(self, name) is not None] + (['a service principal'] if principal else [])
        if len(ways) > 1:
            raise ValueError('{} are each a way to sign in; set one, or none to use the Azure default credential chain'.format(_listed(ways)))

        return self

    def _locationName(self) -> str:

        raise NotImplementedError

    def plain(self, setting: str) -> Optional[str]:
        """A secret setting's value, or None where it isn't set: for a driver
        that takes the text, when about to hand it over.
        """

        value = getattr(self, setting)

        return None if value is None else value.get_secret_value()

    def endpointParts(self) -> Tuple[Optional[str], Optional[str]]:
        """`endpoint` as its scheme and host[:port] -- https unless it says
        http -- or (None, None) without one.
        """

        if self.endpoint is None:
            return None, None
        if '://' in self.endpoint:
            scheme, authority = self.endpoint.split('://', 1)
            return scheme, authority.rstrip('/')

        return 'https', self.endpoint.rstrip('/')


def _checkLocation(location: str, setting: str) -> str:
    """A directory, or a bucket or container, with a prefix or without. Any
    other URL is refused rather than read as a relative directory named
    `hdfs:`.
    """

    scheme = next((scheme for scheme in ROOT_SCHEMES if location.startswith(scheme)), None)

    if scheme is None:
        if '://' in location and not location.startswith('file://'):
            raise ValueError('{} must be a directory, or begin with {}; {} is not supported'.format(
                setting, ', '.join(sorted(ROOT_SCHEMES)), location.split('://', 1)[0] + '://'))
        return location

    if scheme.startswith('abfs'):
        if not _ABFS.match(location):
            raise ValueError('{} {!r} is not an Azure location; write abfss://container@account.dfs.core.windows.net/prefix, or '
                             'az://container/prefix with accountName'.format(setting, location))
    elif not location[len(scheme):].split('/', 1)[0]:
        raise ValueError('{} {!r} names no {}; write {}name or {}name/prefix'.format(
            setting, location, 'container' if scheme == 'az://' else 'bucket', scheme, scheme))

    return location.rstrip('/')


class FilesConnection(_Lake):
    """A directory -- on this machine, or in S3, Google Cloud Storage or
    Azure Blob Storage -- that jobs write tables of files into, one directory
    per table: `targetTableFinal` is its path under `root`. Written, never
    read: a files connection is a target only. See "Files as a target" in
    docs/concepts/how-it-works.md.

    `fileSize` is where a part is closed and the next begun; `rowGroupSize`
    is how much of a table is held in memory, before compression, and written
    at once -- one Parquet row group. `keepSnapshots` is how many of an
    overwrite job's complete snapshots stay, the newest included.
    """

    type: Literal[StoreType.FILES] = StoreType.FILES
    root: str = Field(min_length=1)
    format: FileFormat = FileFormat.PARQUET
    # CSV's alone; a comma when not given.
    delimiter: Optional[str] = None
    keepSnapshots: int = Field(default=DEFAULT_KEEP_SNAPSHOTS, ge=1)

    @field_validator('root')
    @classmethod
    def _supportedRoot(cls, root: str) -> str:

        if root.startswith('file://'):
            raise ValueError('root is a directory, written without file://')

        return _checkLocation(root, 'root')

    @model_validator(mode='after')
    def _coherentFileSettings(self) -> 'FilesConnection':

        if self.store() == FileStore.S3 and self.fileSize > S3_LARGEST_COPY:
            raise ValueError('fileSize can be at most 5GiB on S3, which copies no larger an object in one request, and a part is '
                             'published by copying it')

        allowed = FORMAT_COMPRESSIONS[self.format]
        if self.compression is not None and self.compression not in allowed:
            raise ValueError('compression {} is not one for {}; choose from {}'.format(
                self.compression.value, self.format.value, ', '.join(compression.value for compression in allowed)))

        if self.delimiter is not None:
            if self.format != FileFormat.CSV:
                raise ValueError('delimiter is for format: csv')
            if len(self.delimiter) != 1 or self.delimiter in '"\r\n':
                raise ValueError('delimiter must be one character, other than a quote or a line break, got {!r}'.format(self.delimiter))

        return self

    def _cloudLocation(self) -> Optional[str]:

        return self.root

    def _locationName(self) -> str:

        return 'root'

    def store(self) -> FileStore:

        store = super().store()
        assert store is not None

        return store

    def bucketPath(self) -> str:
        """The root as its store's filesystem names it: `bucket/prefix`, or
        `container/prefix` on Azure.
        """

        match = _ABFS.match(self.root)
        if match:
            return match.group(1) + (match.group(3) or '').rstrip('/')

        return self.root.split('://', 1)[1].rstrip('/')

    def effectiveCompression(self) -> FileCompression:

        return self.compression or FORMAT_COMPRESSIONS[self.format][0]

    def location(self) -> str:
        """The root as a person would write it: the URL, or the absolute
        directory -- a driver's own message leaves a relative one unresolved.
        """

        if self.isObjectStore():
            return self.root

        return os.path.abspath(os.path.expanduser(self.root))

    def describeTarget(self) -> str:

        return 'files in {}'.format(self.location())


class IcebergCatalog(str, Enum):
    # AWS's own.
    GLUE = 'glue'
    # The Iceberg REST protocol: BigLake on GCP, Databricks Unity Catalog,
    # Snowflake Open Catalog (Polaris), S3 Tables, Lakekeeper, Nessie.
    REST = 'rest'
    # A catalog kept in a database of your own: SQLite or PostgreSQL.
    SQL = 'sql'


# An Iceberg table's snapshots kept by default, the newest included: a query
# still reading one of them keeps its files.
DEFAULT_KEEP_ICEBERG_SNAPSHOTS = 5

# A namespace, written into a catalog's identifiers.
_NAMESPACE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


class IcebergConnection(_Lake):
    """Iceberg tables, found through a catalog. A job's `targetTableFinal` is
    `namespace.table`, or `table` in `namespace`. Written, never read. See
    "Iceberg tables" in docs/concepts/how-it-works.md.

    `warehouse` is where a table the catalog creates keeps its files -- for
    a REST catalog, what that catalog calls its warehouse, often a name. The
    cloud settings are for writing those files; a REST catalog may hand out
    credentials of its own instead. `properties` go to pyiceberg's catalog as
    they are, for anything else it takes.

    `keepSnapshots` is how many of a table's snapshots stay after a run, the
    newest included; the files only older ones reference are deleted with
    them. `evolveSchema` lets a run add a column the table lacks.
    """

    type: Literal[StoreType.ICEBERG] = StoreType.ICEBERG
    catalog: IcebergCatalog
    uri: Optional[str] = None
    warehouse: Optional[str] = None
    namespace: Optional[str] = None
    # A REST catalog's: `clientId:clientSecret` for its OAuth2, or a token.
    credential: Optional[SecretStr] = None
    token: Optional[SecretStr] = None
    properties: CleanedMapping = Field(default_factory=dict, repr=False)
    keepSnapshots: int = Field(default=DEFAULT_KEEP_ICEBERG_SNAPSHOTS, ge=1)
    evolveSchema: bool = False

    @field_validator('warehouse')
    @classmethod
    def _supportedWarehouse(cls, warehouse: Optional[str]) -> Optional[str]:

        return None if warehouse is None else _checkLocation(warehouse, 'warehouse')

    @field_validator('namespace')
    @classmethod
    def _plainNamespace(cls, namespace: Optional[str]) -> Optional[str]:

        if namespace is not None and not _NAMESPACE.match(namespace):
            raise ValueError('namespace must be a plain identifier, got {!r}'.format(namespace))

        return namespace

    @model_validator(mode='after')
    def _coherentCatalogSettings(self) -> 'IcebergConnection':

        required = {IcebergCatalog.SQL: ('uri', 'warehouse'), IcebergCatalog.GLUE: ('warehouse',), IcebergCatalog.REST: ('uri',)}[self.catalog]
        missing = [name for name in required if getattr(self, name) is None]
        if missing:
            raise ValueError('a {} catalog needs {}'.format(self.catalog.value, _listed(missing)))

        if self.catalog == IcebergCatalog.GLUE and self.uri is not None:
            raise ValueError('uri is for a rest or sql catalog; Glue is found by region')
        restOnly = [name for name in ('credential', 'token') if getattr(self, name) is not None]
        if restOnly and self.catalog != IcebergCatalog.REST:
            raise ValueError('{} {} for a rest catalog'.format(_listed(restOnly), 'is' if len(restOnly) == 1 else 'are'))

        if self.store() == FileStore.GCS or (self.store() is None and self.anonymous is not None):
            unsupported = [name for name in ('anonymous', 'serviceAccount') if getattr(self, name) is not None]
            if unsupported:
                raise ValueError('{} {} not something pyiceberg can write Google Cloud Storage with; it takes Application Default '
                                 'Credentials'.format(_listed(unsupported), 'is' if len(unsupported) == 1 else 'are'))

        if self.compression is not None and self.compression not in FORMAT_COMPRESSIONS[FileFormat.PARQUET]:
            raise ValueError('compression {} is not one for Parquet; choose from {}'.format(
                self.compression.value, ', '.join(compression.value for compression in FORMAT_COMPRESSIONS[FileFormat.PARQUET])))

        return self

    def _cloudLocation(self) -> Optional[str]:
        """The warehouse, where it is a location -- a REST catalog's may be a
        name, which says nothing of where its files are.
        """

        if self.warehouse is None:
            return None
        if self.catalog == IcebergCatalog.REST and '://' not in self.warehouse:
            return None

        return self.warehouse

    def _locationName(self) -> str:

        return 'warehouse'

    def effectiveCompression(self) -> FileCompression:

        return self.compression or FORMAT_COMPRESSIONS[FileFormat.PARQUET][0]

    def warehouseLocation(self) -> Optional[str]:
        """The warehouse as pyiceberg takes it: a directory as a file:// URL,
        and az://container/prefix as the abfss:// URL it stands for, the only
        spelling of Azure pyiceberg reads.
        """

        if self.warehouse is not None and self.warehouse.startswith('az://'):
            container, _, prefix = self.warehouse[len('az://'):].partition('/')
            return 'abfss://{}@{}.dfs.core.windows.net{}'.format(container, self.accountName, '/' + prefix if prefix else '')
        if self.warehouse is None or self.store() != FileStore.LOCAL:
            return self.warehouse
        if self.warehouse.startswith('file://'):
            return self.warehouse

        return 'file://' + os.path.abspath(os.path.expanduser(self.warehouse))

    def tableIdentifier(self, table: str) -> Tuple[str, str]:
        """`table`, as a job names it, as (namespace, name)."""

        if '.' in table:
            namespace, name = table.rsplit('.', 1)
            return namespace, name
        if self.namespace is None:
            raise ConfigurationError('table {} names no namespace, and the connection has none; write namespace.{} or set '
                                     'namespace'.format(table, table))

        return self.namespace, table

    def describeTarget(self) -> str:

        where = self.uri if self.catalog != IcebergCatalog.GLUE else 'region {}'.format(self.region or 'from the environment')

        return 'Iceberg tables in the {} catalog at {}'.format(self.catalog.value, where)


# The connections that are tables of files rather than databases.
LakeConfig = Union[FilesConnection, IcebergConnection]


_CONNECTION_MODELS: Dict[Union[DatabaseType, StoreType], Type[_BaseConnection]] = {
    DatabaseType.POSTGRESQL: PostgreSQLConnection, DatabaseType.MYSQL: MySQLConnection, DatabaseType.MARIADB: MariaDBConnection,
    DatabaseType.MSSQL: MSSQLConnection, DatabaseType.ORACLE: OracleConnection, DatabaseType.SQLITE: SQLiteConnection,
    DatabaseType.DUCKDB: DuckDBConnection, StoreType.FILES: FilesConnection, StoreType.ICEBERG: IcebergConnection,
    }

# One database in connections.yaml.
DatabaseConfig = Union[PostgreSQLConnection, MySQLConnection, MariaDBConnection, MSSQLConnection, OracleConnection, SQLiteConnection, DuckDBConnection]

# One connection in connections.yaml: the model its `type` names.
ConnectionConfig = Annotated[Union[PostgreSQLConnection, MySQLConnection, MariaDBConnection, MSSQLConnection, OracleConnection,
                                   SQLiteConnection, DuckDBConnection, FilesConnection, IcebergConnection], Field(discriminator='type')]

# Cast, since pydantic's stubs before 2.7 take a class here and not a union.
_CONNECTION_ADAPTER: 'TypeAdapter[ConnectionConfig]' = TypeAdapter(cast(Any, ConnectionConfig))


# A path that names something other than a file on this machine: a driver's
# own scheme, such as DuckDB's md: for MotherDuck. Two letters at least, so a
# drive letter would still read as a path.
_SCHEME_PREFIX = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]+:')


def isSchemePath(path: str) -> bool:

    return bool(_SCHEME_PREFIX.match(path))


def anchorPaths(settings: ConnectionConfig, directory: str) -> ConnectionConfig:
    """`settings` with a relative file path made relative to `directory`, the
    one connections.yaml is in, as the run state and history a jobs file names
    are relative to it: cron, a shell and CI then open the same file wherever
    they start. Absolute paths, `~`, `:memory:`, URLs and cloud roots are left
    as they are.
    """

    def anchored(path: str) -> str:
        expanded = os.path.expanduser(path)
        return expanded if os.path.isabs(expanded) else os.path.normpath(os.path.join(directory, expanded))

    if isinstance(settings, (SQLiteConnection, DuckDBConnection)):
        if settings.path == ':memory:' or isSchemePath(settings.path):
            return settings
        return settings.model_copy(update={'path': anchored(settings.path)})
    if isinstance(settings, FilesConnection) and settings.store() == FileStore.LOCAL:
        return settings.model_copy(update={'root': anchored(settings.root)})
    if (isinstance(settings, IcebergConnection) and settings.warehouse is not None and settings.store() == FileStore.LOCAL
            and not settings.warehouse.startswith('file://')):
        return settings.model_copy(update={'warehouse': anchored(settings.warehouse)})

    return settings


def _listed(names: Sequence[str]) -> str:

    return names[0] if len(names) == 1 else '{} and {}'.format(', '.join(names[:-1]), names[-1])

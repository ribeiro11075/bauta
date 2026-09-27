"""The configuration's pydantic models -- connection aliases, jobs, discovery
rules -- and the validation that turns loaded YAML into them.
"""
from __future__ import annotations

import os
import re
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Mapping, Optional, Sequence, Set, Tuple, Type, TypeVar, Union, cast

from pydantic import BaseModel, BeforeValidator, ByteSize, ConfigDict, Field, SecretStr, TypeAdapter, ValidationError, field_validator, model_validator

from ..masking import changesValues, policyFor, validateColumnPolicy, validateKey
from .environment import ConfigurationError, runPasswordCommand, splitPasswordCommand
from .fileTypes import parseColumnType

# A job's rows per batch when it names none. What `discover` and `subset`
# generate, so a hand-written job behaves like a generated one.
DEFAULT_CHUNK_SIZE = 5000

# Worker processes when jobs.yaml names none. One: jobs run in order, which is
# what a first configuration wants and what a single-job file needs.
DEFAULT_WORKERS = 1

# Top-level keys a file keeps only to hang YAML anchors from, as docker-compose
# uses them: `x-defaults: &defaults` above, `<<: *defaults` in each job. They
# are dropped before validation rather than read as configuration.
ANCHOR_KEY_PREFIX = 'x-'


def isAnchorKey(name: Any) -> bool:
    """Whether a top-level key only holds a YAML anchor."""

    return isinstance(name, str) and name.startswith(ANCHOR_KEY_PREFIX)


def _withoutAnchorKeys(value: Any) -> Any:
    """The mapping without the keys that only hold anchors."""

    if isinstance(value, dict):
        return {key: item for key, item in value.items() if not isAnchorKey(key)}

    return value


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


WATERMARK_PLACEHOLDER = re.compile(r'\{\{\s*watermark\s*\}\}')


class InsertStrategy(str, Enum):
    SWAP = 'swap'
    UPSERT = 'upsert'
    # A files connection's two: a run adds new files beside the ones already
    # there, or publishes a whole new snapshot of the table.
    APPEND = 'append'
    OVERWRITE = 'overwrite'


# The strategies that write files, and the only ones a files connection takes.
FILE_STRATEGIES = frozenset({InsertStrategy.APPEND, InsertStrategy.OVERWRITE})


class StoreType(str, Enum):
    """The connections that aren't databases."""

    FILES = 'files'


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


class FilesConnection(_BaseConnection):
    """A directory -- on this machine, or in S3, Google Cloud Storage or
    Azure Blob Storage -- that jobs write tables of files into, one directory
    per table: `targetTableFinal` is its path under `root`. Written, never
    read: a files connection is a target only. See "Files as a target" in
    docs/design.md.

    `fileSize` is where a part is closed and the next begun; `rowGroupSize`
    is how much of a table is held in memory, before compression, and written
    at once -- one Parquet row group. `keepSnapshots` is how many of an
    overwrite job's complete snapshots stay, the newest included.

    In each cloud, credentials come from its own default chain -- what its
    command-line tool would find: the environment, a profile or login, the
    machine's or the pod's identity -- unless settings give them. `endpoint`
    points at another service speaking the same API, such as MinIO or R2.
    """

    type: Literal[StoreType.FILES] = StoreType.FILES
    root: str = Field(min_length=1)
    format: FileFormat = FileFormat.PARQUET
    # None is the format's default: FORMAT_COMPRESSIONS.
    compression: Optional[FileCompression] = None
    # CSV's alone; a comma when not given.
    delimiter: Optional[str] = None
    fileSize: ByteSize = ByteSize(DEFAULT_FILE_SIZE)
    rowGroupSize: ByteSize = ByteSize(DEFAULT_ROW_GROUP_SIZE)
    keepSnapshots: int = Field(default=DEFAULT_KEEP_SNAPSHOTS, ge=1)
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

    @field_validator('root')
    @classmethod
    def _supportedRoot(cls, root: str) -> str:
        """A directory on this machine, or a bucket or container, with a
        prefix or without. Any other URL is refused rather than read as a
        relative directory named `hdfs:`.
        """

        scheme = next((scheme for scheme in ROOT_SCHEMES if root.startswith(scheme)), None)

        if scheme is None:
            if '://' in root:
                raise ValueError('root must be a directory, or begin with {}; {} is not supported'.format(
                    ', '.join(sorted(ROOT_SCHEMES)), root.split('://', 1)[0] + '://'))
            return root

        if scheme.startswith('abfs'):
            if not _ABFS.match(root):
                raise ValueError('root {!r} is not an Azure location; write abfss://container@account.dfs.core.windows.net/prefix, or '
                                 'az://container/prefix with accountName'.format(root))
        elif not root[len(scheme):].split('/', 1)[0]:
            raise ValueError('root {!r} names no {}; write {}name or {}name/prefix'.format(
                root, 'container' if scheme == 'az://' else 'bucket', scheme, scheme))

        return root.rstrip('/')

    @model_validator(mode='after')
    def _coherentSettings(self) -> 'FilesConnection':

        store = self.store()
        for other, names in STORE_SETTINGS.items():
            given = [name for name in names if getattr(self, name) is not None and name not in STORE_SETTINGS.get(store, ())]
            if given:
                where = 'a directory on this machine' if store == FileStore.LOCAL else 'on {}'.format(_STORE_NAMES[store])
                raise ValueError('{} {} for a root on {}, and root is {}'.format(
                    _listed(given), 'is' if len(given) == 1 else 'are', _STORE_NAMES[other], where))

        if store == FileStore.S3:
            if (self.accessKeyId is None) != (self.secretAccessKey is None):
                raise ValueError('set accessKeyId and secretAccessKey together, or neither to use the AWS default credential chain')
            if self.sessionToken is not None and self.accessKeyId is None:
                raise ValueError('sessionToken goes with accessKeyId and secretAccessKey')
            if self.fileSize > S3_LARGEST_COPY:
                raise ValueError('fileSize can be at most 5GiB on S3, which copies no larger an object in one request, and a part is '
                                 'published by copying it')

        if store == FileStore.GCS and self.anonymous and self.serviceAccount is not None:
            raise ValueError('anonymous and serviceAccount say two different things; set one')

        if store == FileStore.AZURE:
            match = _ABFS.match(self.root)
            if match and self.accountName is not None and self.accountName != match.group(2):
                raise ValueError('accountName {} is not the account root names, {}'.format(self.accountName, match.group(2)))
            if not match and self.accountName is None:
                raise ValueError('an az:// root needs accountName, the storage account the container is in')
            principal = [name for name in ('clientId', 'clientSecret', 'tenantId') if getattr(self, name) is not None]
            if principal and len(principal) != 3:
                raise ValueError('a service principal needs clientId, clientSecret and tenantId together')
            ways = [name for name in ('accountKey', 'sasToken') if getattr(self, name) is not None] + (['a service principal'] if principal else [])
            if len(ways) > 1:
                raise ValueError('{} are each a way to sign in; set one, or none to use the Azure default credential chain'.format(_listed(ways)))

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

    def store(self) -> FileStore:

        return next((store for scheme, store in ROOT_SCHEMES.items() if self.root.startswith(scheme)), FileStore.LOCAL)

    def isObjectStore(self) -> bool:

        return self.store() != FileStore.LOCAL

    def bucketPath(self) -> str:
        """The root as its store's filesystem names it: `bucket/prefix`, or
        `container/prefix` on Azure.
        """

        match = _ABFS.match(self.root)
        if match:
            return match.group(1) + (match.group(3) or '').rstrip('/')

        return self.root.split('://', 1)[1].rstrip('/')

    def azureAccount(self) -> Optional[str]:

        match = _ABFS.match(self.root)

        return match.group(2) if match else self.accountName

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


_CONNECTION_MODELS: Dict[Union[DatabaseType, StoreType], Type[_BaseConnection]] = {
    DatabaseType.POSTGRESQL: PostgreSQLConnection, DatabaseType.MYSQL: MySQLConnection, DatabaseType.MARIADB: MariaDBConnection,
    DatabaseType.MSSQL: MSSQLConnection, DatabaseType.ORACLE: OracleConnection, DatabaseType.SQLITE: SQLiteConnection,
    DatabaseType.DUCKDB: DuckDBConnection, StoreType.FILES: FilesConnection,
    }

# One database in connections.yaml.
DatabaseConfig = Union[PostgreSQLConnection, MySQLConnection, MariaDBConnection, MSSQLConnection, OracleConnection, SQLiteConnection, DuckDBConnection]

# One connection in connections.yaml: the model its `type` names.
ConnectionConfig = Annotated[Union[PostgreSQLConnection, MySQLConnection, MariaDBConnection, MSSQLConnection, OracleConnection,
                                   SQLiteConnection, DuckDBConnection, FilesConnection], Field(discriminator='type')]

# Cast, since pydantic's stubs before 2.7 take a class here and not a union.
_CONNECTION_ADAPTER: 'TypeAdapter[ConnectionConfig]' = TypeAdapter(cast(Any, ConnectionConfig))


def _listed(names: Sequence[str]) -> str:

    return names[0] if len(names) == 1 else '{} and {}'.format(', '.join(names[:-1]), names[-1])


def connectionConfig(**settings: Any) -> ConnectionConfig:
    """One connection from its settings, as connections.yaml would give them,
    validated into the model its `type` names. Invalid settings raise
    ConfigurationError, worded as for connections.yaml.
    """

    return Configuration.validateConnection(settings, '{} connection settings'.format(getattr(settings.get('type'), 'value', settings.get('type'))))


class BaseJobConfig(BaseModel):
    """An unknown key is an error, inherited by every kind of job: a misspelled
    `masking` block that was quietly ignored would copy the source unmasked.
    """

    model_config = ConfigDict(extra='forbid')

    # A job written down is a job meant to run; `active: false` is the case
    # worth saying out loud.
    active: bool = True
    refresh: Optional[int] = None
    predecessors: CleanedStringList = Field(default_factory=list)


class MaskingConfig(BaseModel):
    """A job's masking policy, normalized here so a bad strategy or option
    fails `bauta validate` rather than a run. An unknown key is an error, so a
    misspelled option can't leave a column masked some other way in silence.
    """

    model_config = ConfigDict(extra='forbid')

    key: SecretStr
    columns: Dict[str, Any]
    defaultStrategy: Optional[Any] = None

    @field_validator('key')
    @classmethod
    def _requireStrongKey(cls, key: SecretStr) -> SecretStr:

        validateKey(key.get_secret_value())

        return key


    @field_validator('columns')
    @classmethod
    def _validateColumns(cls, columns: Dict[str, Any]) -> Dict[str, Any]:

        normalized = {}
        problems = []
        folded: Dict[str, str] = {}

        for column, policy in columns.items():
            try:
                normalized[column] = validateColumnPolicy(policy)
            except ValueError as error:
                problems.append('{}: {}'.format(column, error))
            if column.upper() in folded:
                problems.append('{}: differs only in case from {} -- column names match case-insensitively'.format(column, folded[column.upper()]))
            folded[column.upper()] = column

        if problems:
            raise ValueError('; '.join(problems))

        return normalized


    @field_validator('defaultStrategy')
    @classmethod
    def _validateDefaultStrategy(cls, policy: Any) -> Any:

        return None if policy is None else validateColumnPolicy(policy)


def _names(table: str) -> Tuple[str, str]:
    """A table's schema and name, upper-cased and without the quotes a
    reserved word or a folded name needs, for comparing two names a job gives.

    databaseDialects is imported here rather than at the top because it
    imports this module; which database a job's alias names isn't known at
    validation time either, so the case a quoted name asked for is set aside.
    """

    from ..database.dialects import bareName, splitTableName

    schema, name = splitTableName(table)

    return (bareName(schema).upper() if schema else ''), bareName(name).upper()


class DataJobConfig(BaseJobConfig):
    sourceConnection: str
    sourceQuery: str
    targetColumns: CleanedStringList = Field(default_factory=list)
    sourceQueryColumnTransforms: CleanedListMapping = Field(default_factory=dict)
    targetConnection: str
    targetTableStage: Optional[str] = None
    targetTableFinal: str
    insertStrategy: InsertStrategy
    chunkSize: int = Field(default=DEFAULT_CHUNK_SIZE, ge=1)
    watermarkColumn: Optional[str] = None
    watermarkInitial: Optional[Any] = None
    retries: int = 0
    retryDelaySeconds: float = 5.0
    timeoutSeconds: Optional[float] = Field(default=None, gt=0)
    masking: Optional[MaskingConfig] = None
    # The explicit way to say a job was reviewed and copies as it stands, as
    # `keep` says it of a column. Without it, `audit` reports every unmasked
    # job, since copying unmasked is a choice a reviewer has to see.
    unmasked: bool = False
    preTargetAdhocQueries: CleanedStringList = Field(default_factory=list)
    postTargetAdhocQueries: CleanedStringList = Field(default_factory=list)
    # A file target's alone. Column -> type, for a column whose values don't
    # settle its type, or settle it as something else; see
    # configuration.fileTypes. singleFile writes an overwrite job's table as
    # one file, <targetTableFinal>.parquet, replaced whole each run.
    targetColumnTypes: CleanedMapping = Field(default_factory=dict)
    singleFile: bool = False

    @field_validator('targetColumnTypes')
    @classmethod
    def _parseColumnTypes(cls, declared: Dict[str, Any]) -> Dict[str, Any]:

        problems = []
        folded: Dict[str, str] = {}
        for column, text in declared.items():
            if not isinstance(text, str):
                problems.append('{}: a type is text, such as int64 or decimal(18,2), got {!r}'.format(column, text))
            else:
                try:
                    parseColumnType(text)
                except ValueError as error:
                    problems.append('{}: {}'.format(column, error))
            if column.upper() in folded:
                problems.append('{}: differs only in case from {} -- column names match case-insensitively'.format(column, folded[column.upper()]))
            folded[column.upper()] = column

        if problems:
            raise ValueError('; '.join(problems))

        return declared


    @model_validator(mode='after')
    def _fileSettingsNeedAFileStrategy(self) -> 'DataJobConfig':
        """What only a file target has, or what only a table has, given to the
        other kind is an error rather than ignored.
        """

        writesFiles = self.insertStrategy in FILE_STRATEGIES

        if writesFiles:
            tableOnly = [name for name in ('targetTableStage', 'preTargetAdhocQueries', 'postTargetAdhocQueries') if getattr(self, name)]
            if tableOnly:
                raise ValueError('{} {} for a table in a database; insertStrategy: {} writes files'.format(
                    _listed(tableOnly), 'is' if len(tableOnly) == 1 else 'are', self.insertStrategy.value))
            problem = filePathProblem(self.targetTableFinal)
            if problem:
                raise ValueError('targetTableFinal {!r} {}'.format(self.targetTableFinal, problem))
        else:
            fileOnly = [name for name in ('targetColumnTypes', 'singleFile') if getattr(self, name)]
            if fileOnly:
                raise ValueError('{} {} for a file target, with insertStrategy append or overwrite'.format(
                    _listed(fileOnly), 'is' if len(fileOnly) == 1 else 'are'))

        if self.singleFile and self.insertStrategy != InsertStrategy.OVERWRITE:
            raise ValueError('singleFile needs insertStrategy: overwrite -- a file can be replaced, not added to')

        return self


    @model_validator(mode='after')
    def _rejectUnmaskedWithMasking(self) -> 'DataJobConfig':
        """`unmasked` says the job copies as it stands, so a masking policy
        beside it says two different things about the same rows.
        """

        if self.unmasked and self.masking is not None:
            raise ValueError('unmasked is set on a job that also has a masking policy; unmasked says the job copies its rows as they '
                             'stand, so remove one of the two')

        return self


    @model_validator(mode='after')
    def _requireSeparateStageTable(self) -> 'DataJobConfig':
        """A stage table naming the target would have the target truncated
        before each load. Compared case-insensitively and without the quotes a
        name may need; a qualified and an unqualified name for the same table
        still slip through.
        """

        if self.targetTableStage is not None and _names(self.targetTableStage) == _names(self.targetTableFinal):
            raise ValueError('targetTableStage must be a different table from targetTableFinal: the stage table is emptied before each load')

        return self


    @model_validator(mode='after')
    def _requireStageTableForSwap(self) -> 'DataJobConfig':
        """A rename never moves a table between schemas, so a swap's stage
        table must share the target's.
        """

        if self.insertStrategy != InsertStrategy.SWAP:
            return self

        if not self.targetTableStage:
            raise ValueError('targetTableStage is required when insertStrategy is swap')

        stageSchema, finalSchema = (_names(table)[0] for table in (self.targetTableStage, self.targetTableFinal))
        if stageSchema != finalSchema:
            raise ValueError('targetTableStage and targetTableFinal must be in the same schema for insertStrategy: swap, '
                             'since a rename cannot move a table between schemas')

        return self


    @model_validator(mode='after')
    def _requireNonNegativeRetries(self) -> 'DataJobConfig':

        if self.retries < 0:
            raise ValueError('retries cannot be negative')
        if self.retryDelaySeconds < 0:
            raise ValueError('retryDelaySeconds cannot be negative')

        return self


    @model_validator(mode='after')
    def _requireCoherentWatermarkConfiguration(self) -> 'DataJobConfig':
        """watermarkColumn and the {{ watermark }} token need each other, and
        watermarkInitial is required: binding None would match no rows, forever.
        """

        hasPlaceholder = bool(WATERMARK_PLACEHOLDER.search(self.sourceQuery))

        if self.watermarkColumn and not hasPlaceholder:
            raise ValueError('watermarkColumn is set but sourceQuery has no {{ watermark }} placeholder to bind it into')

        if hasPlaceholder and not self.watermarkColumn:
            raise ValueError('sourceQuery has a {{ watermark }} placeholder but watermarkColumn is not set')

        if self.watermarkColumn and self.watermarkInitial is None:
            raise ValueError('watermarkInitial is required when watermarkColumn is set -- the first run has no stored watermark to bind')

        return self


    @model_validator(mode='after')
    def _rejectMaskedWatermarkColumn(self) -> 'DataJobConfig':
        """The watermark is read from the raw rows, before masking, and then
        logged, kept in run state and shown by `bauta jobs`. A masked column
        there would leak the values the job exists to hide.
        """

        if self.watermarkColumn and self.masking is not None:
            # The watermark column is always among the columns the query
            # returns (a run refuses otherwise), so one the policy doesn't name
            # falls to defaultStrategy -- decidable here, without connecting.
            policy = policyFor(self.watermarkColumn, self.masking.columns, self.masking.defaultStrategy)
            if policy is not None and changesValues(policy):
                how = policy['strategy']
                if policy is self.masking.defaultStrategy:
                    how = '{} (its defaultStrategy)'.format(how)
                raise ValueError('watermarkColumn "{}" is masked with {} -- the watermark is read before masking and kept in run state and logs, '
                                 'so it would leak the unmasked value. Watermark on a column masked with keep, or on another column'.format(
                                     self.watermarkColumn, how))

        return self


    @model_validator(mode='after')
    def _rejectWatermarkWithSwap(self) -> 'DataJobConfig':
        """A watermark needs upsert, or append for files: a swap or an
        overwrite would replace the target with only the rows that changed.
        """

        if self.watermarkColumn and self.insertStrategy not in (InsertStrategy.UPSERT, InsertStrategy.APPEND):
            raise ValueError('watermarkColumn requires insertStrategy: upsert, or append for a file target -- {} would replace the whole '
                             'target with only the rows that changed'.format(self.insertStrategy.value))

        return self


    def writesFiles(self) -> bool:

        return self.insertStrategy in FILE_STRATEGIES


def filePathProblem(path: str) -> Optional[str]:
    """What is wrong with `path` as a table's directory under a files
    connection's root, or None.

    It must stay under the root, and no part of it may begin with `_` or `.`:
    an engine discovering a directory tree -- Spark, Hive, pyarrow -- skips
    such names, which is what keeps bauta's own staging out of sight, so a
    table named that way would be skipped too.
    """

    if path.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:', path):
        return 'must be a path under the connection\'s root, not an absolute one'

    parts = path.replace('\\', '/').split('/')
    if any(part in ('', '.', '..') for part in parts):
        return 'must be a path under the connection\'s root, such as sales/orders, without empty, . or .. parts'
    if any(part.startswith(('_', '.')) for part in parts):
        return 'has a part beginning with _ or ., which engines reading a directory tree skip as hidden, and bauta keeps for its own staging'

    return None


def targetMismatch(job: DataJobConfig, connection: Any) -> Optional[str]:
    """Why `job` can't write to `connection`, or None: files take append and
    overwrite, and a database the rest.
    """

    if isinstance(connection, FilesConnection) and not job.writesFiles():
        return 'is a files connection, which takes insertStrategy append or overwrite, not {}'.format(job.insertStrategy.value)
    if not isinstance(connection, FilesConnection) and job.writesFiles():
        return 'is a {} database, and insertStrategy: {} writes files -- use swap or upsert, or a files connection'.format(
            connection.type.value, job.insertStrategy.value)

    return None


class NameRuleConfig(BaseModel):
    """A discovery.yaml rule on column names."""

    model_config = ConfigDict(extra='forbid')

    words: List[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    # A strategy name alone, or a mapping with a `strategy`, as in jobs.yaml.
    policy: Dict[str, Any]
    reason: Optional[str] = Field(default=None, min_length=1)

    @field_validator('words')
    @classmethod
    def _wordsHaveLetters(cls, words: List[str]) -> List[str]:

        empty = [word for word in words if not re.search(r'[A-Za-z0-9]', word)]
        if empty:
            raise ValueError('a word needs a letter or digit: {}'.format(', '.join(repr(word) for word in empty)))

        return words

    @field_validator('policy', mode='before')
    @classmethod
    def _policyIsValid(cls, policy: Any) -> Dict[str, Any]:

        return validateColumnPolicy(policy)


class ValueRuleConfig(BaseModel):
    """A discovery.yaml rule on sampled values: a regular expression the whole
    value must match.
    """

    model_config = ConfigDict(extra='forbid')

    pattern: str = Field(min_length=1)
    # A strategy name alone, or a mapping with a `strategy`, as in jobs.yaml.
    policy: Dict[str, Any]
    reason: Optional[str] = Field(default=None, min_length=1)

    @field_validator('pattern')
    @classmethod
    def _patternCompiles(cls, pattern: str) -> str:

        try:
            re.compile(pattern)
        except re.error as error:
            raise ValueError('not a valid regular expression: {}'.format(error)) from error

        return pattern

    @field_validator('policy', mode='before')
    @classmethod
    def _policyIsValid(cls, policy: Any) -> Dict[str, Any]:

        return validateColumnPolicy(policy)


class DiscoveryRulesFile(BaseModel):
    """discovery.yaml: rules of your own for what personal data looks like,
    checked before the built-in ones in builtinDiscovery.
    """

    model_config = ConfigDict(extra='forbid')

    builtins: bool = True
    exclude: List[str] = Field(default_factory=list)
    names: List[NameRuleConfig] = Field(default_factory=list)
    values: List[ValueRuleConfig] = Field(default_factory=list)
    personalTables: List[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)

    @field_validator('exclude')
    @classmethod
    def _excludeNamesBuiltins(cls, exclude: List[str]) -> List[str]:

        from ..generate.builtinDiscovery import RULE_NAMES

        unknown = sorted(set(exclude) - RULE_NAMES)
        if unknown:
            raise ValueError('no built-in rule named {}. Built-in rules: {}'.format(', '.join(unknown), ', '.join(sorted(RULE_NAMES))))

        return exclude


class TableLocation(BaseModel):
    """A table in one of connections.yaml's aliases, to keep run state, history or
    manifests in rather than a file. `table` defaults per use.
    """

    model_config = ConfigDict(extra='forbid')

    connection: str = Field(min_length=1)
    table: Optional[str] = Field(default=None, min_length=1)


# A file, relative to the jobs file, or a table.
StorageLocation = Union[Annotated[str, Field(min_length=1)], TableLocation]


# What a file-level `defaults:` block may supply to every job. The plumbing
# only: which databases, how rows are written, and the retry and timeout
# settings. A job that names any of these itself keeps its own value.
DEFAULTABLE_JOB_FIELDS = frozenset({
    'active', 'refresh', 'sourceConnection', 'targetConnection', 'insertStrategy',
    'chunkSize', 'retries', 'retryDelaySeconds', 'timeoutSeconds',
    })

# Within `masking`, the key alone. A key reference is not a policy: `columns`
# stays with its job, so a reviewer can read what one job does to its data
# without holding the whole file in their head.
DEFAULTABLE_MASKING_FIELDS = frozenset({'key'})

DEFAULTS_KEY = 'defaults'
MASKING_KEY = 'masking'


def _checkDefaultsKeys(defaults: Mapping[str, Any]) -> None:
    """Rejects a setting `defaults:` may not supply, naming what it may."""
    unknown = sorted(set(defaults) - DEFAULTABLE_JOB_FIELDS - {MASKING_KEY})
    if unknown:
        raise ValueError('{} cannot be set in defaults: {}. defaults may set {}, and masking.key'.format(
            'these settings' if len(unknown) > 1 else 'this setting', ', '.join(unknown),
            ', '.join(sorted(DEFAULTABLE_JOB_FIELDS))))

    masking = defaults.get(MASKING_KEY)
    if masking is None:
        return
    if not isinstance(masking, Mapping):
        raise ValueError('defaults.masking must be a mapping holding a key')

    unknown = sorted(set(masking) - DEFAULTABLE_MASKING_FIELDS)
    if unknown:
        raise ValueError('defaults.masking may only set {}, not {}. A masking policy stays with its job, '
                         'so that what a job does to its data can be read in one place'.format(
                             ', '.join(sorted(DEFAULTABLE_MASKING_FIELDS)), ', '.join(unknown)))


def _jobWithDefaults(job: Any, defaults: Mapping[str, Any]) -> Any:
    """One job's settings, with anything it doesn't name taken from `defaults`.

    A job that masks takes the default key when it doesn't give one. A job with
    no `masking` block doesn't grow one: an unmasked job must stay visibly
    unmasked, rather than becoming a key with no policy.
    """

    if not isinstance(job, Mapping):
        return job

    merged = {name: value for name, value in defaults.items() if name in DEFAULTABLE_JOB_FIELDS}
    merged.update(job)

    defaultMasking = defaults.get(MASKING_KEY)
    jobMasking = job.get(MASKING_KEY)
    if isinstance(defaultMasking, Mapping) and isinstance(jobMasking, Mapping):
        combined = {name: value for name, value in defaultMasking.items() if name in DEFAULTABLE_MASKING_FIELDS}
        combined.update(jobMasking)
        merged[MASKING_KEY] = combined

    return merged


class DataJobsFile(BaseModel):
    """An unknown key is an error, apart from the `x-` keys that hold nothing
    but YAML anchors.

    `defaults:` supplies what every job would otherwise repeat; see
    DEFAULTABLE_JOB_FIELDS.
    """

    model_config = ConfigDict(extra='forbid')

    workers: int = Field(default=DEFAULT_WORKERS, ge=1)
    cycleSleepSeconds: float = 0.5
    # Where the CLI keeps run state, records history and writes the masking
    # manifest; command-line flags override each. See cli._resolveLocation.
    memory: Optional[StorageLocation] = None
    history: Optional[StorageLocation] = None
    manifest: Optional[StorageLocation] = None
    # Threads the native masker spreads each job's chunks over: one by default,
    # a number up to the cores available, or `auto`, which shares the cores as
    # each job starts with the jobs running alongside it. See
    # masking.maskingThreadsFor and runner._runCycle.
    maskingThreads: Union[Literal['auto'], Annotated[int, Field(ge=1)]] = 1
    # Tables no job copies, on purpose: connection alias -> table -> why. What
    # `bauta coverage` reads, so a table left out is a decision on the page
    # rather than something nobody noticed.
    acknowledged: Dict[str, Dict[str, Annotated[str, Field(min_length=1)]]] = Field(default_factory=dict)
    jobs: Dict[str, DataJobConfig]

    @model_validator(mode='before')
    @classmethod
    def _applyFileLevelKeys(cls, value: Any) -> Any:
        """Drops the `x-` keys that hold nothing but YAML anchors, then spreads
        `defaults:` over the jobs, so what follows validates whole jobs.
        """

        value = _withoutAnchorKeys(value)
        if not isinstance(value, Mapping):
            return value

        settings = dict(value)
        defaults = settings.pop(DEFAULTS_KEY, None) or {}
        if not isinstance(defaults, Mapping):
            raise ValueError('defaults must be a mapping of job settings')
        _checkDefaultsKeys(defaults)

        jobs = settings.get('jobs')
        if defaults and isinstance(jobs, Mapping):
            settings['jobs'] = {name: _jobWithDefaults(job, defaults) for name, job in jobs.items()}

        return settings


    def tableLocations(self) -> Dict[str, TableLocation]:
        """The settings that name a table, by setting."""

        return {name: location for name, location in (('memory', self.memory), ('history', self.history), ('manifest', self.manifest))
                if isinstance(location, TableLocation)}


def findCycle(predecessors: Mapping[str, Sequence[str]]) -> Optional[List[str]]:
    """A cycle in a job -> predecessors graph, as a path that ends where it
    starts, or None. Predecessors that aren't keys are ignored.
    """

    state: Dict[str, int] = {}
    path: List[str] = []

    def visit(job: str) -> Optional[List[str]]:
        state[job] = 1
        path.append(job)
        for predecessor in predecessors[job]:
            if predecessor not in predecessors:
                continue
            if state.get(predecessor) == 1:
                return path[path.index(predecessor):] + [predecessor]
            if predecessor not in state:
                found = visit(predecessor)
                if found:
                    return found
        path.pop()
        state[job] = 2
        return None

    for job in sorted(predecessors):
        if job not in state:
            found = visit(job)
            if found:
                return found

    return None


T = TypeVar('T', bound=BaseModel)


class Configuration:
    """Validates already-loaded configuration data, from wherever the caller
    loaded it.
    """

    @staticmethod
    def _validate(schema: Type[T], rawConfiguration: Any, sourceDescription: str) -> T:

        try:
            return schema.model_validate(rawConfiguration)
        except ValidationError as error:
            raise Configuration._invalid(error, sourceDescription) from error


    @staticmethod
    def _invalid(error: ValidationError, sourceDescription: str, skipLocation: int = 0) -> ConfigurationError:
        """`skipLocation` leaves out the leading parts of each location, such as
        the name of the union member a connection was validated as.
        """

        messages = []
        for issue in error.errors():
            where = '.'.join(str(part) for part in issue['loc'][skipLocation:])
            message = issue['msg'][len('Value error, '):] if issue['msg'].startswith('Value error, ') else issue['msg']
            messages.append('{}: {}'.format(where, message) if where else message)

        return ConfigurationError(f'Invalid configuration in {sourceDescription}:\n' + '\n'.join(messages))


    @staticmethod
    def validateConnection(rawConnection: Any, sourceDescription: str) -> ConnectionConfig:
        """One connection, as the model its `type` names. The union's own
        location -- the type's name -- is left out of each message, which
        names the setting alone.
        """

        if isinstance(rawConnection, Mapping) and getattr(rawConnection.get('type'), 'value', rawConnection.get('type')) not in CONNECTION_TYPES:
            raise ConfigurationError('Invalid configuration in {}:\ntype: {!r} is not a connection type; choose from {}'.format(
                sourceDescription, rawConnection.get('type'), ', '.join(sorted(CONNECTION_TYPES))))

        try:
            return _CONNECTION_ADAPTER.validate_python(rawConnection)
        except ValidationError as error:
            raise Configuration._invalid(error, sourceDescription, skipLocation=1) from error


    @staticmethod
    def validateConnectionConfiguration(rawConfiguration: Dict[str, Any]) -> Dict[str, ConnectionConfig]:
        """Two DuckDB aliases for one file are refused: each would count its
        jobs separately, and two jobs would open the file at once.
        """

        connections = {
            alias: Configuration.validateConnection(connectionSettings, f'connections.yaml -> {alias}')
            for alias, connectionSettings in (rawConfiguration or {}).items() if not isAnchorKey(alias)
            }

        files: Dict[str, List[str]] = {}
        for alias, settings in connections.items():
            if isinstance(settings, DuckDBConnection) and settings.path != ':memory:':
                files.setdefault(os.path.realpath(settings.path), []).append(alias)

        shared = ['{} all name {}'.format(', '.join(aliases), path) for path, aliases in sorted(files.items()) if len(aliases) > 1]
        if shared:
            raise ConfigurationError('Invalid database configuration: DuckDB lets one process at a time open a file, and one job at a time '
                                     'runs per connection, so give each file one alias: ' + '; '.join(shared))

        return connections


    @staticmethod
    def validateJobConfiguration(rawConfiguration: Any, schema: Type[T]) -> T:

        return Configuration._validate(schema, rawConfiguration, schema.__name__)


    @staticmethod
    def validateDiscoveryRules(rawConfiguration: Any, sourceDescription: str = 'discovery.yaml') -> DiscoveryRulesFile:
        """An empty file is no rules of your own, not an error."""

        return Configuration._validate(DiscoveryRulesFile, rawConfiguration or {}, sourceDescription)


    @staticmethod
    def validateJobGraph(jobs: Mapping[str, BaseJobConfig], connectionAliases: Optional[Set[str]] = None,
                         connections: Optional[Mapping[str, ConnectionConfig]] = None) -> None:
        """`connections` gives the aliases and their settings, so a connection that
        requires masking can refuse a job that doesn't mask. `connectionAliases`
        is the names alone, for a caller that has nothing more.
        """

        if connections is not None and connectionAliases is None:
            connectionAliases = set(connections)

        problems: List[str] = []

        for jobName, job in jobs.items():

            if connections is not None and isinstance(job, DataJobConfig) and job.masking is None:
                for setting in ('sourceConnection', 'targetConnection'):
                    alias = getattr(job, setting)
                    connection = connections.get(alias)
                    if connection is not None and connection.requireMasking:
                        problems.append('{}: {} "{}" is configured with requireMasking, and this job has no masking policy. '
                                        'Add one naming every column sourceQuery returns -- `keep` for the ones that need no '
                                        'masking'.format(jobName, setting, alias))

            for predecessor in job.predecessors:
                if predecessor not in jobs:
                    problems.append(f'{jobName}: predecessor "{predecessor}" is not a known job')

            if connections is not None and isinstance(job, DataJobConfig):
                source, target = connections.get(job.sourceConnection), connections.get(job.targetConnection)
                if isinstance(source, FilesConnection):
                    problems.append('{}: sourceConnection "{}" is a files connection, which can only be written to'.format(jobName, job.sourceConnection))
                mismatch = None if target is None else targetMismatch(job, target)
                if mismatch:
                    problems.append('{}: targetConnection "{}" {}'.format(jobName, job.targetConnection, mismatch))

            if connectionAliases is not None and isinstance(job, DataJobConfig):
                if job.sourceConnection not in connectionAliases:
                    problems.append(f'{jobName}: sourceConnection "{job.sourceConnection}" is not a known connection alias')
                if job.targetConnection not in connectionAliases:
                    problems.append(f'{jobName}: targetConnection "{job.targetConnection}" is not a known connection alias')

        cycle = findCycle({jobName: job.predecessors for jobName, job in jobs.items()})
        if cycle:
            problems.append('predecessors form a cycle, so none of these jobs could ever start: {}'.format(' -> '.join(cycle)))

        if problems:
            raise ConfigurationError('Invalid job graph:\n' + '\n'.join(problems))

"""The configuration's pydantic models -- jobs, discovery rules, and the
jobs file that holds them -- and the validation that turns loaded YAML into
them and the connections of connections.py.
"""
from __future__ import annotations

import os
import re
from enum import Enum
from typing import Annotated, Any, Callable, Dict, List, Literal, Mapping, Optional, Sequence, Set, Tuple, Type, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator

from .sqltext import codeOnly
from ..masking import changesValues, policyFor, validateColumnPolicy, validateKey, warnIfWeakKey
from .connections import (_CONNECTION_ADAPTER, CONNECTION_TYPES, anchorPaths, CleanedListMapping, CleanedMapping, CleanedStringList, ConnectionConfig,
                          DuckDBConnection, FilesConnection, IcebergConnection, _listed)
from .environment import ConfigurationError
from .columnTypes import parseColumnType

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


WATERMARK_PLACEHOLDER = re.compile(r'\{\{\s*watermark\s*\}\}')

def watermarkPlaceholders(query: str) -> int:
    """How many {{ watermark }} placeholders `query` has outside its comments,
    string literals and quoted names: the ones bound.
    """

    return len(WATERMARK_PLACEHOLDER.findall(codeOnly(query, identifiers=True)))


class InsertStrategy(str, Enum):
    SWAP = 'swap'
    UPSERT = 'upsert'
    # A files connection's two: a run adds new files beside the ones already
    # there, or publishes a whole new snapshot of the table.
    APPEND = 'append'
    OVERWRITE = 'overwrite'


# The strategies that write files, and the only ones a files connection takes.
FILE_STRATEGIES = frozenset({InsertStrategy.APPEND, InsertStrategy.OVERWRITE})


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


def _eachColumn(columns: Mapping[str, Any], validate: Callable[[Any], Any]) -> Dict[str, Any]:
    """A column -> setting mapping with each setting validated, every problem
    said at once, and no two names differing only in case: column names
    match case-insensitively, so two such would be one column.
    """

    validated = {}
    problems = []
    folded: Dict[str, str] = {}

    for column, value in columns.items():
        try:
            validated[column] = validate(value)
        except ValueError as error:
            problems.append('{}: {}'.format(column, error))
        if column.upper() in folded:
            problems.append('{}: differs only in case from {} -- column names match case-insensitively'.format(column, folded[column.upper()]))
        folded[column.upper()] = column

    if problems:
        raise ValueError('; '.join(problems))

    return validated


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
        warnIfWeakKey(key.get_secret_value())

        return key


    @field_validator('columns')
    @classmethod
    def _validateColumns(cls, columns: Dict[str, Any]) -> Dict[str, Any]:

        return _eachColumn(columns, validateColumnPolicy)


    @field_validator('defaultStrategy')
    @classmethod
    def _validateDefaultStrategy(cls, policy: Any) -> Any:

        return None if policy is None else validateColumnPolicy(policy)


class PartitionsConfig(BaseModel):
    """A job read, masked and written as `count` slices at once, each a range
    of the numeric `column` its sourceQuery returns. See "Partitions" in
    docs/concepts/how-it-works.md.

    At least two: one slice is the job without partitions, which says so more
    plainly by leaving the setting out. `count: auto` lets the job choose, as
    it starts; see jobs.partitions.automaticCount. `partitions: auto` chooses
    the column too, and is held as a PartitionsConfig with no column.
    """

    model_config = ConfigDict(extra='forbid')

    column: Optional[str] = Field(default=None, min_length=1)
    count: Union[Literal['auto'], Annotated[int, Field(ge=2)]]

    @model_validator(mode='before')
    @classmethod
    def _fromShorthand(cls, value: Any) -> Any:

        if value == AUTOMATIC:
            return {'count': AUTOMATIC, 'column': None}
        if isinstance(value, Mapping) and value.get('column') is None:
            raise ValueError('partitions names a column and a count, or is `auto` to choose both')

        return value


    @property
    def automatic(self) -> bool:
        """Whether the job chooses how many slices, or the column too."""

        return self.count == AUTOMATIC


AUTOMATIC = 'auto'


def _passesUnnamedColumns(masking: MaskingConfig) -> bool:
    """Whether a policy's defaultStrategy copies the columns it doesn't name
    as they are: `keep`, or a custom strategy that says it passes values through.
    """

    from ..masking import changesValues

    return masking.defaultStrategy is not None and not changesValues(masking.defaultStrategy)


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
    # configuration.columnTypes. singleFile writes an overwrite job's table as
    # one file, <targetTableFinal>.parquet, replaced whole each run.
    targetColumnTypes: CleanedMapping = Field(default_factory=dict)
    singleFile: bool = False
    # An Iceberg target's alone: the columns an upsert matches rows by, and
    # the identifier fields of a table the job creates. An existing table's
    # own identifier fields serve without it.
    targetKey: CleanedStringList = Field(default_factory=list)
    # A database target's alone: the job's rows read, masked and written as
    # several slices at once. See PartitionsConfig. `auto` is ignored for a
    # files or Iceberg target, so it can sit under `defaults`.
    partitions: Optional[PartitionsConfig] = None

    @field_validator('targetColumnTypes')
    @classmethod
    def _parseColumnTypes(cls, declared: Dict[str, Any]) -> Dict[str, Any]:

        def checked(text: Any) -> Any:
            if not isinstance(text, str):
                raise ValueError('a type is text, such as int64 or decimal(18,2), got {!r}'.format(text))
            parseColumnType(text)
            return text

        return _eachColumn(declared, checked)


    @model_validator(mode='after')
    def _lakeSettingsFitTheStrategy(self) -> 'DataJobConfig':
        """What the strategy alone rules out. Append and overwrite write a lake,
        which has no stage table and runs no SQL; which settings fit the target
        otherwise is for targetProblems, once the connection is known.
        """

        if self.insertStrategy in FILE_STRATEGIES:
            tableOnly = [name for name in ('targetTableStage', 'preTargetAdhocQueries', 'postTargetAdhocQueries', 'partitions')
                         if getattr(self, name) and not (name == 'partitions' and self.partitions is not None and self.partitions.column is None)]
            if tableOnly:
                raise ValueError('{} {} for a table in a database; insertStrategy: {} writes to files or Iceberg'.format(
                    _listed(tableOnly), 'is' if len(tableOnly) == 1 else 'are', self.insertStrategy.value))

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

        # Outside comments and literals: one only in a comment is bound to
        # nothing, so the job would read every row on every run.
        hasPlaceholder = watermarkPlaceholders(self.sourceQuery) > 0

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


def _requiredMaskingProblems(job: DataJobConfig, connections: Mapping[str, ConnectionConfig]) -> List[str]:
    """A job without a masking policy, or one whose defaultStrategy copies
    what it doesn't name, reading or writing a connection with requireMasking.
    """

    if job.masking is not None and not _passesUnnamedColumns(job.masking):
        return []

    problems = []
    for setting in ('sourceConnection', 'targetConnection'):
        alias = getattr(job, setting)
        connection = connections.get(alias)
        if connection is None or not connection.requireMasking:
            continue
        if job.masking is None:
            problems.append('{} "{}" is configured with requireMasking, and this job has no masking policy. Add one naming every column '
                            'sourceQuery returns -- `keep` for the ones that need no masking'.format(setting, alias))
        else:
            # Every column named is a decision someone made; a passthrough
            # default copies the ones nobody did, a column production adds
            # later included.
            problems.append('{} "{}" is configured with requireMasking, and this job\'s defaultStrategy copies every column its policy '
                            'doesn\'t name as it is. Name each column -- `keep` for the ones that need no masking -- or give '
                            'defaultStrategy one that masks'.format(setting, alias))

    return problems


def _incrementalShuffleProblems(job: DataJobConfig, connections: Mapping[str, ConnectionConfig]) -> List[str]:
    """`shuffle` on an incremental job reading or writing a connection with
    requireMasking. shuffle moves values between the rows of one chunk, and
    an incremental run's chunks are what changed since the last: often a row
    or two, which keep their own values, or swap them between two people.
    """

    if not job.watermarkColumn or job.masking is None:
        return []

    shuffled = sorted(column for column, policy in job.masking.columns.items() if policy.get('strategy') == 'shuffle')
    if job.masking.defaultStrategy and job.masking.defaultStrategy.get('strategy') == 'shuffle':
        shuffled.append('its defaultStrategy')
    if not shuffled:
        return []

    return ['{} "{}" is configured with requireMasking, and this incremental job shuffles {}: its chunks hold only the rows that changed, '
            'often one, whose value shuffle leaves where it is. Mask with a strategy that replaces each value'.format(
                setting, getattr(job, setting), ', '.join(shuffled))
            for setting in ('sourceConnection', 'targetConnection')
            if connections.get(getattr(job, setting)) is not None and connections[getattr(job, setting)].requireMasking]


def _connectionProblems(job: DataJobConfig, connections: Mapping[str, ConnectionConfig]) -> List[str]:
    """What the job's connections, as configured, rule out: writing to a
    readOnly one, reading from files or Iceberg, settings its target doesn't
    take, and more partitions than its connections allow jobs.
    """

    source, target = connections.get(job.sourceConnection), connections.get(job.targetConnection)
    problems = []
    if getattr(target, 'readOnly', False):
        problems.append('targetConnection "{}" is readOnly, so nothing may be written to it'.format(job.targetConnection))
    if isLake(source):
        problems.append('sourceConnection "{}" is {}, which can only be written to'.format(job.sourceConnection, _TARGET_NAMES[targetKind(source)]))
    problems.extend('targetConnection "{}" {}'.format(job.targetConnection, problem) for problem in ([] if target is None else targetProblems(job, target)))
    problems.extend(partitionLimitProblems(job, connections))

    return problems


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


# A plain identifier: an Iceberg table's name or namespace, or a key column.
_ICEBERG_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

_STRATEGIES = {
    'database': (InsertStrategy.SWAP, InsertStrategy.UPSERT),
    'files': (InsertStrategy.APPEND, InsertStrategy.OVERWRITE),
    'iceberg': (InsertStrategy.APPEND, InsertStrategy.OVERWRITE, InsertStrategy.UPSERT),
    }

# The settings only some kinds of target take.
_TARGET_SETTINGS = {
    'targetColumnTypes': ('files', 'iceberg'),
    'singleFile': ('files',),
    'targetKey': ('iceberg',),
    'targetTableStage': ('database',),
    'preTargetAdhocQueries': ('database',),
    'postTargetAdhocQueries': ('database',),
    # A files or Iceberg target publishes what one writer wrote, and has no
    # table for several to load into at once.
    'partitions': ('database',),
    }

_TARGET_NAMES = {'database': 'a database', 'files': 'a files connection', 'iceberg': 'an Iceberg connection'}


def targetKind(connection: Any) -> str:

    if isinstance(connection, FilesConnection):
        return 'files'
    if isinstance(connection, IcebergConnection):
        return 'iceberg'

    return 'database'


def isLake(connection: Any) -> bool:
    """Whether `connection` is written as tables of files -- a files or an
    Iceberg connection -- rather than a database.
    """

    return isinstance(connection, (FilesConnection, IcebergConnection))


def targetProblems(job: DataJobConfig, connection: Any) -> List[str]:
    """Why `job` can't write to `connection`: a strategy or a setting the
    connection's kind of target doesn't take, or a table it can't name.
    """

    kind = targetKind(connection)
    problems = []

    if job.insertStrategy not in _STRATEGIES[kind]:
        what = 'a {} database'.format(connection.type.value) if kind == 'database' else _TARGET_NAMES[kind]
        problems.append('is {}, which takes insertStrategy {}, not {}'.format(
            what, _listed([strategy.value for strategy in _STRATEGIES[kind]]).replace(' and ', ' or '), job.insertStrategy.value))

    for setting, kinds in _TARGET_SETTINGS.items():
        if setting == 'partitions' and job.partitions is not None and job.partitions.column is None:
            # `partitions: auto` reads a lake target's job as one stream.
            continue
        if getattr(job, setting) and kind not in kinds:
            problems.append('{} is for {}, and it is {}'.format(
                setting, ' or '.join(_TARGET_NAMES[other] for other in kinds), _TARGET_NAMES[kind]))

    if kind == 'files':
        problem = filePathProblem(job.targetTableFinal)
        if problem:
            problems.append('targetTableFinal {!r} {}'.format(job.targetTableFinal, problem))

    if kind == 'iceberg':
        try:
            namespace, name = connection.tableIdentifier(job.targetTableFinal)
        except ConfigurationError as error:
            problems.append(str(error))
        else:
            bad = [part for part in namespace.split('.') + [name] if not _ICEBERG_NAME.match(part)]
            if bad:
                problems.append('targetTableFinal {!r} is not namespace.table, each a plain identifier'.format(job.targetTableFinal))
        badKeys = [column for column in job.targetKey if not _ICEBERG_NAME.match(column)]
        if badKeys:
            problems.append('targetKey names {}, which is not a plain identifier'.format(', '.join(badKeys)))

    return problems


def partitionLimitProblems(job: DataJobConfig, connections: Mapping[str, ConnectionConfig]) -> List[str]:
    """Why `job`'s partitions can't all run against its connections: each
    partition opens a connection of its own to the source and the target, so
    a partitioned job takes one of a connection's maxConcurrentJobs places for
    each, and a job needing more places than there are could never start.
    """

    if job.partitions is None or job.partitions.automatic:
        # An automatic count is fitted to what the connections have free.
        return []

    problems = []
    for setting in ('sourceConnection', 'targetConnection'):
        alias = getattr(job, setting)
        connection = connections.get(alias)
        limit = None if connection is None else connection.jobLimit()
        if limit is not None and int(job.partitions.count) > limit:
            why = ('DuckDB lets one process at a time open a file' if isinstance(connection, DuckDBConnection)
                   else 'its maxConcurrentJobs is {}'.format(limit))
            problems.append('partitions.count is {}, and {} "{}" takes at most {} at once ({}); each partition holds one of its places, '
                            'as a job does'.format(job.partitions.count, setting, alias, limit, why))

    return problems


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
    'chunkSize', 'retries', 'retryDelaySeconds', 'timeoutSeconds', 'partitions',
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
    # a number up to the cores available, or `auto`, which shares half the cores
    # as each job starts with the jobs running alongside it. See
    # masking.maskingThreadsFor and runner._runCycle.
    maskingThreads: Union[Literal['auto'], Annotated[int, Field(ge=1)]] = 1
    # Whether a run with masked jobs stops before it starts, rather than masking
    # in Python: true when the native masker isn't in use for any reason, false
    # never, and by default (None) only when it is installed but another
    # version. See masking.requireNativeProblem.
    requireNative: Optional[bool] = None
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
        if 'include' in settings:
            raise ValueError('include names other files, which a mapping cannot hold: read the jobs file with readJobsFile(path), '
                             'which merges them, and validate what it returns')
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
            # Not chained: pydantic's own error quotes the input it was given,
            # a masking key or a password among it, in any traceback logged.
            raise Configuration._invalid(error, sourceDescription) from None


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
            raise Configuration._invalid(error, sourceDescription, skipLocation=1) from None


    @staticmethod
    def validateConnectionConfiguration(rawConfiguration: Dict[str, Any], directory: Optional[str] = None) -> Dict[str, ConnectionConfig]:
        """Two DuckDB aliases for one file are refused: each would count its
        jobs separately, and two jobs would open the file at once.

        With `directory`, the one connections.yaml was read from, a relative
        file path is taken relative to it rather than to the working
        directory; see anchorPaths.
        """

        connections = {
            alias: Configuration.validateConnection(connectionSettings, f'connections.yaml -> {alias}')
            for alias, connectionSettings in (rawConfiguration or {}).items() if not isAnchorKey(alias)
            }
        if directory is not None:
            connections = {alias: anchorPaths(settings, directory) for alias, settings in connections.items()}

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
            found: List[str] = []
            if isinstance(job, DataJobConfig) and connections is not None:
                found += _requiredMaskingProblems(job, connections) + _incrementalShuffleProblems(job, connections)
            found += ['predecessor "{}" is not a known job'.format(predecessor) for predecessor in job.predecessors if predecessor not in jobs]
            if isinstance(job, DataJobConfig) and connections is not None:
                found += _connectionProblems(job, connections)
            if isinstance(job, DataJobConfig) and connectionAliases is not None:
                found += ['{} "{}" is not a known connection alias'.format(setting, getattr(job, setting))
                          for setting in ('sourceConnection', 'targetConnection') if getattr(job, setting) not in connectionAliases]
            problems.extend('{}: {}'.format(jobName, problem) for problem in found)

        cycle = findCycle({jobName: job.predecessors for jobName, job in jobs.items()})
        if cycle:
            problems.append('predecessors form a cycle, so none of these jobs could ever start: {}'.format(' -> '.join(cycle)))

        if problems:
            raise ConfigurationError('Invalid job graph:\n' + '\n'.join(problems))

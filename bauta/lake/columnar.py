"""What a table of files and an Iceberg table share: each column's type,
declared or settled by the first values it holds, and a job's rows held in
Arrow until they reach rowGroupSize, then handed on at once.
"""
from __future__ import annotations

import datetime
import logging
import secrets
from operator import itemgetter
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..configuration import ColumnType, ConfigurationError, DataJobConfig, parseColumnType
from ..jobs.targets import LoadTarget
from ..log import LOGGER_NAME
from .columns import PLAIN_KINDS, arrowType, inferType, normalized, reportedDecimal, toArrow

logger = logging.getLogger(LOGGER_NAME)


def newRunId() -> str:
    """Sorts by when the run began, to the second, and differs between two
    runs in the same second.
    """

    return '{}-{}'.format(datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'), secrets.token_hex(3))


class Column:
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
        # Holds JSON text alone, whatever its value: a PostgreSQL json or
        # jsonb column, which JSON Lines writes as it is.
        self.jsonText = False
        # Can't be null: an Iceberg table's key, or a column it requires.
        self.required = False


class ColumnarTarget(LoadTarget):
    """What a table of files and an Iceberg table share: each column's type,
    declared or settled by its first values, and a job's rows held until they
    reach `rowGroupSize`, then handed to _writeTable() at once. A subclass
    says where that goes, and publishes it in finish().
    """

    # Where the columns are written, for messages.
    loadName: str

    def holdJson(self, indexes: Sequence[int]) -> None:
        """JSON columns arrive as their text, not as the dicts that mark a
        column as documents: the pipeline says which they are, by the same
        type codes it decodes and encodes them by, so JSON Lines writes each
        value as the JSON it is -- PostgreSQL's, MySQL's and DuckDB's alike.
        """

        for index in indexes:
            if index < len(self.columns):
                self.columns[index].documents = self.columns[index].jsonText = True


    def __init__(self, job: str, jobConfig: DataJobConfig, rowGroupSize: int) -> None:
        self.job = job
        self.jobConfig = jobConfig
        self.rowGroupSize = rowGroupSize
        self.columns: List[Column] = []
        self.schema: Any = None
        self._buffered: List[List[Any]] = []
        self._bufferedBytes = 0
        self._rowsSeen = 0


    def begin(self, sourceColumns: List[str], description: Optional[Sequence[Sequence[Any]]] = None) -> None:

        jobConfig = self.jobConfig
        names = jobConfig.targetColumns or sourceColumns

        if len(names) != len(sourceColumns):
            raise ConfigurationError('sourceQuery returns {} column(s) {} and targetColumns names {} ({}); the target takes the '
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
        self.columns = [Column(name, declared.get(name.upper()), reportedDecimal(descriptions[index]) if index < len(descriptions) else None)
                        for index, name in enumerate(names)]

        self._prepare()


    def _prepare(self) -> None:
        """Readies the target once its columns are known, before a row is
        written; raises ConfigurationError for what can't be written.
        """


    def _settled(self, columnType: ColumnType) -> ColumnType:
        """A column's type as this target can hold it."""

        return columnType


    def write(self, rows: List[Any]) -> None:

        import pyarrow

        if not rows:
            return

        arrays = []
        for index, column in enumerate(self.columns):
            raw = list(map(itemgetter(index), rows))
            kinds = set(map(type, raw))
            exactKinds = kinds if kinds <= PLAIN_KINDS else None
            if exactKinds is not None:
                # Nothing a file holds differently, and no documents.
                values = raw
            else:
                if not column.documents and any(isinstance(value, (dict, list)) for value in raw):
                    column.documents = True
                values = [normalized(value) for value in raw]
            if column.type is None:
                inferred = inferType(column.name, values, column.reported)
                column.type = None if inferred is None else self._settled(inferred)
                # Settled as text by its values: SQLite's columns hold numbers
                # among text, so a later number is its text too.
                column.lenient = column.type is not None and column.type.kind == 'string'
            if column.type is None:
                arrays.append(pyarrow.nulls(len(values)))
            else:
                arrays.append(toArrow(column.name, values, column.type, column.lenient, exactKinds))

        self._buffered.append(arrays)
        self._bufferedBytes += sum(array.nbytes for array in arrays)
        self._rowsSeen += len(rows)

        if self._bufferedBytes >= self.rowGroupSize:
            self._flush()


    def _field(self, column: Column) -> Any:

        import pyarrow

        assert column.type is not None

        return pyarrow.field(column.name, arrowType(column.type))


    def _settleSchema(self) -> Any:
        """The table's schema, fixed from now on, settling as text any column
        nothing has had a value in yet.
        """

        import pyarrow

        if self.schema is not None:
            return self.schema

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

        self.schema = pyarrow.schema([self._field(column) for column in self.columns])

        return self.schema


    def _flush(self) -> None:

        import pyarrow

        if not self._buffered:
            return

        schema = self._settleSchema()
        columns = []
        for index, field in enumerate(schema):
            # A column settled after the first of these chunks holds nulls of
            # no type in the earlier ones.
            columns.append(pyarrow.chunked_array([arrays[index] if arrays[index].type == field.type else arrays[index].cast(field.type)
                                                  for arrays in self._buffered], type=field.type))
        table = pyarrow.Table.from_arrays(columns, schema=schema)

        self._buffered = []
        self._bufferedBytes = 0
        self._writeTable(table)


    def _writeTable(self, table: Any) -> None:

        raise NotImplementedError

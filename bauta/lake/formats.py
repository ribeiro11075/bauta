"""How a run's parts are written: Parts rolls from one to the next at
fileSize, and a writer per format -- Parquet, CSV and JSON Lines -- takes
Arrow tables of the part's one schema, a row group at a time, and writes them
to a stream it closes.

CSV and JSON Lines hold text, so each type is spelled one way in both:

| Type                    | Spelled as                                        |
| ----------------------- | ------------------------------------------------- |
| null                    | an empty unquoted field; JSON's null              |
| empty text              | "" -- quoted, so it isn't read as null            |
| decimal                 | its exact digits; a JSON string, not a number     |
| timestamp               | 2026-01-02 03:04:05.000006                        |
| timestamptz             | 2026-01-02 03:04:05.000006Z, in UTC               |
| binary                  | base64                                            |
| a JSON document or list | CSV: its text; JSON Lines: nested, as itself      |

A timestamp has a space rather than ISO 8601's `T`: it is what Hive, Athena
and Spark read as a timestamp in text, and Snowflake and BigQuery read both.
"""
from __future__ import annotations

import base64
import json
import math
import posixpath
from typing import AbstractSet, Any, Callable, List, NamedTuple, Optional, Protocol, Tuple

from ..configuration import ConfigurationError, FileCompression, FileFormat

_TIMESTAMP = '%Y-%m-%d %H:%M:%S.%f'

# JSON has no NaN or infinity; BigQuery and Snowflake read these strings as them.
_NOT_FINITE = {'nan': '"NaN"', 'inf': '"Infinity"', '-inf': '"-Infinity"'}


class PartSettings(NamedTuple):
    """How a target writes its parts: a files connection as it says, an
    Iceberg table as Parquet.
    """

    format: FileFormat
    compression: FileCompression
    # Where a part is closed and the next begun.
    fileSize: int
    # CSV's; a comma when None.
    delimiter: Optional[str] = None


def extension(settings: PartSettings) -> str:
    """What a part's name ends in. Engines reading text tell gzip by `.gz`."""

    compressed = '.gz' if settings.format != FileFormat.PARQUET and settings.compression == FileCompression.GZIP else ''

    return {FileFormat.PARQUET: '.parquet', FileFormat.CSV: '.csv', FileFormat.NDJSON: '.ndjson'}[settings.format] + compressed


class PartWriter:
    """One part, being written. write() takes a table of `schema`; close()
    finishes the file and closes `stream`.
    """

    def __init__(self, stream: Any, schema: Any, settings: PartSettings, jsonColumns: AbstractSet[str]) -> None:
        self.stream = stream
        self.schema = schema
        self.settings = settings
        self.jsonColumns = jsonColumns

    def write(self, table: Any) -> None:

        raise NotImplementedError

    def close(self) -> None:

        raise NotImplementedError


def partWriter(stream: Any, schema: Any, settings: PartSettings, jsonColumns: AbstractSet[str]) -> PartWriter:

    writer = {FileFormat.PARQUET: _Parquet, FileFormat.CSV: _Csv, FileFormat.NDJSON: _JsonLines}[settings.format]

    return writer(stream, schema, settings, jsonColumns)


class _Parquet(PartWriter):
    """Each table written is one row group."""

    def __init__(self, stream: Any, schema: Any, settings: PartSettings, jsonColumns: AbstractSet[str]) -> None:
        super().__init__(stream, schema, settings, jsonColumns)
        import pyarrow.parquet

        compression = settings.compression
        self._writer = pyarrow.parquet.ParquetWriter(stream, schema, compression=None if compression == FileCompression.NONE else compression.value)

    def write(self, table: Any) -> None:

        self._writer.write_table(table, row_group_size=max(1, table.num_rows))

    def close(self) -> None:

        self._writer.close()
        self.stream.close()


class _Text(PartWriter):
    """The text formats: written through gzip where the connection says,
    measured as the part's size by the stream beneath it.
    """

    def __init__(self, stream: Any, schema: Any, settings: PartSettings, jsonColumns: AbstractSet[str]) -> None:
        super().__init__(stream, schema, settings, jsonColumns)
        import pyarrow

        self.output = pyarrow.CompressedOutputStream(stream, 'gzip') if settings.compression == FileCompression.GZIP else stream

    def close(self) -> None:

        # Closing the gzip stream writes its trailer and closes the one beneath.
        self.output.close()
        if not self.stream.closed:
            self.stream.close()


def _base64(values: List[Optional[bytes]]) -> List[Optional[str]]:

    return [None if value is None else base64.b64encode(value).decode('ascii') for value in values]


class _Csv(_Text):
    """With a header in every part, so each file reads on its own."""

    def __init__(self, stream: Any, schema: Any, settings: PartSettings, jsonColumns: AbstractSet[str]) -> None:
        super().__init__(stream, schema, settings, jsonColumns)
        import pyarrow
        import pyarrow.csv

        self._binary = [index for index, field in enumerate(schema) if pyarrow.types.is_binary(field.type)]
        textSchema = schema
        for index in self._binary:
            textSchema = textSchema.set(index, pyarrow.field(schema[index].name, pyarrow.string()))
        self._textSchema = textSchema
        options = pyarrow.csv.WriteOptions(delimiter=settings.delimiter or ',', quoting_style='needed')
        self._writer = pyarrow.csv.CSVWriter(self.output, textSchema, write_options=options)

    def write(self, table: Any) -> None:

        import pyarrow

        for index in self._binary:
            table = table.set_column(index, self._textSchema[index], pyarrow.array(_base64(table.column(index).to_pylist()), pyarrow.string()))

        self._writer.write_table(table)

    def close(self) -> None:

        self._writer.close()
        super().close()


def _jsonText(value: Any) -> str:

    return json.dumps(value, ensure_ascii=False)


def _document(value: str) -> str:
    """A JSON document or list, as itself; any other text as a JSON string.
    What the source held as JSON arrives here as its text.
    """

    if value[:1] in ('{', '['):
        try:
            json.loads(value)
            return value
        except ValueError:
            pass

    return _jsonText(value)


def _float(value: float) -> str:

    return json.dumps(value) if math.isfinite(value) else _NOT_FINITE[str(value)]


def _encoder(field: Any, isDocument: bool) -> Callable[[Any], str]:
    """How one column's non-null values are written in a JSON line."""

    import pyarrow

    kind = field.type
    if pyarrow.types.is_boolean(kind):
        return lambda value: 'true' if value else 'false'
    if pyarrow.types.is_integer(kind):
        return str
    if pyarrow.types.is_floating(kind):
        return _float
    if pyarrow.types.is_decimal(kind):
        return lambda value: '"{}"'.format(value)
    if pyarrow.types.is_timestamp(kind):
        zone = 'Z' if kind.tz is not None else ''
        return lambda value: '"{}{}"'.format(value.strftime(_TIMESTAMP), zone)
    if pyarrow.types.is_date(kind):
        return lambda value: '"{}"'.format(value.isoformat())
    if pyarrow.types.is_time(kind):
        return lambda value: '"{}"'.format(value.strftime('%H:%M:%S.%f'))
    if pyarrow.types.is_binary(kind):
        return lambda value: '"{}"'.format(base64.b64encode(value).decode('ascii'))

    return _document if isDocument else _jsonText


class _JsonLines(_Text):
    """One object per line, keyed by column, every column present."""

    # Rows turned into Python at once: a row group whole would be several
    # times its size in memory.
    BATCH_ROWS = 4096

    def __init__(self, stream: Any, schema: Any, settings: PartSettings, jsonColumns: AbstractSet[str]) -> None:
        super().__init__(stream, schema, settings, jsonColumns)

        self._keys = [_jsonText(field.name) + ':' for field in schema]
        self._encoders = [_encoder(field, field.name in jsonColumns) for field in schema]

    def write(self, table: Any) -> None:

        columns = list(zip(self._keys, self._encoders))
        for batch in table.to_batches(max_chunksize=self.BATCH_ROWS):
            lines = []
            for row in zip(*(column.to_pylist() for column in batch.columns)):
                fields = [key + ('null' if value is None else encode(value)) for (key, encode), value in zip(columns, row)]
                lines.append('{' + ','.join(fields) + '}\n')
            self.output.write(''.join(lines).encode('utf-8'))


class PartOutput(Protocol):
    """Where Parts writes: a files connection's Store, or an Iceberg table's
    FileIO.
    """

    def openOutput(self, path: str) -> Any: ...

    def largestMove(self) -> Optional[int]:
        """The largest file publishing can move there, or None for any size."""


class Parts:
    """The parts of one run, written into `directory`, each closed at the
    settings' fileSize and the next begun -- unless the table is one file.
    """

    def __init__(self, output: PartOutput, directory: str, runId: str, schema: Any, settings: PartSettings, jsonColumns: AbstractSet[str] = frozenset(),
                 singleFile: bool = False) -> None:
        self.output = output
        self.directory = directory
        self.runId = runId
        self.schema = schema
        self.settings = settings
        self.jsonColumns = jsonColumns
        self.singleFile = singleFile
        self.written: List[Tuple[str, int]] = []
        self._path: Optional[str] = None
        self._stream: Any = None
        self._writer: Optional[PartWriter] = None
        self._rows = 0


    def _open(self) -> None:

        name = 'part-{}-{:05d}{}'.format(self.runId, len(self.written) + 1, extension(self.settings))
        self._path = posixpath.join(self.directory, name)
        self._stream = self.output.openOutput(self._path)
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

        largest = self.output.largestMove()
        if self.singleFile and largest is not None and written > largest:
            raise ConfigurationError('the table is past {} MiB, the largest file this store can publish, by copying it in one request; '
                                     'write it in parts, without singleFile'.format(largest // 2 ** 20))
        if not self.singleFile and written >= self.settings.fileSize:
            self._close()


    def _close(self) -> None:

        if self._writer is None:
            return
        assert self._path is not None
        self._writer.close()
        self.written.append((self._path, self._rows))
        self._writer = self._stream = None
        self._path = None


    def close(self) -> List[Tuple[str, int]]:
        """Every part written, and its rows. An empty table still gets one part,
        so its columns are written down.
        """

        if self._writer is None and not self.written:
            self.write(self.schema.empty_table())
        self._close()

        return self.written


    def discard(self) -> List[str]:
        """Closes what is open, best-effort, and returns every part's path, the
        one being written included. A cloud completes an upload that is
        closed, so what was written still has to be deleted.
        """

        paths = [path for path, _ in self.written] + ([self._path] if self._path else [])
        for closing in (self._writer, self._stream):
            if closing is not None:
                try:
                    closing.close()
                except Exception:
                    pass
        self._writer = self._stream = None

        return paths

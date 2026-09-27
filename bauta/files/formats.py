"""How a part is spelled in each format a files connection writes: Parquet,
CSV and JSON Lines. Each writer takes Arrow tables of the part's one schema,
a row group at a time, and writes them to a stream it closes.

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
from typing import Any, Callable, List, Optional, Set

from ..configuration import FileCompression, FileFormat, FilesConnection

_TIMESTAMP = '%Y-%m-%d %H:%M:%S.%f'

# JSON has no NaN or infinity; BigQuery and Snowflake read these strings as them.
_NOT_FINITE = {'nan': '"NaN"', 'inf': '"Infinity"', '-inf': '"-Infinity"'}


def extension(settings: FilesConnection) -> str:
    """What a part's name ends in. Engines reading text tell gzip by `.gz`."""

    compressed = '.gz' if settings.format != FileFormat.PARQUET and settings.effectiveCompression() == FileCompression.GZIP else ''

    return {FileFormat.PARQUET: '.parquet', FileFormat.CSV: '.csv', FileFormat.NDJSON: '.ndjson'}[settings.format] + compressed


class PartWriter:
    """One part, being written. write() takes a table of `schema`; close()
    finishes the file and closes `stream`.
    """

    def __init__(self, stream: Any, schema: Any, settings: FilesConnection, jsonColumns: Set[str]) -> None:
        self.stream = stream
        self.schema = schema
        self.settings = settings
        self.jsonColumns = jsonColumns

    def write(self, table: Any) -> None:

        raise NotImplementedError

    def close(self) -> None:

        raise NotImplementedError


def partWriter(stream: Any, schema: Any, settings: FilesConnection, jsonColumns: Set[str]) -> PartWriter:

    writer = {FileFormat.PARQUET: _Parquet, FileFormat.CSV: _Csv, FileFormat.NDJSON: _JsonLines}[settings.format]

    return writer(stream, schema, settings, jsonColumns)


class _Parquet(PartWriter):
    """Each table written is one row group."""

    def __init__(self, stream: Any, schema: Any, settings: FilesConnection, jsonColumns: Set[str]) -> None:
        super().__init__(stream, schema, settings, jsonColumns)
        import pyarrow.parquet

        compression = settings.effectiveCompression()
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

    def __init__(self, stream: Any, schema: Any, settings: FilesConnection, jsonColumns: Set[str]) -> None:
        super().__init__(stream, schema, settings, jsonColumns)
        import pyarrow

        self.output = pyarrow.CompressedOutputStream(stream, 'gzip') if settings.effectiveCompression() == FileCompression.GZIP else stream

    def close(self) -> None:

        # Closing the gzip stream writes its trailer and closes the one beneath.
        self.output.close()
        if not self.stream.closed:
            self.stream.close()


def _base64(values: List[Optional[bytes]]) -> List[Optional[str]]:

    return [None if value is None else base64.b64encode(value).decode('ascii') for value in values]


class _Csv(_Text):
    """With a header in every part, so each file reads on its own."""

    def __init__(self, stream: Any, schema: Any, settings: FilesConnection, jsonColumns: Set[str]) -> None:
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

    def __init__(self, stream: Any, schema: Any, settings: FilesConnection, jsonColumns: Set[str]) -> None:
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

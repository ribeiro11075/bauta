"""A table of files -- Parquet, CSV or JSON Lines -- as a data job's source,
on this machine or in a cloud. See "Files as a source" in
docs/concepts/how-it-works.md.

A sourceQuery is SQL, and files can't answer it, so an in-memory DuckDB does:
each table the query names is opened through pyarrow, with the connection's
own credentials, and given to DuckDB as a dataset, which it scans as the rows
are asked for, pushing filters down. Nothing is copied first. Everything past
the query is what a database source goes through.
"""
from __future__ import annotations

import posixpath
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..configuration import ConfigurationError, DuckDBConnection, FileFormat, FilesConnection, filePathProblem
from ..database import Database
from ..database.connection import RowStream
from ..database.dialects import DuckDBDialect
from ..database.driver import Connection
from ..review.queryTables import tablesRead
from .formats import PartSettings, extension
from .stores import Store, requirePyarrow
from .target import SNAPSHOT_PREFIX, SUCCESS_FILE

# Run on the DuckDB answering a query, before it reads anything: with it, it
# can reach only the tables given to it, and DuckDB won't turn it back on. A
# sourceQuery could otherwise read any file bauta's account can with
# read_csv('/etc/passwd'), or write one.
LOCKDOWN_STATEMENT = 'SET enable_external_access = false'


def requireDuckDB() -> None:

    try:
        import duckdb  # noqa: F401 -- imported to be there
    except ImportError as error:
        raise ConfigurationError('a files connection is read with DuckDB, which is not installed: pip install "bauta[files]"') from error


class _TablesDialect(DuckDBDialect):
    """DuckDB, with a source's tables given to each streaming cursor: such a
    cursor is a second connection, which what is registered on the first
    can't be seen from.
    """

    def __init__(self, datasets: Dict[str, Any]) -> None:
        super().__init__()
        self.datasets = datasets


    def streamingCursor(self, connection: Connection, chunkSize: int) -> Any:

        cursor = super().streamingCursor(connection, chunkSize)
        for table, dataset in self.datasets.items():
            cursor.register(table, dataset)

        return cursor


class FileSource(Database):
    """A files connection, opened for reading: a Database as far as a job can
    tell, over an in-memory DuckDB that holds only the tables its queries name.
    """

    def __init__(self, settings: FilesConnection) -> None:
        requirePyarrow()
        requireDuckDB()
        self.files = settings
        self.store = Store(settings)
        self._datasets: Dict[str, Any] = {}
        super().__init__(DuckDBConnection(path=':memory:'))
        self.dialect = _TablesDialect(self._datasets)
        self.cursor.execute(LOCKDOWN_STATEMENT)
        self.connection.commit()


    def stream(self, query: str, chunkSize: int, parameters: Optional[Sequence[Any]] = None) -> Tuple[List[str], RowStream]:

        self._openTables(query)

        return super().stream(query, chunkSize, parameters)


    def _openTables(self, query: str) -> None:
        """Gives DuckDB each table `query` reads that it doesn't yet have."""

        tables = tablesRead(query, foldCase=False)
        if tables is None:
            raise ConfigurationError('sourceQuery\'s parentheses don\'t balance, so the tables it reads can\'t be told')

        for table in sorted(tables - set(self._datasets)):
            self._datasets[table] = self._dataset(table)


    def _dataset(self, table: str) -> Any:

        import pyarrow.dataset

        problem = filePathProblem(table)
        if problem:
            raise ConfigurationError('sourceQuery reads {!r}, which {}'.format(table, problem))

        files, base = self._tableFiles(table)

        return pyarrow.dataset.dataset(files, filesystem=self.store.filesystem, format=self._format(), partitioning='hive', partition_base_dir=base)


    def _format(self) -> Any:

        import pyarrow.csv
        import pyarrow.dataset

        if self.files.format == FileFormat.PARQUET:
            return pyarrow.dataset.ParquetFileFormat()
        if self.files.format == FileFormat.CSV:
            return pyarrow.dataset.CsvFileFormat(parse_options=pyarrow.csv.ParseOptions(delimiter=self.files.delimiter or ','))

        return pyarrow.dataset.JsonFileFormat()


    def _tableFiles(self, table: str) -> Tuple[List[str], str]:
        """The files that are `table` now, and the directory any partition
        directories in their paths are read from: a directory of them, its
        newest complete snapshot if it has snapshots, or one file, by its name
        or the name a singleFile job gives it.
        """

        directory = self.store.path(table)
        if self._isFile(directory):
            return [directory], posixpath.dirname(directory)

        files = self._visibleFiles(directory)
        if files:
            current = self._newestSnapshot(table, directory, files)
            return current, self._baseOf(directory, current)

        settings = PartSettings(self.files.format, self.files.effectiveCompression(), self.files.fileSize, self.files.delimiter)
        singleFile = directory + extension(settings)
        if self._isFile(singleFile):
            return [singleFile], posixpath.dirname(singleFile)

        raise ConfigurationError('sourceQuery reads {!r}, and {} holds no such table: no files in that directory, and no file of that name'.format(
            table, self.files.location()))


    def _isFile(self, path: str) -> bool:

        import pyarrow.fs

        return bool(self.store.filesystem.get_file_info(path).type == pyarrow.fs.FileType.File)


    def _visibleFiles(self, directory: str) -> List[str]:
        """The files under `directory` a reader sees, in order: none of it
        through a name beginning with `_` or `.`, as bauta's staging and
        _SUCCESS do and as Spark's and Hive's own files do.
        """

        import pyarrow.fs

        selector = pyarrow.fs.FileSelector(directory, recursive=True, allow_not_found=True)
        prefix = directory.rstrip('/') + '/'
        found = []
        for info in self.store.filesystem.get_file_info(selector):
            relative = info.path[len(prefix):] if info.path.startswith(prefix) else info.path
            if info.type == pyarrow.fs.FileType.File and not any(part.startswith(('_', '.')) for part in relative.split('/')):
                found.append(info.path)

        return sorted(found)


    def _newestSnapshot(self, table: str, directory: str, files: List[str]) -> List[str]:
        """`files`, or where the table is an overwrite job's, those of its
        newest complete snapshot alone: every snapshot it keeps holds all of
        the table, and one without _SUCCESS is still being published.
        """

        snapshots = sorted({file[len(directory) + 1:].split('/', 1)[0] for file in files if file[len(directory) + 1:].startswith(SNAPSHOT_PREFIX)})
        if not snapshots:
            return files

        complete = [name for name in snapshots if self.store.exists(posixpath.join(directory, name, SUCCESS_FILE))]
        if not complete:
            raise ConfigurationError('{} has snapshots, and none is complete yet, so there is nothing to read'.format(table))
        newest = posixpath.join(directory, complete[-1]) + '/'

        return [file for file in files if file.startswith(newest)]


    def _baseOf(self, directory: str, files: List[str]) -> str:
        """Where partition directories start: `snapshot=<run>` is bauta's,
        and would otherwise read as a column.
        """

        relative = files[0][len(directory) + 1:]
        if relative.startswith(SNAPSHOT_PREFIX):
            return posixpath.join(directory, relative.split('/', 1)[0])

        return directory


def checkReadable(settings: FilesConnection) -> None:
    """Raises unless a job could read under `settings`' root: it is listed,
    which is what reading a table starts with. Asks for nothing to be
    writable, since a source's credentials often can't. In a cloud it is the
    bucket that is listed: a prefix with nothing under it isn't there to
    find, and asking leniently would pass a bucket that doesn't exist.
    """

    import pyarrow.fs

    requireDuckDB()
    store = Store(settings)
    listed = store.root.split('/', 1)[0] if store.objectStore else store.root
    store.filesystem.get_file_info(pyarrow.fs.FileSelector(listed, recursive=False))

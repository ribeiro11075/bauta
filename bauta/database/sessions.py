"""One open connection per alias, and what a check reads through one: the
columns a job's query returns, a sample of its rows, a target's columns.

For the checks that ask many things of the same few databases -- a dry run,
`audit --connect`, a console's connection test -- rather than one connection
per question, each running passwordCommand again.
"""
from __future__ import annotations

import contextlib
from typing import Any, Dict, Generator, List, Mapping, Tuple

from ..configuration import ConnectionConfig, DataJobConfig
from .connection import Database
from .values import decodedJson


class Connections:
    """One open connection per alias, opened on first use and closed together.

    A connection whose statement failed is closed and forgotten, since
    PostgreSQL refuses anything more on it until rolled back; the next use
    opens a fresh one.
    """

    def __init__(self, connectionConfiguration: Mapping[str, ConnectionConfig]) -> None:
        self._settings = connectionConfiguration
        self._open: Dict[str, Database] = {}


    @contextlib.contextmanager
    def use(self, alias: str) -> Generator[Database, None, None]:

        database = self._open.get(alias)
        if database is None:
            database = self._open[alias] = Database(connectionSettings=self._settings[alias])

        try:
            yield database
        except BaseException:
            self._open.pop(alias, None)
            with contextlib.suppress(Exception):
                database.close()
            raise


    def __enter__(self) -> 'Connections':

        return self


    def __exit__(self, *exception: Any) -> None:

        for database in self._open.values():
            with contextlib.suppress(Exception):
                database.close()
        self._open.clear()


def sourceQueryColumns(job: DataJobConfig, connections: Connections) -> List[str]:
    """The columns a job's sourceQuery returns, by reading and discarding one
    row, since rewriting arbitrary SQL isn't portable.
    """

    with connections.use(job.sourceConnection) as database:
        query = job.sourceQuery
        parameters = None
        if job.watermarkColumn:
            query, parameters = database.bindWatermark(query, job.watermarkInitial)
        columns, chunks = database.stream(query=query, chunkSize=1, parameters=parameters)
        chunks.close()

    return columns


def sourceQuerySample(job: DataJobConfig, connections: Connections, rows: int) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    """The columns a job's sourceQuery returns, and up to `rows` of its rows
    from the start, without reading the rest. They stay in memory: nothing
    that reads them may report a value.
    """

    with connections.use(job.sourceConnection) as database:
        query = job.sourceQuery
        parameters = None
        if job.watermarkColumn:
            query, parameters = database.bindWatermark(query, job.watermarkInitial)
        columns, chunks = database.stream(query=query, chunkSize=max(1, rows), parameters=parameters)
        with chunks:
            sampled = next(chunks, []) if rows else []

    # JSON parsed, since it is classified rather than loaded; see decodedJson.
    return columns, decodedJson(sampled, getattr(chunks, 'description', None), database.type)


def targetColumns(job: DataJobConfig, connections: Connections) -> List[str]:
    """The target's columns, in the order a load fills them."""

    with connections.use(job.targetConnection) as database:
        return database.getAllColumnNames(table=job.targetTableFinal)

"""What bauta needs of a database driver's connection and cursor.

Each dialect's driver is a Python DB-API one, and bauta relies on more of its
behaviour than the method names say:

- A statement opens a transaction, which lasts until commit() or rollback().
  A load commits a chunk at a time, and `clear` empties every table or none.
- The cursor a dialect's prepareSession returns shares the connection's
  transaction, so the connection's commit() commits what the cursor did.
- rowcount is the number of rows a DML statement changed.
- rollback() with no transaction open does nothing. It is called while
  handling another error, which it must not replace.

Six of the seven drivers behave this way as they stand. DuckDB's does not --
each statement commits on its own, and its cursor() is a second connection --
so DuckDBDialect wraps it in a session that does; see dialects/duckdb.py.

A dialect that needs more of its driver than this -- psycopg's COPY, DuckDB's
registered views -- says so where it uses it.
"""
from __future__ import annotations

from typing import Any, List, Optional, Protocol, Sequence


class Cursor(Protocol):

    @property
    def description(self) -> Any: ...

    @property
    def rowcount(self) -> int: ...

    def execute(self, query: str, parameters: Optional[Sequence[Any]] = ...) -> Any: ...

    def executemany(self, query: str, parameters: Sequence[Sequence[Any]]) -> Any: ...

    def fetchone(self) -> Any: ...

    def fetchmany(self, size: int = ...) -> List[Any]: ...

    def fetchall(self) -> List[Any]: ...

    def close(self) -> None: ...


class Connection(Protocol):

    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


def native(driverObject: Any) -> Any:
    """A connection or cursor as its driver's own type, for a feature this
    interface doesn't declare -- psycopg's COPY, oracledb's type handlers. A
    call through it is a place the dialect depends on its driver alone.
    """

    return driverObject

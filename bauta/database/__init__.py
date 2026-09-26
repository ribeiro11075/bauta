"""Connections and the rows through them: `connection` streams and loads rows the
same way on every database, `driver` states what it needs of each database's
driver, `values` what each Python type is sent to each database as, and
`dialects` holds everything else that differs between the seven.
"""
from .connection import DIALECTS, Database

__all__ = [
    'Database',
    'DIALECTS',
    ]

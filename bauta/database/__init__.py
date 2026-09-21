"""Connections and the rows through them: `connection` streams and loads rows
the same way on every database, and `dialects` holds what differs between
the six.
"""
from .connection import DIALECTS, Database

__all__ = [
    'Database',
    'DIALECTS',
    ]

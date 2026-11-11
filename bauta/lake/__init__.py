"""Tables of files, and Iceberg tables, as a data job's target: written
through pyarrow, which the `files` extra installs, and pyiceberg, which the
`iceberg` extra does. Tables of files are a job's source too, in .source. See
"Files as a target" and "Files as a source" in docs/concepts/how-it-works.md.
"""
from typing import Any

from ..configuration import IcebergConnection
from .columns import FileTypeError
from .target import FileTarget, checkWritable as checkFilesWritable


def checkWritable(settings: Any) -> None:
    """Raises unless a job could write to `settings`: a files connection's
    root, or an Iceberg connection's catalog and warehouse.
    """

    if isinstance(settings, IcebergConnection):
        from .iceberg import checkIcebergWritable

        checkIcebergWritable(settings)
        return

    checkFilesWritable(settings)


def checkReadable(settings: Any) -> None:
    """Raises unless a job could read from `settings`, a files connection."""

    from .source import checkReadable as checkFilesReadable

    checkFilesReadable(settings)


__all__ = [
    'checkReadable',
    'checkWritable',
    'FileTarget',
    'FileTypeError',
    ]

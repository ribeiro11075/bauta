"""Tables of files, and Iceberg tables, as a data job's target: written
through pyarrow, which the `files` extra installs, and pyiceberg, which the
`iceberg` extra does. See "Files as a target" in docs/concepts/how-it-works.md.
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


__all__ = [
    'checkWritable',
    'FileTarget',
    'FileTypeError',
    ]

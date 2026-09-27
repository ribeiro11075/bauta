"""Tables of files as a data job's target: Parquet, written through pyarrow,
which the `files` extra installs. See "Files as a target" in docs/design.md.
"""
from .columns import FileTypeError
from .target import FileTarget, checkWritable

__all__ = [
    'checkWritable',
    'FileTarget',
    'FileTypeError',
    ]

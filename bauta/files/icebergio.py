"""The FileIO bauta's Iceberg tables are written through: pyiceberg's own,
over pyarrow's filesystems, with one location spelled as pyarrow reads it.

pyiceberg 0.12 hands pyarrow an Azure file as `container@account.dfs.core.
windows.net/path`, and pyarrow reads everything before the first slash as
the container's name, which Azure refuses as invalid. Here it is
`container/path`, as pyarrow names an Azure file everywhere else.

Imported by name, through the catalog's `py-io-impl` property, so only
where a job writes Iceberg.
"""
from __future__ import annotations

from typing import Tuple
from urllib.parse import urlparse

from pyiceberg.io.pyarrow import PyArrowFileIO
from pyiceberg.typedef import EMPTY_DICT, Properties

_AZURE_SCHEMES = frozenset({'abfs', 'abfss', 'wasb', 'wasbs'})


class FileIO(PyArrowFileIO):

    @staticmethod
    def parse_location(location: str, properties: Properties = EMPTY_DICT) -> Tuple[str, str, str]:

        scheme, netloc, path = PyArrowFileIO.parse_location(location, properties)

        if scheme in _AZURE_SCHEMES and '@' in netloc:
            container = netloc.split('@', 1)[0]
            path = container + urlparse(location).path

        return scheme, netloc, path

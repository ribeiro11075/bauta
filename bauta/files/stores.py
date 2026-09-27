"""Where a files connection's files go: a directory on this machine or a
prefix in an S3 bucket, behind one small interface over pyarrow's
filesystems, so publishing reads the same for both.

The two differ where it matters to a reader. A local move is a rename, which
a reader sees happen at once; on S3 it is a copy and a delete, and each
object appears whole when its copy completes. S3 has no directories, so
nothing here creates one there -- though pyarrow, deleting the last object
under a prefix, leaves an empty one standing for it. Only staging is ever
emptied, so the one it leaves is `<root>/_bauta_staging/`, hidden.
"""
from __future__ import annotations

import posixpath
from typing import Any, List, Optional

from ..configuration import ConfigurationError, FilesConnection
from ..configuration.models import S3_SCHEME


def requirePyarrow() -> Any:

    try:
        import pyarrow
        import pyarrow.fs  # noqa: F401 -- a submodule, imported to be there
    except ImportError as error:
        raise ConfigurationError('a files connection writes with pyarrow, which is not installed: pip install "bauta[files]"') from error

    return pyarrow


class Store:
    """A files connection's root, opened. Paths are the filesystem's own --
    `bucket/prefix/...` on S3 -- and location() spells one for a person.
    """

    def __init__(self, settings: FilesConnection) -> None:
        requirePyarrow()
        import pyarrow.fs

        self.settings = settings
        self.objectStore = settings.isObjectStore()

        if self.objectStore:
            self.filesystem = _s3(settings)
            self.root = settings.root[len(S3_SCHEME):].rstrip('/')
        else:
            self.filesystem = pyarrow.fs.LocalFileSystem()
            self.root = settings.location().replace('\\', '/')


    def path(self, *parts: str) -> str:

        return posixpath.join(self.root, *(part.replace('\\', '/') for part in parts))


    def location(self, path: str) -> str:

        return S3_SCHEME + path if self.objectStore else path


    def ensureDirectory(self, path: str) -> None:
        """Locally, creates `path` and its parents. S3 has no directories: an
        object's key is its whole path, and pyarrow would write an empty
        marker object for each, which some readers list as a file.
        """

        if not self.objectStore:
            self.filesystem.create_dir(path, recursive=True)


    def openOutput(self, path: str) -> Any:
        """A stream writing `path` as given. pyarrow would otherwise gzip a
        path ending in .gz itself, and the format already has.
        """

        return self.filesystem.open_output_stream(path, compression=None)


    def writeBytes(self, path: str, content: bytes) -> None:

        with self.filesystem.open_output_stream(path, compression=None) as stream:
            stream.write(content)


    def move(self, source: str, destination: str) -> None:

        self.filesystem.move(source, destination)


    def size(self, path: str) -> Optional[int]:

        return self.filesystem.get_file_info(path).size


    def exists(self, path: str) -> bool:

        import pyarrow.fs

        return bool(self.filesystem.get_file_info(path).type != pyarrow.fs.FileType.NotFound)


    def directories(self, path: str) -> List[str]:
        """The names of the directories -- the common prefixes, on S3 --
        directly under `path`, or none if it isn't there.
        """

        import pyarrow.fs

        selector = pyarrow.fs.FileSelector(path, allow_not_found=True)

        return sorted(info.base_name for info in self.filesystem.get_file_info(selector) if info.type == pyarrow.fs.FileType.Directory)


    def deleteDirectory(self, path: str) -> None:
        """Removes `path` and everything under it; nothing if it isn't there."""

        try:
            self.filesystem.delete_dir(path)
        except FileNotFoundError:
            pass


    def deleteFile(self, path: str) -> None:

        self.filesystem.delete_file(path)


def _s3(settings: FilesConnection) -> Any:
    """pyarrow's S3 filesystem for `settings`. Without keys, the AWS SDK's
    default chain finds credentials as the AWS CLI would.
    """

    import pyarrow.fs

    region = settings.region
    if region is None and settings.endpoint is None:
        # Asked of S3 itself: a request to another region than the bucket's
        # is refused with a redirect pyarrow doesn't follow.
        bucket = settings.root[len(S3_SCHEME):].split('/', 1)[0]
        try:
            region = pyarrow.fs.resolve_s3_region(bucket)
        except Exception:
            region = None

    arguments = {
        'region': region,
        'endpoint_override': settings.endpoint,
        'role_arn': settings.roleArn,
        'session_name': 'bauta' if settings.roleArn else None,
        }
    if settings.accessKeyId is not None and settings.secretAccessKey is not None:
        arguments['access_key'] = settings.accessKeyId
        arguments['secret_key'] = settings.secretAccessKey.get_secret_value()
        if settings.sessionToken is not None:
            arguments['session_token'] = settings.sessionToken.get_secret_value()

    return pyarrow.fs.S3FileSystem(**{name: value for name, value in arguments.items() if value is not None})

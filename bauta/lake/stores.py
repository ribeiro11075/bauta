"""Where a files connection's files go: a directory on this machine, or a
prefix in S3, Google Cloud Storage or Azure Blob Storage, behind one small
interface over pyarrow's filesystems, so publishing reads the same for all.

They differ where it matters to a reader. A local move is a rename, which a
reader sees happen at once, and so is one in an Azure account with a
hierarchical namespace. Elsewhere it is a copy within the bucket and a
delete, and each object appears whole when its copy completes: pyarrow
copies on S3 and GCS itself, and on Azure's flat namespace, which it can't
move in, this does. S3 copies at most 5 GiB in one request, which bounds a
part; GCS and Azure copy any size.

An object store has no directories, so nothing here creates one there --
though pyarrow, deleting the last object under a prefix, may leave an empty
one standing for it. Only staging is ever emptied, so the one it leaves is
`<root>/_bauta_staging/`, hidden.
"""
from __future__ import annotations

import posixpath
from typing import Any, Callable, Dict, List, Optional

from ..configuration import S3_LARGEST_COPY, ConfigurationError, FilesConnection, FileStore

def requirePyarrow() -> Any:

    try:
        import pyarrow
        import pyarrow.fs  # noqa: F401 -- a submodule, imported to be there
    except ImportError as error:
        raise ConfigurationError('a files connection writes with pyarrow, which is not installed: pip install "bauta[files]"') from error

    return pyarrow


class Store:
    """A files connection's root, opened. Paths are the filesystem's own --
    `bucket/prefix/...` in a cloud -- and location() spells one for a person.
    """

    def __init__(self, settings: FilesConnection) -> None:
        requirePyarrow()
        import pyarrow.fs

        self.settings = settings
        kind = settings.store()
        assert kind is not None
        self.kind = kind
        self.objectStore = self.kind != FileStore.LOCAL
        self._copiesToMove: Optional[bool] = None

        if self.objectStore:
            self.filesystem = _FILESYSTEMS[self.kind](settings)
            self.root = settings.bucketPath()
        else:
            self.filesystem = pyarrow.fs.LocalFileSystem()
            self.root = settings.location().replace('\\', '/')


    def path(self, *parts: str) -> str:

        return posixpath.join(self.root, *(part.replace('\\', '/') for part in parts))


    def location(self, path: str) -> str:
        """`path` as the root was written: under its URL, in a cloud."""

        return self.settings.location() + path[len(self.root):] if self.objectStore else path


    def largestMove(self) -> Optional[int]:
        """The largest file a move can publish, where the store has a limit:
        a move copying it in one request.
        """

        return S3_LARGEST_COPY if self.kind == FileStore.S3 else None


    def ensureDirectory(self, path: str) -> None:
        """Locally, creates `path` and its parents. An object store has no
        directories: an object's key is its whole path, and pyarrow would write
        an empty marker object for each, which some readers list as a file.
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
        """Moves a file, by copying and deleting where pyarrow can't move:
        Azure without a hierarchical namespace. With one, the destination's
        directory must be there first.
        """

        import pyarrow

        if not self._copiesToMove:
            try:
                self.filesystem.move(source, destination)
                self._copiesToMove = False
                return
            except pyarrow.ArrowNotImplementedError:
                self._copiesToMove = True
            except FileNotFoundError:
                if self.kind != FileStore.AZURE or not self.exists(source):
                    raise
                self.filesystem.create_dir(posixpath.dirname(destination), recursive=True)
                self.filesystem.move(source, destination)
                return

        self.filesystem.copy_file(source, destination)
        self.filesystem.delete_file(source)


    def exists(self, path: str) -> bool:

        import pyarrow.fs

        return bool(self.filesystem.get_file_info(path).type != pyarrow.fs.FileType.NotFound)


    def directories(self, path: str) -> List[str]:
        """The names of the directories directly under `path` that hold a file,
        or none if it isn't there.

        Read from the files under it, not asked for as directories: a cloud
        has only prefixes, and a listing of GCS one level deep, in pyarrow
        against fake-gcs-server, returned none of them.
        """

        import pyarrow.fs

        selector = pyarrow.fs.FileSelector(path, recursive=True, allow_not_found=True)
        prefix = path.rstrip('/') + '/'
        names = set()
        for info in self.filesystem.get_file_info(selector):
            relative = info.path[len(prefix):] if info.path.startswith(prefix) else ''
            if info.type == pyarrow.fs.FileType.File and '/' in relative:
                names.add(relative.split('/', 1)[0])

        return sorted(names)


    def deleteDirectory(self, path: str) -> None:
        """Removes `path` and everything under it; nothing if it isn't there.

        File by file, then the directory: on GCS, pyarrow's own delete_dir
        also deletes a marker object bauta never writes, and fails when it
        isn't there.
        """

        import pyarrow.fs

        if not self.objectStore:
            try:
                self.filesystem.delete_dir(path)
            except FileNotFoundError:
                pass
            return

        selector = pyarrow.fs.FileSelector(path, recursive=True, allow_not_found=True)
        for info in self.filesystem.get_file_info(selector):
            if info.type == pyarrow.fs.FileType.File:
                self.filesystem.delete_file(info.path)
        try:
            self.filesystem.delete_dir(path)
        except OSError:
            # Nothing is left to delete but what stood for the directory.
            pass


    def deleteFile(self, path: str) -> None:

        self.filesystem.delete_file(path)


    def readBytes(self, path: str) -> bytes:

        with self.filesystem.open_input_stream(path) as stream:
            return bytes(stream.read())


def _s3(settings: FilesConnection) -> Any:
    """Without keys, the AWS SDK's default chain finds credentials as the
    AWS CLI would.
    """

    import pyarrow.fs

    region = settings.region
    if region is None and settings.endpoint is None:
        # Asked of S3 itself: a request to another region than the bucket's
        # is refused with a redirect pyarrow doesn't follow.
        bucket = settings.bucketPath().split('/', 1)[0]
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
    if settings.accessKeyId is not None:
        arguments.update(access_key=settings.accessKeyId, secret_key=settings.plain('secretAccessKey'), session_token=settings.plain('sessionToken'))

    return pyarrow.fs.S3FileSystem(**{name: value for name, value in arguments.items() if value is not None})


def _gcs(settings: FilesConnection) -> Any:
    """Without `anonymous`, Google's Application Default Credentials: the
    file GOOGLE_APPLICATION_CREDENTIALS names, `gcloud auth application-default
    login`, or the machine's or the pod's service account.
    """

    import pyarrow.fs

    scheme, authority = settings.endpointParts()
    arguments = {
        'anonymous': bool(settings.anonymous),
        'target_service_account': settings.serviceAccount,
        'scheme': scheme,
        'endpoint_override': authority,
        }

    return pyarrow.fs.GcsFileSystem(**{name: value for name, value in arguments.items() if value is not None})


def _azure(settings: FilesConnection) -> Any:
    """Without a key, a SAS token or a service principal, Azure's default
    chain: the environment's AZURE_CLIENT_ID and friends, a workload or
    managed identity, or `az login`.
    """

    import pyarrow.fs

    scheme, authority = settings.endpointParts()
    arguments = {
        'account_name': settings.azureAccount(),
        'account_key': settings.plain('accountKey'),
        'sas_token': settings.plain('sasToken'),
        'client_id': settings.clientId,
        'client_secret': settings.plain('clientSecret'),
        'tenant_id': settings.tenantId,
        # Blob and Data Lake answer at one address in an emulator.
        'blob_storage_authority': authority, 'dfs_storage_authority': authority,
        'blob_storage_scheme': scheme, 'dfs_storage_scheme': scheme,
        }

    return pyarrow.fs.AzureFileSystem(**{name: value for name, value in arguments.items() if value is not None})


_FILESYSTEMS: Dict[FileStore, Callable[[FilesConnection], Any]] = {FileStore.S3: _s3, FileStore.GCS: _gcs, FileStore.AZURE: _azure}

"""Editing bauta's configuration in place: jobs.yaml and the files its
`include` brings in, as text, so comments and layout survive.

A change is written only if the whole configuration still loads and
validates with it, as `bauta validate` would; the file as it was is kept in
the backup directory, and the change is recorded with its diff. Files are
replaced in one step, so a run starting meanwhile reads the old or the new,
never half. connections.yaml is not edited here: it is where credentials
are named, and is changed where they are managed.
"""
from __future__ import annotations

import datetime
import difflib
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from .. import ConfigurationError, Project
from ..project import UsageError

from .settings import Settings


class EditRefused(Exception):
    """A change that wasn't written, and why."""


class EditConflict(EditRefused):
    """The file changed since the editor read it."""


def digest(text: str) -> str:

    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def editableFiles(settings: Settings, project: Project) -> List[Dict[str, Any]]:
    """The jobs file and its includes, each with the jobs it defines."""

    files = []
    for path in project.document.files:
        jobsHere = sorted(name for name, origin in project.document.origins.items() if origin == path)
        files.append({'path': os.path.relpath(path, settings.configDirectory), 'jobs': jobsHere})

    return files


def _resolve(settings: Settings, project: Project, relative: str) -> Path:
    """`relative` as one of the editable files, or refused: nothing outside
    them is read or written, whatever path is asked for.
    """

    allowed = {os.path.relpath(path, settings.configDirectory): Path(path) for path in project.document.files}
    if relative not in allowed:
        raise EditRefused('{} is not one of the jobs files'.format(relative))

    return allowed[relative]


def readFile(settings: Settings, project: Project, relative: str) -> Dict[str, Any]:

    text = _resolve(settings, project, relative).read_text()

    return {'path': relative, 'text': text, 'digest': digest(text)}


def _replace(path: Path, text: str) -> None:

    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.', suffix='.studio')
    try:
        with os.fdopen(descriptor, 'w') as file:
            file.write(text)
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def writeFile(settings: Settings, project: Project, relative: str, text: str, expectedDigest: str) -> Dict[str, Any]:
    """Writes `text` over `relative` if the file is still as read and the
    configuration validates with it. Returns the diff, for the record.
    """

    path = _resolve(settings, project, relative)
    before = path.read_text()
    if digest(before) != expectedDigest:
        raise EditConflict('{} changed since it was opened; reload it and make the change again'.format(relative))
    if text == before:
        raise EditRefused('nothing changed')

    # Validated as a copy of the whole directory with the change in it, so
    # nothing a run could read is touched until it passes.
    with tempfile.TemporaryDirectory(prefix='studio-check-') as scratch:
        copy = Path(scratch) / 'configuration'
        shutil.copytree(settings.configDirectory, copy, symlinks=True)
        (copy / relative).write_text(text)
        try:
            checked = Project.load(copy)
        except (ConfigurationError, UsageError) as error:
            raise EditRefused(str(error)) from error
        problems = checked.validationProblems()
        if problems:
            raise EditRefused('\n'.join(problems))

    settings.backupDirectory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = settings.backupDirectory / '{}.{}'.format(relative.replace(os.sep, '__'), stamp)
    backup.write_text(before)
    _replace(path, text)

    diff = ''.join(difflib.unified_diff(before.splitlines(keepends=True), text.splitlines(keepends=True), fromfile=relative, tofile=relative))

    return {'path': relative, 'digest': digest(text), 'diff': diff, 'backup': backup.name}

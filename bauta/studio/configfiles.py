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
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Set, Tuple

from .. import ConfigurationError, Project
from ..project import UsageError, readJobs

from .settings import PRIVATE_DIRECTORY_MODE, Settings

# bauta's own transforms, which any edit may name.
OWN_CODE_MODULES = ('bauta.transform.builtinTransforms',)

# One edit at a time, so two can't both pass the check that the file is as
# it was read and the second silently undo the first.
_writing = threading.Lock()


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
            # On disk before it takes the file's place: a machine that lost
            # power just after could otherwise leave the file empty.
            file.flush()
            os.fsync(file.fileno())
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _policyModules(policy: Any, into: Set[str]) -> None:
    """The modules a masking policy names its own strategies from, as
    `module.path:ClassName` -- a column's, a defaultStrategy, or one of json's
    `fields` and `otherwise` -- added to `into`.
    """

    name = policy.get('strategy') if isinstance(policy, Mapping) else policy
    if isinstance(name, str) and ':' in name:
        into.add(name.partition(':')[0].strip())
    if isinstance(policy, Mapping):
        fields = policy.get('fields')
        for nested in (fields.values() if isinstance(fields, Mapping) else []):
            _policyModules(nested, into)
        if 'otherwise' in policy:
            _policyModules(policy['otherwise'], into)


def _codeModules(content: Any) -> Set[str]:
    """Every module a jobs document, as read and before validation, names
    code from: its transforms, and its masking policies' own strategies.
    Read from the document rather than a validated configuration, since
    validating imports a strategy's module.
    """

    modules: Set[str] = set()
    jobs = content.get('jobs') if isinstance(content, Mapping) else None
    for job in (jobs.values() if isinstance(jobs, Mapping) else []):
        if not isinstance(job, Mapping):
            continue
        transforms = job.get('sourceQueryColumnTransforms')
        for references in (transforms.values() if isinstance(transforms, Mapping) else []):
            for reference in (references if isinstance(references, list) else [references]):
                if isinstance(reference, str):
                    modules.add(reference.partition(':')[0].strip())
        masking = job.get('masking')
        if isinstance(masking, Mapping):
            columns = masking.get('columns')
            for policy in (columns.values() if isinstance(columns, Mapping) else []):
                _policyModules(policy, modules)
            _policyModules(masking.get('defaultStrategy'), modules)

    return modules


def _refuseNewCodeModules(settings: Settings, project: Project, edited: Any) -> None:
    """Refuses an edit naming a transform or a strategy from a module nothing
    has vouched for. A transform is any callable in any installed module,
    called with each value -- `os:system` is one -- and naming a strategy
    imports its module, so an edit here could otherwise run code as Studio's
    account. Asked of the edited document before it is validated, since
    validating imports every strategy's module.
    """

    allowed = _codeModules(project.document.content) | set(OWN_CODE_MODULES)
    prefixes = tuple(module + '.' for module in settings.transformModules)
    refused = sorted(module for module in _codeModules(edited) - allowed
                     if module not in settings.transformModules and not module.startswith(prefixes))
    if refused:
        raise EditRefused('transforms and strategies from {} can\'t be added here: they would run as Studio\'s account. Add the module to '
                          'BAUTA_STUDIO_TRANSFORM_MODULES, or add the reference in the file where it is managed'.format(', '.join(refused)))


def writeFile(settings: Settings, project: Project, relative: str, text: str, expectedDigest: str) -> Dict[str, Any]:
    """Writes `text` over `relative` if the file is still as read and the
    configuration validates with it. Returns the diff, for the record.
    """

    return writeFiles(settings, project, {relative: (text, expectedDigest)})[0]


def writeFiles(settings: Settings, project: Project, edits: Mapping[str, Tuple[str, str]]) -> List[Dict[str, Any]]:
    """Writes each of `edits` -- a relative path to (text, digest as read) --
    if every file is still as read and the configuration validates with all
    of them at once; otherwise writes none. Returns each file's diff, for the
    record, in the order given.
    """

    paths = {relative: _resolve(settings, project, relative) for relative in edits}
    with _writing:
        befores = {}
        for relative, (text, expectedDigest) in edits.items():
            befores[relative] = paths[relative].read_text()
            if digest(befores[relative]) != expectedDigest:
                raise EditConflict('{} changed since it was opened; reload it and make the change again'.format(relative))
        if all(text == befores[relative] for relative, (text, _) in edits.items()):
            raise EditRefused('nothing changed')

        # Read with the changes standing in for the files, so nothing a run
        # could read is touched until they pass. A copy of the directory was
        # checked before, which refused every edit where an include reached
        # outside it, and copied run state and any database kept there.
        replacing = {paths[relative]: text for relative, (text, _) in edits.items()}
        try:
            edited = readJobs(project.document.files[0], replacing=replacing).content
        except (ConfigurationError, UsageError) as error:
            raise EditRefused(str(error)) from error
        _refuseNewCodeModules(settings, project, edited)
        try:
            checked = Project.load(settings.configDirectory, replacing=replacing)
        except (ConfigurationError, UsageError) as error:
            raise EditRefused(str(error)) from error
        problems = checked.validationProblems()
        if problems:
            raise EditRefused('\n'.join(problems))

        settings.backupDirectory.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        written = []
        for relative, (text, _) in edits.items():
            backup = settings.backupDirectory / '{}.{}'.format(relative.replace(os.sep, '__'), stamp)
            backup.write_text(befores[relative])
            _replace(paths[relative], text)
            diff = ''.join(difflib.unified_diff(befores[relative].splitlines(keepends=True), text.splitlines(keepends=True),
                                                fromfile=relative, tofile=relative))
            written.append({'path': relative, 'digest': digest(text), 'diff': diff, 'backup': backup.name})

    return written

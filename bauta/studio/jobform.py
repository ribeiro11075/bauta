"""Editing one job as a form: its settings read from the file it is defined
in, and written back into that one mapping, the rest of the file -- other
jobs, comments, anchors, layout -- as it was. What is written then goes
through the same checks as any edit: see configfiles.writeFile.

A setting a job doesn't write in its file takes the file's `defaults:`, or
bauta's own; the form shows those as inherited, and only a setting someone
changes is written. A masking key is never read into the form: it shows
where the key comes from -- `defaults:`, an environment variable, or the
file itself -- and a job that starts masking names a variable for its key.
"""
from __future__ import annotations

import io
import os
import re
from typing import Any, Dict, Optional

from ..project import Project
from .configfiles import EditConflict, EditRefused, readFile, writeFile
from .settings import Settings

# The settings the form edits, by kind; others a job has are left alone.
TEXT_FIELDS = ['sourceConnection', 'targetConnection', 'sourceQuery', 'targetTableFinal', 'targetTableStage', 'insertStrategy', 'watermarkColumn',
               'watermarkInitial']
NUMBER_FIELDS = ['refresh', 'chunkSize', 'retries', 'timeoutSeconds']
FLAG_FIELDS = ['active', 'unmasked']
LIST_FIELDS = ['predecessors']
FIELDS = TEXT_FIELDS + NUMBER_FIELDS + FLAG_FIELDS + LIST_FIELDS
VARIABLE = re.compile(r'^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$')
NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_.-]*$')
DEFAULT_KEY_VARIABLE = 'MASKING_KEY'


def _yaml() -> Any:

    from ruamel.yaml import YAML

    yaml = YAML(typ='rt')
    yaml.preserve_quotes = True
    # Never re-wrapped: a long query stays on its line.
    yaml.width = 1 << 16
    yaml.indent(mapping=2, sequence=4, offset=2)

    return yaml


def _load(text: str) -> Any:

    return _yaml().load(text)


def _dump(document: Any) -> str:

    stream = io.StringIO()
    _yaml().dump(document, stream)

    return stream.getvalue()


def _plain(value: Any) -> Any:
    """A loaded value as plain data, for JSON."""

    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value

    return str(value)


def _keySource(fileMasking: Any, defaults: Any) -> Dict[str, Any]:

    key = fileMasking.get('key') if isinstance(fileMasking, dict) else None
    if key is None:
        defaultKey = (defaults.get('masking') or {}).get('key') if isinstance(defaults, dict) else None
        return {'source': 'defaults' if defaultKey is not None else 'none'}
    found = VARIABLE.match(str(key))
    if found:
        return {'source': 'variable', 'variable': found.group(1)}

    return {'source': 'file'}


def _documentJobs(document: Any, relative: str) -> Any:

    if not isinstance(document, dict) or not isinstance(document.get('jobs'), dict):
        raise EditRefused('{} holds no jobs: mapping'.format(relative))

    return document['jobs']


def choices(project: Project, exclude: Optional[str] = None) -> Dict[str, Any]:
    """What the form's lists offer: strategies, connections, the other jobs."""

    from .. import STRATEGIES

    return {'strategies': sorted(STRATEGIES), 'connections': sorted(project.connections),
            'jobs': sorted(job for job in project.jobsFile.jobs if job != exclude), 'insertStrategies': ['swap', 'upsert', 'append', 'overwrite'],
            'files': [os.path.relpath(path, project.directory) for path in project.document.files], 'inherited': newJobInherited(project)}


def newJobInherited(project: Project) -> Dict[str, Any]:
    """What a new job takes for each setting it doesn't write: the file's
    `defaults:`, else bauta's own default -- active, 5000 rows a chunk.
    """

    from pydantic_core import PydanticUndefined

    from ..configuration import DataJobConfig

    defaults = project.document.content.get('defaults') or {}
    inherited: Dict[str, Any] = {}
    for field in FIELDS:
        if field in defaults:
            inherited[field] = _plain(defaults[field])
            continue
        declared = DataJobConfig.model_fields.get(field)
        default = declared.default if declared is not None else PydanticUndefined
        if default is not PydanticUndefined and default is not None and default != []:
            inherited[field] = default.value if hasattr(default, 'value') else default

    return inherited


def readForm(settings: Settings, project: Project, name: str) -> Dict[str, Any]:
    """The job as the form shows it: what its file writes, what it inherits,
    and its policy column by column. An option-bearing policy is shown as
    custom, kept as it is unless removed.
    """

    origin = project.document.origins.get(name)
    if origin is None:
        raise EditRefused('no job {}'.format(name))
    relative = os.path.relpath(origin, settings.configDirectory)
    opened = readFile(settings, project, relative)
    document = _load(opened['text'])
    raw = _documentJobs(document, relative)[name]
    effective = project.jobsFile.jobs[name]

    fileMasking = raw.get('masking')
    columns = []
    if isinstance(fileMasking, dict) and isinstance(fileMasking.get('columns'), dict):
        for column, policy in fileMasking['columns'].items():
            simple = policy if isinstance(policy, str) else None
            columns.append({'column': str(column), 'strategy': simple, 'custom': simple is None,
                            'summary': None if simple else str(_plain(policy).get('strategy', 'custom') if isinstance(policy, dict) else policy)})
    defaultStrategy = fileMasking.get('defaultStrategy') if isinstance(fileMasking, dict) else None

    return {'name': name, 'file': relative, 'digest': opened['digest'],
            'fields': {field: _plain(raw[field]) for field in FIELDS if field in raw},
            'inherited': {field: _effective(effective, field) for field in FIELDS if field not in raw},
            'masking': {'enabled': isinstance(fileMasking, dict), 'columns': columns,
                        'defaultStrategy': defaultStrategy if isinstance(defaultStrategy, str) else None,
                        'customDefault': defaultStrategy is not None and not isinstance(defaultStrategy, str),
                        'key': _keySource(fileMasking, project.document.content.get('defaults'))},
            'choices': choices(project, exclude=name),
            'others': sorted(str(key) for key in raw if key not in FIELDS and key != 'masking')}


def _effective(job: Any, field: str) -> Any:
    """What a job has for `field` without writing it: from `defaults:`, or
    bauta's own default.
    """

    from enum import Enum

    value = getattr(job, field, None)

    return value.value if isinstance(value, Enum) else _plain(value)


def _setField(mapping: Any, field: str, value: Any) -> None:

    from ruamel.yaml.scalarstring import LiteralScalarString

    if value is None or value == '' or value == []:
        mapping.pop(field, None)
        return
    if field in NUMBER_FIELDS:
        try:
            value = float(value) if field == 'timeoutSeconds' else int(value)
        except (TypeError, ValueError) as error:
            raise EditRefused('{} must be a number'.format(field)) from error
    elif field in FLAG_FIELDS:
        value = bool(value)
    elif field in LIST_FIELDS:
        value = [str(item) for item in value]
    elif isinstance(value, str) and '\n' in value:
        value = LiteralScalarString(value.rstrip('\n') + '\n')
    mapping[field] = value


def _applyMasking(raw: Any, masking: Optional[Dict[str, Any]], defaults: Any) -> None:
    """The job's policy as the form left it. A key is kept as the file has it;
    a job that starts masking without one from `defaults:` names a variable.
    """

    if masking is None:
        return
    if not masking.get('enabled'):
        raw.pop('masking', None)
        return

    current = raw.get('masking') if isinstance(raw.get('masking'), dict) else None
    if current is None:
        from ruamel.yaml.comments import CommentedMap

        current = CommentedMap()
        defaultKey = (defaults.get('masking') or {}).get('key') if isinstance(defaults, dict) else None
        if defaultKey is None:
            variable = str(masking.get('keyVariable') or DEFAULT_KEY_VARIABLE)
            if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', variable):
                raise EditRefused('the key variable must be a name such as MASKING_KEY')
            current['key'] = '${' + variable + '}'
        raw['masking'] = current
        raw.pop('unmasked', None)

    before = current.get('columns') if isinstance(current.get('columns'), dict) else {}
    from ruamel.yaml.comments import CommentedMap

    columns = CommentedMap()
    for entry in masking.get('columns', []):
        column = str(entry.get('column') or '').strip()
        if not column:
            continue
        if column in columns:
            raise EditRefused('column {} is listed twice'.format(column))
        if entry.get('custom'):
            if column not in before:
                raise EditRefused('column {} has no policy of its own to keep'.format(column))
            columns[column] = before[column]
        elif entry.get('strategy'):
            columns[column] = str(entry['strategy'])
        else:
            raise EditRefused('column {} needs a strategy'.format(column))
    current['columns'] = columns

    if 'defaultStrategy' in masking and not masking.get('customDefault'):
        if masking['defaultStrategy']:
            current['defaultStrategy'] = str(masking['defaultStrategy'])
        else:
            current.pop('defaultStrategy', None)


def writeForm(settings: Settings, project: Project, name: str, body: Dict[str, Any], create: Optional[str] = None) -> Dict[str, Any]:
    """Writes the form's job into its file, or with `create` naming a file, a
    new job into it. Returns the write, with its diff.
    """

    from ruamel.yaml.comments import CommentedMap

    if create is not None:
        if not NAME.match(name):
            raise EditRefused('a job is named with letters, digits, _ . and -, starting with a letter or _')
        if name in project.jobsFile.jobs:
            raise EditRefused('there is already a job {}'.format(name))
        relative = create
    else:
        origin = project.document.origins.get(name)
        if origin is None:
            raise EditRefused('no job {}'.format(name))
        relative = os.path.relpath(origin, settings.configDirectory)

    opened = readFile(settings, project, relative)
    if create is None and opened['digest'] != body.get('digest'):
        raise EditConflict('{} changed since the form was opened; reload it and make the change again'.format(relative))
    document = _load(opened['text'])
    if create is not None:
        if not isinstance(document, dict):
            raise EditRefused('{} is not a mapping'.format(relative))
        if not isinstance(document.get('jobs'), dict):
            document['jobs'] = CommentedMap()
        document['jobs'][name] = CommentedMap()
    raw = _documentJobs(document, relative)[name]

    # Only the settings someone changed: the rest stay as written, or
    # inherited. An empty one is removed, to inherit again.
    changes = body.get('changes', {})
    unknown = sorted(set(changes) - set(FIELDS))
    if unknown:
        raise EditRefused('the form does not edit {}'.format(', '.join(unknown)))
    for field, value in changes.items():
        _setField(raw, field, value)

    _applyMasking(raw, body.get('masking'), project.document.content.get('defaults'))

    text = _dump(document)

    return writeFile(settings, project, relative, text, opened['digest'] if create is not None else body['digest'])

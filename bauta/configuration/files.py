"""Reading a jobs.yaml from disk: the files its `include` names, merged into
one document before validation, so `defaults:` and every check apply to the
jobs of all of them as if they were written in one file.
"""
from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, NamedTuple

import yaml

from .environment import ConfigurationError, expandEnvironmentVariables
from .models import isAnchorKey

INCLUDE_KEY = 'include'

# What an included file may hold. Everything else -- workers, defaults, where
# run state goes -- is said once, in the jobs file itself.
INCLUDED_KEYS = ('jobs', 'acknowledged')


class JobsDocument(NamedTuple):
    """A jobs file with its includes merged in: `content` to validate, the
    `files` it came from (the jobs file first), and the file each job is
    defined in.
    """

    content: Dict[str, Any]
    files: List[Path]
    origins: Dict[str, Path]


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping naming one key twice.

    PyYAML keeps the last value without a word, so a policy reading
    `email: email` and, further down, `email: keep` copied every address
    unmasked while the file read as masked. A key given again over a merge
    (`<<: *defaults`) is an override, not a repeat, and stays allowed.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> Any:

        seen: Dict[Any, Any] = {}
        for keyNode, _ in node.value:
            if keyNode.tag == 'tag:yaml.org,2002:merge' or not isinstance(keyNode, yaml.ScalarNode):
                continue
            key = keyNode.value
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, '"{}" is given twice in one mapping, first on line {}; YAML would keep only the second, silently '
                                'dropping the first'.format(key, seen[key].start_mark.line + 1), keyNode.start_mark)
            seen[key] = keyNode

        return super().construct_mapping(node, deep=deep)


def parseYaml(stream: Any) -> Any:
    """YAML from a file or text, as yaml.safe_load reads it, except that a key
    given twice in one mapping is a yaml.YAMLError; see _UniqueKeyLoader.
    """

    return yaml.load(stream, Loader=_UniqueKeyLoader)


def loadYamlFile(path: Path) -> Any:
    """A YAML file, with ${NAME} expanded from the environment."""

    try:
        with open(path) as file:
            return expandEnvironmentVariables(parseYaml(file))
    except FileNotFoundError as error:
        raise ConfigurationError('no such file: {}'.format(path)) from error
    except yaml.YAMLError as error:
        raise ConfigurationError('{} is not valid YAML: {}'.format(path, error)) from error


def _includedFiles(jobsPath: Path, patterns: Any) -> List[Path]:
    """The files `include` names, in the order its patterns give them and
    sorted within each, relative to the jobs file. A pattern that matches
    nothing is an error: a misspelled directory would otherwise drop every
    job in it without a word.
    """

    if isinstance(patterns, str):
        patterns = [patterns]
    if not isinstance(patterns, list) or not all(isinstance(pattern, str) for pattern in patterns):
        raise ConfigurationError('{}: include must be a list of file paths or glob patterns, such as jobs.d/*.yaml'.format(jobsPath))

    files: List[Path] = []
    for pattern in patterns:
        matches = sorted(glob.glob(os.path.join(str(jobsPath.parent), os.path.expanduser(pattern)), recursive=True))
        matches = [match for match in matches if os.path.isfile(match)]
        if not matches:
            raise ConfigurationError('{}: include {} matches no file, relative to {}'.format(jobsPath, pattern, jobsPath.parent))
        for match in matches:
            path = Path(os.path.normpath(match))
            if path.resolve() != jobsPath.resolve() and path not in files:
                files.append(path)

    return files


def readJobsFile(jobsPath: Path, load: Callable[[Path], Any] = loadYamlFile) -> JobsDocument:
    """`jobsPath`, with the jobs and `acknowledged` tables of every file its
    `include` names merged into it. A job defined in two files, or a table
    acknowledged in two, is an error naming both, rather than one silently
    replacing the other. Included files include nothing themselves.
    """

    document = load(jobsPath)
    if document is None:
        document = {}
    if not isinstance(document, Mapping):
        raise ConfigurationError('{} must be a mapping of settings'.format(jobsPath))

    content = dict(document)
    patterns = content.pop(INCLUDE_KEY, None)
    jobs = content.get('jobs') or {}
    origins = {name: jobsPath for name in jobs} if isinstance(jobs, Mapping) else {}
    files = [jobsPath]

    if patterns is None:
        return JobsDocument(content=content, files=files, origins=origins)

    jobs = dict(jobs) if isinstance(jobs, Mapping) else jobs
    acknowledged = {alias: dict(tables) if isinstance(tables, Mapping) else tables
                    for alias, tables in (content.get('acknowledged') or {}).items()}
    acknowledgedIn = {(alias, table): jobsPath for alias, tables in acknowledged.items() if isinstance(tables, Mapping) for table in tables}

    for path in _includedFiles(jobsPath, patterns):
        part = load(path) or {}
        if not isinstance(part, Mapping):
            raise ConfigurationError('{} must be a mapping holding jobs: and acknowledged:'.format(path))
        unexpected = sorted(str(key) for key in part if not isAnchorKey(key) and key not in INCLUDED_KEYS)
        if unexpected:
            raise ConfigurationError('{} holds {}, which only {} may set; an included file holds jobs: and acknowledged: alone'.format(
                path, ', '.join(unexpected), jobsPath))

        partJobs = part.get('jobs') or {}
        if not isinstance(partJobs, Mapping):
            raise ConfigurationError('{}: jobs must be a mapping of job name to job'.format(path))
        for name, job in partJobs.items():
            if name in origins:
                raise ConfigurationError('job {} is defined in both {} and {}'.format(name, origins[name], path))
            origins[name] = path
            jobs[name] = job

        partAcknowledged = part.get('acknowledged') or {}
        if not isinstance(partAcknowledged, Mapping):
            raise ConfigurationError('{}: acknowledged must be a mapping of connection alias to tables'.format(path))
        for alias, tables in partAcknowledged.items():
            if not isinstance(tables, Mapping):
                raise ConfigurationError('{}: acknowledged -> {} must be a mapping of table to reason'.format(path, alias))
            merged = acknowledged.setdefault(alias, {})
            for table, reason in tables.items():
                if (alias, table) in acknowledgedIn:
                    raise ConfigurationError('table {} in {} is acknowledged in both {} and {}'.format(
                        table, alias, acknowledgedIn[(alias, table)], path))
                acknowledgedIn[(alias, table)] = path
                merged[table] = reason

        files.append(path)

    content['jobs'] = jobs
    if acknowledged:
        content['acknowledged'] = acknowledged

    return JobsDocument(content=content, files=files, origins=origins)

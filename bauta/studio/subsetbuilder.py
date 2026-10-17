"""The subset builder: a referentially complete slice of a source, rooted at
one table and a filter, previewed -- the tables in load order, what each
waits for, its rows if counted, the jobs drafted -- and then added to a jobs
file, as `bauta subset` drafts them, through the same checks as any edit.

The filter is SQL run against the source as written, so only an admin builds
a subset.
"""
from __future__ import annotations

import io
from typing import Any, Dict, List

from ..database import Database
from ..generate.subset import SubsetError
from ..project import Project, requireDatabase
from .configfiles import EditRefused, readFile, writeFile
from .settings import Settings

KEY_VARIABLE = 'MASKING_KEY'


def sourceTables(project: Project, connection: str) -> List[str]:

    requireDatabase(project.connections, connection)
    with Database(connectionSettings=project.connections[connection]) as database:
        return sorted(database.listTables())


def _draft(project: Project, request: Dict[str, Any], count: bool) -> Dict[str, Any]:

    from ..generate.discovery import renderJobs
    from ..generate.subset import draftSubset

    source, target = request['connection'], request['target']
    requireDatabase(project.connections, source)
    targetSettings = requireDatabase(project.connections, target)
    if source == target:
        raise EditRefused('the target must differ from the source: a subset is loaded into another database')
    if not str(request.get('where') or '').strip():
        raise EditRefused('a subset needs a filter on its root table')
    # A target that requires masking refuses every job without a policy.
    mask = bool(request.get('mask')) or bool(getattr(targetSettings, 'requireMasking', False))

    with Database(connectionSettings=project.connections[source]) as database:
        try:
            draft = draftSubset(database, root=request['root'], where=request['where'], followChildren=not request.get('noChildren'),
                                ignore=request.get('ignore') or [], mask=mask, rules=project.discoveryRules(), count=count)
        except SubsetError as error:
            raise EditRefused(str(error)) from error

    heading = ['Added by bauta Studio: a subset of {} rooted at {} where {}.'.format(source, request['root'], request['where'])]
    text = renderJobs(draft.drafts, source, target, heading, keyVariable=KEY_VARIABLE, targetType=targetSettings.type, selfContained=True)
    jobs = _jobsOf(text)
    with Database(connectionSettings=project.connections[target]) as database:
        present = {table.upper() for table in database.listTables()}
    # Jobs already loading the same table of the target: a second would
    # overwrite or duplicate what the first loads.
    loadedBy: Dict[str, List[str]] = {}
    for existing, job in project.jobsFile.jobs.items():
        if job.targetConnection == target:
            loadedBy.setdefault(job.targetTableFinal.upper(), []).append(existing)

    return {'mask': mask, 'text': text, 'jobs': jobs, 'tables': [
                {'table': table, 'job': name, 'parents': draft.plan.parents[table], 'rows': (draft.rowCounts or {}).get(table),
                 'targetExists': table.upper() in present, 'clashes': name in project.jobsFile.jobs,
                 'loadedBy': sorted(loadedBy.get(table.upper(), []))}
                for table, name in zip(draft.plan.tables, jobs)],
            'ignored': ['{}.{} -> {}'.format(key.table, ','.join(key.columns), key.referencedTable) for key in draft.plan.ignored]}


def _yaml() -> Any:

    from .jobform import _yaml as yaml

    return yaml()


def _jobsOf(text: str) -> List[str]:

    return [str(name) for name in _yaml().load(text)['jobs']]


def preview(project: Project, request: Dict[str, Any]) -> Dict[str, Any]:
    """What the subset would add, and the command that creates the target
    tables it needs that aren't there.
    """

    drafted = _draft(project, request, count=bool(request.get('count')))
    missing = [entry['table'] for entry in drafted['tables'] if not entry['targetExists']]
    drafted['createMissing'] = ('bauta schema --connection {} --target {} --table {} --related --apply'.format(
        request['connection'], request['target'], request['root']) if missing else None)

    return drafted


def apply(settings: Settings, project: Project, request: Dict[str, Any], relative: str) -> Dict[str, Any]:
    """Adds the subset's jobs into `relative`, one of the jobs files, drafted
    again so what is written is what the source holds now. Refused if any of
    their names is taken.
    """

    drafted = _draft(project, request, count=False)
    clashes = [entry['job'] for entry in drafted['tables'] if entry['clashes']]
    if clashes:
        raise EditRefused('there are already jobs named {}; rename or remove them first'.format(', '.join(clashes)))

    from ruamel.yaml.comments import CommentedMap

    opened = readFile(settings, project, relative)
    yaml = _yaml()
    document = yaml.load(opened['text'])
    if not isinstance(document, dict):
        raise EditRefused('{} is not a mapping'.format(relative))
    if not isinstance(document.get('jobs'), dict):
        document['jobs'] = CommentedMap()
    for name, job in yaml.load(drafted['text'])['jobs'].items():
        document['jobs'][name] = job
    stream = io.StringIO()
    yaml.dump(document, stream)

    written = writeFile(settings, project, relative, stream.getvalue(), opened['digest'])

    return dict(written, jobs=drafted['jobs'])


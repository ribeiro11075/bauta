"""The overview, and what shows a copy is safe: the audit, coverage,
references, manifests, the checks' schedule, and proposals from discovery.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import checks, discovery, evidence, schedule, workspace
from ..auth import record
from ..db import ArchivedManifest, User
from .context import Context, reachingDatabases


class AuditRequest(BaseModel):
    """Whether the audit connects to the databases."""

    connect: bool = False


class CoverageRequest(BaseModel):
    """The connection and schema to check coverage of; every source's when not
    given.
    """

    connection: Optional[str] = None
    schema_: Optional[str] = Field(default=None, alias='schema')


class DiscoveryApply(BaseModel):
    """The jobs whose proposed policies are accepted."""

    jobs: List[str]


class CheckSchedule(BaseModel):
    """The check schedule: whether it runs, at what time in UTC, and which checks."""

    enabled: bool
    time: str
    kinds: List[str]


def _now() -> str:
    """The time now, in UTC, as ISO 8601."""

    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def build(context: Context) -> APIRouter:
    """The routes for what proves a copy is safe: checks, the audit, coverage,
    manifests and dry runs.
    """

    router = APIRouter()
    sessions, viewer, operator, admin, settings, project = context.sessions, context.viewer, context.operator, context.admin, context.settings, context.project

    @router.get('/discovery')
    def discoveryRead(user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """GET /discovery: the latest check for new columns."""

        return {'check': checks.latestDict(session, 'discovery')}

    @router.post('/discovery/check')
    def discoveryCheck(user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """POST /discovery/check: checks the masked jobs for new columns."""

        with reachingDatabases('could not check for new columns'):
            check = checks.runDiscovery(session, project(), user.email)
        record(session, user, 'checked for new columns', drifted=len(check['jobs']))
        session.commit()
        return {'check': check}

    @router.post('/discovery/apply')
    def discoveryApply(body: DiscoveryApply, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """POST /discovery/apply: writes the latest check's proposed policies
        into the chosen jobs. Admins only.
        """

        kept = checks.latestDict(session, 'discovery')
        if kept is None:
            raise HTTPException(status_code=409, detail='check for new columns first')
        written = discovery.apply(settings, project(), kept, body.jobs)
        for write in written:
            record(session, user, 'accepted proposed policies', write['path'], jobs=body.jobs, diff=write['diff'], backup=write['backup'])
        session.commit()
        return {'written': written}

    @router.get('/overview')
    def overview(user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """GET /overview: the home page: recent runs, stale jobs and the latest
        checks.
        """

        current = project()
        evidence.archiveManifest(session, current)
        stale = [entry for entry in schedule.jobSchedule(current, schedule.staleness(session)) if entry['stale']]
        kept, history, complete = workspace.readHistory(current, runs=14)
        allRuns = workspace.runs(current, history, complete)
        latest: Dict[str, Dict[str, Any]] = {}
        for entry in history:
            latest.setdefault(entry['job'], entry)
        failing = [entry for entry in latest.values() if entry['status'] in ('failed', 'skipped')]
        audit = current.audit()
        return {'historyKept': kept, 'runs': allRuns[:14], 'jobCount': len(current.jobsFile.jobs),
                'activeJobCount': sum(job.active for job in current.jobsFile.jobs.values()),
                'failing': failing, 'audit': {'summary': audit['summary'], 'findings': [finding for finding in audit['findings']
                                                                                       if finding['severity'] in ('error', 'warning')][:10]},
                'manifest': workspace.manifestState(current), 'coverage': checks.latestDict(session, 'coverage'), 'stale': stale,
                'news': checks.whatsNew(session)}

    @router.get('/proof')
    def proof(user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """GET /proof: the archived manifests, verified, and the latest checks
        of each kind.
        """

        current = project()
        evidence.archiveManifest(session, current)
        manifests = [{'key': kept.runId, 'archivedAt': kept.archivedAt.isoformat() if kept.archivedAt else None,
                      'generatedAt': kept.manifest.get('generatedAt'), 'jobs': len(kept.manifest.get('jobs', [])),
                      'verification': evidence.verification(kept.manifest)}
                     for kept in session.scalars(select(ArchivedManifest).order_by(ArchivedManifest.archivedAt.desc()).limit(50))]
        coverages = [found[0].asDict() for database in checks.sourceDatabases(current)
                     for found in [checks.latest(session, 'coverage', 1, where=checks._ofDatabase(database))] if found]
        return {'audit': checks.latestDict(session, 'audit-connected') or {'report': current.audit(), 'connected': False, 'at': None},
                'offlineAudit': current.audit(), 'coverages': coverages, 'sources': checks.sourceDatabases(current),
                'references': checks.latestDict(session, 'references'), 'manifest': workspace.manifestState(current), 'manifests': manifests}

    @router.post('/proof/audit')
    def runAudit(body: AuditRequest, user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """POST /proof/audit: runs the audit, stored when it connected."""

        current = project()
        with reachingDatabases('could not run the audit'):
            report = current.audit(connect=body.connect)
        result = {'report': report, 'connected': body.connect, 'at': _now(), 'by': user.email}
        if body.connect:
            result = checks.saveCheck(session, 'audit-connected', user.email, {'report': report, 'connected': True})
        record(session, user, 'ran the audit', 'connected' if body.connect else 'offline', summary=report['summary'])
        session.commit()
        return result

    @router.post('/proof/coverage')
    def runCoverage(body: CoverageRequest, user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """POST /proof/coverage: checks which tables are covered."""

        current = project()
        with reachingDatabases('could not check coverage'):
            results = checks.runCoverage(session, current, user.email, connection=body.connection, schema=body.schema_)
        record(session, user, 'checked coverage', ', '.join(result['report']['database'] for result in results))
        session.commit()
        return {'checks': results}

    @router.post('/proof/references')
    def runReferences(user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """POST /proof/references: checks the targets for orphaned rows."""

        with reachingDatabases('could not check references'):
            result = checks.runReferences(session, project(), user.email)
        record(session, user, 'checked references', summary=result['report']['summary'])
        session.commit()
        return result

    @router.get('/checks/schedule')
    def checkSchedule(user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """GET /checks/schedule: the check schedule."""

        return {'schedule': checks.schedule(session), 'kinds': checks.KINDS}

    @router.put('/checks/schedule')
    def setCheckSchedule(body: CheckSchedule, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """PUT /checks/schedule: sets the check schedule. Admins only."""

        try:
            value = checks.setSchedule(session, body.enabled, body.time, body.kinds)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        record(session, user, 'changed the check schedule', **value)
        session.commit()
        return value

    @router.post('/proof/dry-run')
    def dryRun(user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """POST /proof/dry-run: validates the configuration and dry-runs every job."""

        current = project()
        problems = current.validationProblems() + current.dryRunProblems()
        record(session, user, 'ran a dry run', problems=len(problems))
        session.commit()
        return {'problems': problems, 'at': _now()}

    return router

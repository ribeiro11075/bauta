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
    connect: bool = False


class CoverageRequest(BaseModel):
    connection: Optional[str] = None
    schema_: Optional[str] = Field(default=None, alias='schema')


class DiscoveryApply(BaseModel):
    jobs: List[str]


class CheckSchedule(BaseModel):
    enabled: bool
    time: str
    kinds: List[str]


def _now() -> str:

    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def build(context: Context) -> APIRouter:

    router = APIRouter()
    sessions, viewer, operator, admin, settings, project = context.sessions, context.viewer, context.operator, context.admin, context.settings, context.project

    @router.get('/discovery')
    def discoveryRead(user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        return {'check': checks.latestDict(session, 'discovery')}

    @router.post('/discovery/check')
    def discoveryCheck(user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        with reachingDatabases('could not check for new columns'):
            check = checks.runDiscovery(session, project(), user.email)
        record(session, user, 'checked for new columns', drifted=len(check['jobs']))
        session.commit()
        return {'check': check}

    @router.post('/discovery/apply')
    def discoveryApply(body: DiscoveryApply, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
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
        current = project()
        with reachingDatabases('could not check coverage'):
            results = checks.runCoverage(session, current, user.email, connection=body.connection, schema=body.schema_)
        record(session, user, 'checked coverage', ', '.join(result['report']['database'] for result in results))
        session.commit()
        return {'checks': results}

    @router.post('/proof/references')
    def runReferences(user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        with reachingDatabases('could not check references'):
            result = checks.runReferences(session, project(), user.email)
        record(session, user, 'checked references', summary=result['report']['summary'])
        session.commit()
        return result

    @router.get('/checks/schedule')
    def checkSchedule(user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        return {'schedule': checks.schedule(session), 'kinds': checks.KINDS}

    @router.put('/checks/schedule')
    def setCheckSchedule(body: CheckSchedule, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        try:
            value = checks.setSchedule(session, body.enabled, body.time, body.kinds)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        record(session, user, 'changed the check schedule', **value)
        session.commit()
        return value

    @router.post('/proof/dry-run')
    def dryRun(user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        current = project()
        problems = current.validationProblems() + current.dryRunProblems()
        record(session, user, 'ran a dry run', problems=len(problems))
        session.commit()
        return {'problems': problems, 'at': _now()}

    return router

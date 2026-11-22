"""Runs: those in history and running now, starting and cancelling them, and
a run's evidence.
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...project import toolVersion
from .. import evidence, workspace
from ..auth import record
from ..db import Activity, LaunchedRun, User
from ..launcher import CancelRefused, cancelRun
from ..settings import HISTORY_RUNS
from .context import Context


class NewRun(BaseModel):
    """The jobs a run is started for, every due job when none, and whether it
    is a full refresh.
    """

    jobs: List[str] = Field(default_factory=list)
    fullRefresh: bool = False


def build(context: Context) -> APIRouter:
    """The routes for runs: history, live runs, launching, cancelling and evidence."""

    router = APIRouter()
    sessions, viewer, operator, launcher, project = context.sessions, context.viewer, context.operator, context.launcher, context.project

    def _launched(session: Session, liveByPid: Dict[int, str], limit: int = 100) -> List[LaunchedRun]:
        """The latest runs Studio launched, each brought up to date with its process."""

        launchedRuns = list(session.scalars(select(LaunchedRun).order_by(LaunchedRun.id.desc()).limit(limit)))
        for launched in launchedRuns:
            launcher.refresh(session, launched, liveByPid)
        return launchedRuns

    @router.get('/runs')
    def listRuns(user: User = Depends(viewer), session: Session = Depends(sessions), limit: int = Query(HISTORY_RUNS, ge=1, le=500)) -> Dict[str, Any]:
        """GET /runs: recent runs from history and those running now, with who
        started each from Studio.
        """

        current = project()
        kept, history, complete = workspace.readHistory(current, runs=limit)
        allRuns = workspace.runs(current, history, complete)[:limit + 10]
        live = current.running()
        launchedRuns = _launched(session, {record['pid']: record['runId'] for record in live})
        evidence.archiveManifest(session, current)
        startedBy = {launched.runId: launched.email for launched in launchedRuns if launched.runId}
        for run in allRuns:
            run['startedBy'] = startedBy.get(run['runId'])
        # Started here but not yet, or never, a run: refused for a lock, or
        # failing before its first cycle.
        pending = [launched.asDict() for launched in launchedRuns if launched.runId is None][:10]
        return {'historyKept': kept, 'runs': allRuns, 'launched': pending}

    @router.get('/runs/compare')
    def compare(earlier: str, later: str, user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """GET /runs/compare: two runs side by side, manifests included."""

        current = project()
        _, history, _ = workspace.readHistory(current)
        runs = {runId: workspace.runDetail(current, history, runId) for runId in (earlier, later)}
        missing = [runId for runId, found in runs.items() if found is None]
        if missing:
            raise HTTPException(status_code=404, detail='no run {} in history'.format(', '.join(missing)))
        return workspace.compareRuns(runs[earlier], runs[later], evidence.manifestOf(session, earlier), evidence.manifestOf(session, later))  # type: ignore[arg-type]

    @router.get('/runs/{runId}')
    def getRun(runId: str, user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """GET /runs/{runId}: one run, its jobs, and who started or cancelled it."""

        current = project()
        _, history, _ = workspace.readHistory(current)
        detail = workspace.runDetail(current, history, runId)
        launched = session.scalar(select(LaunchedRun).where(LaunchedRun.runId == runId))
        cancelled = session.scalars(select(Activity).where(Activity.action == 'cancelled a run', Activity.target == runId)).first()
        if detail is None:
            raise HTTPException(status_code=404, detail='no run {} in history or running now'.format(runId))
        if launched is not None:
            launcher.refresh(session, launched, {})
            detail['launched'] = launched.asDict()
            detail['log'] = launcher.log(launched)
        detail['cancelledBy'] = cancelled.email if cancelled is not None else None
        detail['hasManifest'] = evidence.manifestOf(session, runId) is not None
        return detail

    @router.get('/launched/{launchedId}')
    def getLaunched(launchedId: int, user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """GET /launched/{launchedId}: one run Studio launched, its log
        included. A run that has just finished has its manifest archived before
        another replaces it.
        """

        launched = session.get(LaunchedRun, launchedId)
        if launched is None:
            raise HTTPException(status_code=404, detail='no such launched run')
        current = project()
        finishedBefore = launched.finishedAt is not None
        launcher.refresh(session, launched, {record['pid']: record['runId'] for record in current.running()})
        if launched.finishedAt is not None and not finishedBefore:
            evidence.archiveManifest(session, current)
        return dict(launched.asDict(), log=launcher.log(launched))

    @router.post('/runs/{runId}/cancel')
    def cancel(runId: str, user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """POST /runs/{runId}/cancel: stops a run that is running."""

        live = next((found for found in project().running() if found['runId'] == runId), None)
        if live is None:
            raise HTTPException(status_code=409, detail='the run is not running')
        try:
            cancelRun(live)
        except CancelRefused as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        record(session, user, 'cancelled a run', runId)
        session.commit()
        return {'cancelled': True}

    @router.post('/launched/{launchedId}/cancel')
    def cancelLaunched(launchedId: int, user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """POST /launched/{launchedId}/cancel: stops a run Studio launched."""

        launched = session.get(LaunchedRun, launchedId)
        if launched is None or launched.finishedAt is not None or not launcher.cancel(launched):
            raise HTTPException(status_code=409, detail='the run is not running')
        record(session, user, 'cancelled a run', launched.runId or 'launched {}'.format(launchedId))
        session.commit()
        return {'cancelled': True}

    @router.get('/runs/{runId}/evidence')
    def runEvidence(runId: str, user: User = Depends(viewer), session: Session = Depends(sessions)) -> Response:
        """GET /runs/{runId}/evidence: a zip of what proves what the run did."""

        current = project()
        evidence.archiveManifest(session, current)
        _, history, _ = workspace.readHistory(current)
        detail = workspace.runDetail(current, history, runId)
        if detail is None:
            raise HTTPException(status_code=404, detail='no run {} in history or running now'.format(runId))
        name, content = evidence.bundle(session, current, detail, toolVersion())
        record(session, user, 'downloaded evidence', runId)
        session.commit()
        return Response(content, media_type='application/zip', headers={'Content-Disposition': 'attachment; filename="{}"'.format(name)})

    @router.post('/runs')
    def startRun(body: NewRun, user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """POST /runs: starts `bauta run` for the jobs given."""

        current = project()
        unknown = [job for job in body.jobs if job not in current.jobsFile.jobs]
        if unknown:
            raise HTTPException(status_code=400, detail='no such job(s): {}'.format(', '.join(unknown)))
        launched = launcher.start(session, user, body.jobs, body.fullRefresh)
        record(session, user, 'started a run', ', '.join(body.jobs) or 'every due job', launchedId=launched.id, fullRefresh=body.fullRefresh)
        session.commit()
        return launched.asDict()

    return router

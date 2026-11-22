"""Jobs: reading them, editing them as forms, measuring them, and when each
is due.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...configuration import isLake
from ...database import Database
from ...generate.schema import DEFAULT_STAGE_SUFFIX, SchemaError, createStageTable
from ...jobs.fullRefresh import fullRefreshProblem
from ...log.scrubbing import describeError
from ...project import requireDatabase
from .. import checks, configfiles, jobform, schedule, workspace
from ..auth import record
from ..db import User
from ..launcher import BenchRefused, startBench
from .context import Context

# TREND_RUNS: runs of a job its page reads back, for its trend.
TREND_RUNS = 60


class BenchRequest(BaseModel):
    """How many rows a benchmark reads."""

    rows: int = Field(default=100_000, ge=1, le=10_000_000)


class JobForm(BaseModel):
    """A job's form: its changed fields and masking, with the digest of the
    file it was read from.
    """

    digest: Optional[str] = None
    changes: Dict[str, Any] = Field(default_factory=dict)
    masking: Optional[Dict[str, Any]] = None


class NewJob(JobForm):
    """A new job's form, with its name and the file it is added to."""

    name: str
    file: str


class Staleness(BaseModel):
    """How many hours after its last completed run a job is stale, by default
    and for each job that differs.
    """

    defaultHours: float
    jobs: Dict[str, float] = Field(default_factory=dict)


def build(context: Context) -> APIRouter:
    """The routes for jobs: listing, reading, editing, benchmarking and the
    schedule.
    """

    router = APIRouter()
    sessions, viewer, operator, admin = context.sessions, context.viewer, context.operator, context.admin
    settings, database, project = context.settings, context.database, context.project

    def _writeJob(name: str, body: Dict[str, Any], user: User, session: Session, create: Optional[str] = None) -> Dict[str, Any]:
        """Writes a job's form into its file, or adds a new job to `create`,
        and logs the change.
        """

        written = jobform.writeForm(settings, project(), name, body, create=create)
        record(session, user, 'added a job' if create else 'edited a job', name, file=written['path'], diff=written['diff'], backup=written['backup'])
        session.commit()
        return written

    @router.get('/run-options')
    def runOptions(user: User = Depends(viewer)) -> Dict[str, Any]:
        """Whether `run --full-refresh` could replace every incremental job,
        asked before anyone ticks the box rather than after a run fails.
        """

        current = project()
        problems = []
        for name, job in sorted(current.jobsFile.jobs.items()):
            target = current.connections.get(job.targetConnection)
            problem = fullRefreshProblem(job, target) if job.active and job.watermarkColumn else None
            if not problem:
                continue
            # A database target lacks only a stage table, which can be made
            # from the target table itself; files need a job of their own.
            fix = None
            if target is not None and not isLake(target):
                stage = job.targetTableFinal + DEFAULT_STAGE_SUFFIX
                fix = {'stageTable': stage, 'command': 'bauta schema --connection {0} --target {0} --table {1} --stage-suffix {2} --apply'.format(
                    job.targetConnection, job.targetTableFinal, DEFAULT_STAGE_SUFFIX)}
            # Named first, as `run --full-refresh` names it: "orders has no targetTableStage".
            problems.append({'job': name, 'message': '{} {}'.format(name, problem), 'fix': fix})
        return {'fullRefresh': {'possible': not problems, 'problems': problems}}

    @router.post('/jobs/{name}/stage-table')
    def createStage(name: str, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """Gives an incremental job a stage table, so a full refresh can
        replace its target: created in the target from the target table's
        own definition, then named in the job's targetTableStage, validated
        and recorded as any edit is.
        """

        current = project()
        job = current.jobsFile.jobs.get(name)
        if job is None:
            raise HTTPException(status_code=404, detail='no job {}'.format(name))
        if job.targetTableStage:
            raise HTTPException(status_code=409, detail='{} already loads through {}'.format(name, job.targetTableStage))
        settings_ = requireDatabase(current.connections, job.targetConnection)
        try:
            with Database(connectionSettings=settings_) as target:
                stage, created = createStageTable(target, job.targetTableFinal)
        except SchemaError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=422, detail='could not create the stage table: {}'.format(describeError(error))) from error

        form = jobform.readForm(settings, current, name)
        written = _writeJob(name, {'digest': form['digest'], 'changes': {'targetTableStage': stage}}, user, session)
        record(session, user, 'created a stage table' if created else 'used an existing stage table', '{}.{}'.format(job.targetConnection, stage), job=name)
        session.commit()
        return {'stageTable': stage, 'created': created, 'diff': written['diff']}

    @router.get('/jobs')
    def listJobs(user: User = Depends(viewer)) -> Dict[str, Any]:
        """GET /jobs: every job with its latest runs, and the files jobs can be
        added to.
        """

        current = project()
        _, history, _ = workspace.readHistory(current, runs=10)
        return {'jobs': workspace.jobs(current, history), 'files': configfiles.editableFiles(settings, current)}

    @router.get('/jobs/{name}')
    def getJob(name: str, user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """GET /jobs/{name}: one job, its recent runs and its latest benchmark."""

        current = project()
        if name not in current.jobsFile.jobs:
            raise HTTPException(status_code=404, detail='no job {}'.format(name))
        found = current.history()
        records = found.read(limit=TREND_RUNS, job=name) if found is not None else []
        benches = checks.latest(session, 'bench', 1, where=lambda check: bool(check.result.get('job') == name))
        return dict(workspace.describeJob(current, name), history=records, bench=benches[0].asDict() if benches else None)

    @router.get('/form-choices')
    def formChoices(user: User = Depends(admin)) -> Dict[str, Any]:
        """GET /form-choices: what the job form offers: connections, strategies
        and the like. Admins only.
        """

        return jobform.choices(project())

    @router.post('/jobs/{name}/bench')
    def bench(name: str, body: BenchRequest, user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """POST /jobs/{name}/bench: starts a benchmark of the job."""

        if name not in project().jobsFile.jobs:
            raise HTTPException(status_code=404, detail='no job {}'.format(name))
        try:
            started = startBench(settings, database, user.email, name, body.rows)
        except BenchRefused as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        record(session, user, 'measured a job', name, rows=body.rows)
        session.commit()
        return started

    @router.get('/jobs/{name}/form')
    def jobFormRead(name: str, user: User = Depends(admin)) -> Dict[str, Any]:
        """GET /jobs/{name}/form: the job as its form edits it. Admins only."""

        try:
            return jobform.readForm(settings, project(), name)
        except configfiles.EditRefused as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @router.put('/jobs/{name}/form')
    def jobFormWrite(name: str, body: JobForm, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """PUT /jobs/{name}/form: writes the job's form. Admins only."""

        return _writeJob(name, body.model_dump(), user, session)

    @router.post('/jobs')
    def jobCreate(body: NewJob, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """POST /jobs: adds a new job to a file. Admins only."""

        return _writeJob(body.name, body.model_dump(), user, session, create=body.file)

    @router.get('/schedule')
    def jobsSchedule(user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """GET /schedule: when each job is next due and whether it is stale."""

        thresholds = schedule.staleness(session)
        return {'jobs': schedule.jobSchedule(project(), thresholds), 'staleness': thresholds}

    @router.put('/schedule/staleness')
    def setStaleness(body: Staleness, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """PUT /schedule/staleness: sets when jobs go stale. Admins only."""

        try:
            value = schedule.setStaleness(session, body.defaultHours, body.jobs)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        record(session, user, 'changed when jobs go stale', **value)
        session.commit()
        return value

    return router

"""The HTTP API under /api, and the web app everywhere else.

Viewers read; operators also start runs and run the checks that connect to
a database; admins also manage users and edit the jobs files. Every change
is recorded in the activity log with who made it.
"""
from __future__ import annotations

import contextlib
import datetime
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import checks, configfiles, discovery, evidence, jobform, schedule, subsetbuilder, workspace
from .auth import LoginThrottle, checkPassword, endSession, endSessionsOf, hashPassword, record, requireRole, startSession, userFromRequest
from .db import ADMIN, OPERATOR, ROLES, VIEWER, Activity, ArchivedManifest, Database, LaunchedRun, User
from .launcher import CancelRefused, Launcher, cancelRun, startBench
from .settings import HISTORY_RUNS, Settings

STATIC_DIRECTORY = Path(__file__).parent / 'static'
ACTIVITY_PAGE = 200
# How often the manifest `run` last wrote is looked at, to keep it by run.
ARCHIVE_SECONDS = 10.0
# Runs of a job its page reads back, for its trend.
TREND_RUNS = 60

logger = logging.getLogger(__name__)


class Login(BaseModel):
    email: str
    password: str


class NewRun(BaseModel):
    jobs: List[str] = Field(default_factory=list)
    fullRefresh: bool = False


class FileEdit(BaseModel):
    text: str
    digest: str


class NewUser(BaseModel):
    email: str
    name: str = ''
    role: str = VIEWER
    password: str


class UserChange(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = None


class AuditRequest(BaseModel):
    connect: bool = False


class CoverageRequest(BaseModel):
    connection: Optional[str] = None
    schema_: Optional[str] = Field(default=None, alias='schema')


class Staleness(BaseModel):
    defaultHours: float
    jobs: Dict[str, float] = Field(default_factory=dict)


class DiscoveryApply(BaseModel):
    jobs: List[str]


class CheckSchedule(BaseModel):
    enabled: bool
    time: str
    kinds: List[str]


class SubsetRequest(BaseModel):
    connection: str
    target: str
    root: str
    where: str
    noChildren: bool = False
    ignore: List[str] = Field(default_factory=list)
    mask: bool = False
    count: bool = False
    file: Optional[str] = None


class BenchRequest(BaseModel):
    rows: int = Field(default=100_000, ge=1, le=10_000_000)


class JobForm(BaseModel):
    digest: Optional[str] = None
    changes: Dict[str, Any] = Field(default_factory=dict)
    masking: Optional[Dict[str, Any]] = None


class NewJob(JobForm):
    name: str
    file: str


def _archiveLoop(settings: Settings, database: Database, stop: threading.Event) -> None:
    """Keeps each manifest `run` writes, by run, as it appears: see
    evidence.archiveManifest.
    """

    while not stop.wait(ARCHIVE_SECONDS):
        try:
            with database.open() as session:
                evidence.archiveManifest(session, workspace.loadProject(settings))
        except Exception as error:
            logger.warning('Could not keep the latest manifest: {}'.format(error))


def createApp(settings: Settings) -> FastAPI:

    database = Database(settings.databaseUrl)
    launcher = Launcher(settings)
    throttle = LoginThrottle()

    def sessions() -> Any:
        yield from database.session()

    viewer = requireRole(VIEWER, sessions)
    operator = requireRole(OPERATOR, sessions)
    admin = requireRole(ADMIN, sessions)

    def project() -> Any:
        try:
            return workspace.loadProject(settings)
        except workspace.ConfigurationProblem as error:
            raise HTTPException(status_code=409, detail={'configuration': str(error)}) from error

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        stop = threading.Event()
        threading.Thread(target=_archiveLoop, args=(settings, database, stop), name='studio-manifests', daemon=True).start()
        threading.Thread(target=checks.scheduleLoop, args=(database, lambda: workspace.loadProject(settings), stop), name='studio-checks',
                         daemon=True).start()
        try:
            yield
        finally:
            stop.set()

    app = FastAPI(title='bauta Studio', docs_url=None, redoc_url=None, openapi_url='/api/openapi.json', lifespan=lifespan)
    api = APIRouter(prefix='/api')
    app.state.database, app.state.launcher, app.state.settings = database, launcher, settings

    # Sign in ------------------------------------------------------------------

    @api.post('/auth/login')
    def login(body: Login, request: Request, response: Response, session: Session = Depends(sessions)) -> Dict[str, Any]:
        if request.headers.get('X-Bauta-Studio') != '1':
            raise HTTPException(status_code=403, detail='missing the X-Bauta-Studio header')
        email = body.email.strip().lower()
        if not throttle.allowed(email):
            raise HTTPException(status_code=429, detail='too many failed attempts; try again in a few minutes')
        user = session.scalar(select(User).where(User.email == email))
        if not checkPassword(user, body.password) or user is None or not user.active:
            throttle.failed(email)
            raise HTTPException(status_code=401, detail='wrong email or password')
        throttle.succeeded(email)
        record(session, user, 'signed in')
        startSession(session, user, response, settings.sessionHours, settings.secureCookies)
        return user.asDict()

    @api.post('/auth/logout')
    def logout(request: Request, response: Response, session: Session = Depends(sessions)) -> Dict[str, Any]:
        endSession(session, request, response)
        return {}

    @api.get('/auth/me')
    def me(request: Request, session: Session = Depends(sessions)) -> Dict[str, Any]:
        user = userFromRequest(session, request)
        if user is None:
            hasUsers = bool(session.scalar(select(func.count()).select_from(User)))
            raise HTTPException(status_code=401, detail={'signedIn': False, 'hasUsers': hasUsers})
        return user.asDict()

    # Runs ---------------------------------------------------------------------

    def _launched(session: Session, liveByPid: Dict[int, str], limit: int = 100) -> List[LaunchedRun]:
        launchedRuns = list(session.scalars(select(LaunchedRun).order_by(LaunchedRun.id.desc()).limit(limit)))
        for launched in launchedRuns:
            launcher.refresh(session, launched, liveByPid)
        return launchedRuns

    @api.get('/runs')
    def listRuns(user: User = Depends(viewer), session: Session = Depends(sessions), limit: int = Query(HISTORY_RUNS, ge=1, le=500)) -> Dict[str, Any]:
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

    @api.get('/runs/compare')
    def compare(earlier: str, later: str, user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        current = project()
        _, history, _ = workspace.readHistory(current)
        runs = {runId: workspace.runDetail(current, history, runId) for runId in (earlier, later)}
        missing = [runId for runId, found in runs.items() if found is None]
        if missing:
            raise HTTPException(status_code=404, detail='no run {} in history'.format(', '.join(missing)))
        return workspace.compareRuns(runs[earlier], runs[later], evidence.manifestOf(session, earlier), evidence.manifestOf(session, later))  # type: ignore[arg-type]

    @api.get('/runs/{runId}')
    def getRun(runId: str, user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
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

    @api.get('/launched/{launchedId}')
    def getLaunched(launchedId: int, user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        launched = session.get(LaunchedRun, launchedId)
        if launched is None:
            raise HTTPException(status_code=404, detail='no such launched run')
        current = project()
        finishedBefore = launched.finishedAt is not None
        launcher.refresh(session, launched, {record['pid']: record['runId'] for record in current.running()})
        if launched.finishedAt is not None and not finishedBefore:
            # The run just wrote its manifest; keep it before another replaces it.
            evidence.archiveManifest(session, current)
        return dict(launched.asDict(), log=launcher.log(launched))

    @api.get('/run-options')
    def runOptions(user: User = Depends(viewer)) -> Dict[str, Any]:
        """Whether `run --full-refresh` could replace every incremental job,
        asked before anyone ticks the box rather than after a run fails.
        """

        from ..configuration import isLake
        from ..generate.schema import DEFAULT_STAGE_SUFFIX
        from ..jobs.fullRefresh import fullRefreshProblem

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

    @api.post('/jobs/{name}/stage-table')
    def createStage(name: str, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """Gives an incremental job a stage table, so a full refresh can
        replace its target: created in the target from the target table's
        own definition, then named in the job's targetTableStage, validated
        and recorded as any edit is.
        """

        from .. import Database
        from ..generate.schema import SchemaError, createStageTable
        from ..log.scrubbing import describeError
        from ..project import UsageError, requireDatabase

        current = project()
        job = current.jobsFile.jobs.get(name)
        if job is None:
            raise HTTPException(status_code=404, detail='no job {}'.format(name))
        if job.targetTableStage:
            raise HTTPException(status_code=409, detail='{} already loads through {}'.format(name, job.targetTableStage))
        try:
            settings_ = requireDatabase(current.connections, job.targetConnection)
            with Database(connectionSettings=settings_) as database:
                stage, created = createStageTable(database, job.targetTableFinal)
        except (SchemaError, UsageError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=422, detail='could not create the stage table: {}'.format(describeError(error))) from error

        form = jobform.readForm(settings, current, name)
        written = _writeJob(name, {'digest': form['digest'], 'changes': {'targetTableStage': stage}}, user, session)
        record(session, user, 'created a stage table' if created else 'used an existing stage table', '{}.{}'.format(job.targetConnection, stage), job=name)
        session.commit()
        return {'stageTable': stage, 'created': created, 'diff': written['diff']}

    @api.post('/runs/{runId}/cancel')
    def cancel(runId: str, user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
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

    @api.post('/launched/{launchedId}/cancel')
    def cancelLaunched(launchedId: int, user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        launched = session.get(LaunchedRun, launchedId)
        if launched is None or launched.finishedAt is not None or not launcher.cancel(launched):
            raise HTTPException(status_code=409, detail='the run is not running')
        record(session, user, 'cancelled a run', launched.runId or 'launched {}'.format(launchedId))
        session.commit()
        return {'cancelled': True}

    @api.get('/runs/{runId}/evidence')
    def runEvidence(runId: str, user: User = Depends(viewer), session: Session = Depends(sessions)) -> Response:
        from ..cli.common import _toolVersion

        current = project()
        evidence.archiveManifest(session, current)
        _, history, _ = workspace.readHistory(current)
        detail = workspace.runDetail(current, history, runId)
        if detail is None:
            raise HTTPException(status_code=404, detail='no run {} in history or running now'.format(runId))
        name, content = evidence.bundle(session, current, detail, _toolVersion())
        record(session, user, 'downloaded evidence', runId)
        session.commit()
        return Response(content, media_type='application/zip', headers={'Content-Disposition': 'attachment; filename="{}"'.format(name)})

    @api.post('/runs')
    def startRun(body: NewRun, user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        current = project()
        unknown = [job for job in body.jobs if job not in current.jobsFile.jobs]
        if unknown:
            raise HTTPException(status_code=400, detail='no such job(s): {}'.format(', '.join(unknown)))
        launched = launcher.start(session, user, body.jobs, body.fullRefresh)
        record(session, user, 'started a run', ', '.join(body.jobs) or 'every due job', launchedId=launched.id, fullRefresh=body.fullRefresh)
        session.commit()
        return launched.asDict()

    # Jobs ---------------------------------------------------------------------

    @api.get('/jobs')
    def listJobs(user: User = Depends(viewer)) -> Dict[str, Any]:
        current = project()
        _, history, _ = workspace.readHistory(current, runs=10)
        return {'jobs': workspace.jobs(current, history), 'files': configfiles.editableFiles(settings, current)}

    @api.get('/jobs/{name}')
    def getJob(name: str, user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        current = project()
        if name not in current.jobsFile.jobs:
            raise HTTPException(status_code=404, detail='no job {}'.format(name))
        found = current.history()
        records = found.read(limit=TREND_RUNS, job=name) if found is not None else []
        benches = checks.latest(session, 'bench', 1, where=lambda check: bool(check.result.get('job') == name))
        return dict(workspace.describeJob(current, name), history=records, bench=benches[0].asDict() if benches else None)

    @api.get('/form-choices')
    def formChoices(user: User = Depends(admin)) -> Dict[str, Any]:
        return jobform.choices(project())

    @api.post('/jobs/{name}/bench')
    def bench(name: str, body: BenchRequest, user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        if name not in project().jobsFile.jobs:
            raise HTTPException(status_code=404, detail='no job {}'.format(name))
        started = startBench(settings, database, user.email, name, body.rows)
        record(session, user, 'measured a job', name, rows=body.rows)
        session.commit()
        return started

    @api.get('/jobs/{name}/form')
    def jobFormRead(name: str, user: User = Depends(admin)) -> Dict[str, Any]:
        try:
            return jobform.readForm(settings, project(), name)
        except configfiles.EditRefused as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    def _writeJob(name: str, body: Dict[str, Any], user: User, session: Session, create: Optional[str] = None) -> Dict[str, Any]:
        try:
            written = jobform.writeForm(settings, project(), name, body, create=create)
        except configfiles.EditConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except configfiles.EditRefused as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        record(session, user, 'added a job' if create else 'edited a job', name, file=written['path'], diff=written['diff'], backup=written['backup'])
        session.commit()
        return written

    @api.put('/jobs/{name}/form')
    def jobFormWrite(name: str, body: JobForm, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        return _writeJob(name, body.model_dump(), user, session)

    @api.post('/jobs')
    def jobCreate(body: NewJob, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        return _writeJob(body.name, body.model_dump(), user, session, create=body.file)

    # Schedule -----------------------------------------------------------------

    @api.get('/schedule')
    def jobsSchedule(user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        thresholds = schedule.staleness(session)
        return {'jobs': schedule.jobSchedule(project(), thresholds), 'staleness': thresholds}

    @api.put('/schedule/staleness')
    def setStaleness(body: Staleness, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        try:
            value = schedule.setStaleness(session, body.defaultHours, body.jobs)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        record(session, user, 'changed when jobs go stale', **value)
        session.commit()
        return value

    # Discovery ----------------------------------------------------------------

    @api.get('/discovery')
    def discoveryRead(user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        return {'check': checks.latestDict(session, 'discovery')}

    @api.post('/discovery/check')
    def discoveryCheck(user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        check = checks.runDiscovery(session, project(), user.email)
        record(session, user, 'checked for new columns', drifted=len(check['jobs']))
        session.commit()
        return {'check': check}

    @api.post('/discovery/apply')
    def discoveryApply(body: DiscoveryApply, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        kept = checks.latestDict(session, 'discovery')
        if kept is None:
            raise HTTPException(status_code=409, detail='check for new columns first')
        try:
            written = discovery.apply(settings, project(), kept, body.jobs)
        except configfiles.EditConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except configfiles.EditRefused as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        for write in written:
            record(session, user, 'accepted proposed policies', write['path'], jobs=body.jobs, diff=write['diff'], backup=write['backup'])
        session.commit()
        return {'written': written}

    # Overview -----------------------------------------------------------------

    @api.get('/overview')
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

    # Proof --------------------------------------------------------------------

    @api.get('/proof')
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

    @api.post('/proof/audit')
    def runAudit(body: AuditRequest, user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        current = project()
        report = current.audit(connect=body.connect)
        result = {'report': report, 'connected': body.connect, 'at': _now(), 'by': user.email}
        if body.connect:
            result = checks.saveCheck(session, 'audit-connected', user.email, {'report': report, 'connected': True})
        record(session, user, 'ran the audit', 'connected' if body.connect else 'offline', summary=report['summary'])
        session.commit()
        return result

    @api.post('/proof/coverage')
    def runCoverage(body: CoverageRequest, user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        from ..project import UsageError

        current = project()
        try:
            results = checks.runCoverage(session, current, user.email, connection=body.connection, schema=body.schema_)
        except UsageError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        record(session, user, 'checked coverage', ', '.join(result['report']['database'] for result in results))
        session.commit()
        return {'checks': results}

    @api.post('/proof/references')
    def runReferences(user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        result = checks.runReferences(session, project(), user.email)
        record(session, user, 'checked references', summary=result['report']['summary'])
        session.commit()
        return result

    @api.get('/checks/schedule')
    def checkSchedule(user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        return {'schedule': checks.schedule(session), 'kinds': checks.KINDS}

    @api.put('/checks/schedule')
    def setCheckSchedule(body: CheckSchedule, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        try:
            value = checks.setSchedule(session, body.enabled, body.time, body.kinds)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        record(session, user, 'changed the check schedule', **value)
        session.commit()
        return value

    @api.post('/proof/dry-run')
    def dryRun(user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        current = project()
        problems = current.validationProblems() + current.dryRunProblems()
        record(session, user, 'ran a dry run', problems=len(problems))
        session.commit()
        return {'problems': problems, 'at': _now()}

    # Connections --------------------------------------------------------------

    @api.get('/connections')
    def listConnections(user: User = Depends(viewer)) -> Dict[str, Any]:
        return {'connections': workspace.connections(project())}

    @api.post('/connections/{alias}/test')
    def test(alias: str, user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        current = project()
        if alias not in current.connections:
            raise HTTPException(status_code=404, detail='no connection {}'.format(alias))
        result = workspace.testConnection(current, alias)
        record(session, user, 'tested a connection', alias, ok=result['ok'])
        session.commit()
        return result

    # Configuration ------------------------------------------------------------

    @api.get('/configuration')
    def configuration(user: User = Depends(viewer)) -> Dict[str, Any]:
        try:
            current = workspace.loadProject(settings)
        except workspace.ConfigurationProblem as error:
            return {'valid': False, 'problem': str(error), 'files': []}
        return {'valid': True, 'problems': current.validationProblems(), 'files': configfiles.editableFiles(settings, current),
                'directory': str(settings.configDirectory)}

    @api.get('/configuration/file')
    def readConfigurationFile(path: str, user: User = Depends(admin)) -> Dict[str, Any]:
        try:
            return configfiles.readFile(settings, project(), path)
        except configfiles.EditRefused as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @api.put('/configuration/file')
    def writeConfigurationFile(path: str, body: FileEdit, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        try:
            written = configfiles.writeFile(settings, project(), path, body.text, body.digest)
        except configfiles.EditConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except configfiles.EditRefused as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        record(session, user, 'edited a jobs file', path, diff=written['diff'], backup=written['backup'])
        session.commit()
        return written

    # Subset builder -----------------------------------------------------------

    @api.get('/subset/tables')
    def subsetTables(connection: str, user: User = Depends(admin)) -> Dict[str, Any]:
        from ..project import UsageError

        try:
            return {'tables': subsetbuilder.sourceTables(project(), connection)}
        except UsageError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @api.post('/subset/preview')
    def subsetPreview(body: SubsetRequest, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        from ..project import UsageError

        try:
            previewed = subsetbuilder.preview(project(), body.model_dump())
        except (configfiles.EditRefused, UsageError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        record(session, user, 'previewed a subset', body.root, connection=body.connection, where=body.where, counted=body.count)
        session.commit()
        return previewed

    @api.post('/subset/apply')
    def subsetApply(body: SubsetRequest, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        from ..project import UsageError

        current = project()
        relative = body.file or configfiles.editableFiles(settings, current)[0]['path']
        try:
            written = subsetbuilder.apply(settings, current, body.model_dump(), relative)
        except configfiles.EditConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (configfiles.EditRefused, UsageError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        record(session, user, 'added a subset\'s jobs', relative, jobs=written['jobs'], root=body.root, where=body.where, diff=written['diff'],
               backup=written['backup'])
        session.commit()
        return written

    # Users and activity -------------------------------------------------------

    @api.get('/users')
    def listUsers(user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        return {'users': [found.asDict() for found in session.scalars(select(User).order_by(User.email))], 'roles': ROLES}

    @api.post('/users')
    def addUser(body: NewUser, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        email = body.email.strip().lower()
        if body.role not in ROLES:
            raise HTTPException(status_code=400, detail='role must be one of {}'.format(', '.join(ROLES)))
        if session.scalar(select(User).where(User.email == email)) is not None:
            raise HTTPException(status_code=409, detail='{} already has an account'.format(email))
        try:
            passwordHash = hashPassword(body.password)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        added = User(email=email, name=body.name, role=body.role, passwordHash=passwordHash)
        session.add(added)
        record(session, user, 'added a user', email, role=body.role)
        session.commit()
        return added.asDict()

    @api.patch('/users/{userId}')
    def changeUser(userId: int, body: UserChange, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        found = session.get(User, userId)
        if found is None:
            raise HTTPException(status_code=404, detail='no such user')
        changes: Dict[str, Any] = {}
        if body.role is not None:
            if body.role not in ROLES:
                raise HTTPException(status_code=400, detail='role must be one of {}'.format(', '.join(ROLES)))
            changes['role'] = found.role = body.role
        if body.name is not None:
            changes['name'] = found.name = body.name
        if body.active is not None:
            changes['active'] = found.active = body.active
        if body.password is not None:
            try:
                found.passwordHash = hashPassword(body.password)
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            changes['password'] = 'changed'
        remainingAdmins = session.scalar(select(func.count()).select_from(User).where(User.role == ADMIN, User.active.is_(True)))
        if not remainingAdmins:
            session.rollback()
            raise HTTPException(status_code=400, detail='that would leave no active admin')
        if body.password is not None or body.active is False:
            endSessionsOf(session, found)
        record(session, user, 'changed a user', found.email, **changes)
        session.commit()
        return found.asDict()

    @api.get('/activity')
    def activity(user: User = Depends(admin), session: Session = Depends(sessions), before: Optional[int] = None) -> Dict[str, Any]:
        query = select(Activity).order_by(Activity.id.desc()).limit(ACTIVITY_PAGE)
        if before is not None:
            query = query.where(Activity.id < before)
        return {'activity': [entry.asDict() for entry in session.scalars(query)]}

    app.include_router(api)

    @app.exception_handler(HTTPException)
    def httpError(request: Request, error: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content={'detail': error.detail}, headers=error.headers)

    @app.middleware('http')
    async def securityHeaders(request: Request, callNext: Any) -> Response:
        response: Response = await callNext(request)
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('Referrer-Policy', 'same-origin')
        response.headers.setdefault('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                               "font-src 'self'; frame-ancestors 'none'")
        if request.url.path.startswith('/api/'):
            response.headers.setdefault('Cache-Control', 'no-store')
        return response

    @app.get('/{path:path}', include_in_schema=False)
    def webApp(path: str) -> Response:
        # The built app's files, and its index for every route it handles.
        if path.startswith('api/'):
            raise HTTPException(status_code=404, detail='no such endpoint')
        candidate = (STATIC_DIRECTORY / path).resolve()
        if path and candidate.is_file() and STATIC_DIRECTORY.resolve() in candidate.parents:
            return FileResponse(candidate)
        index = STATIC_DIRECTORY / 'index.html'
        if not index.exists():
            return JSONResponse(status_code=503, content={'detail': 'the web app is not built: run `npm run build` in web/'})
        return FileResponse(index, headers={'Cache-Control': 'no-cache'})

    return app


def _now() -> str:

    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')

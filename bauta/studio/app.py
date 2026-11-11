"""The HTTP API under /api, and the web app everywhere else.

Viewers read; operators also start runs and run the checks that connect to
a database; admins also manage users and edit the jobs files. Every change
is recorded in the activity log with who made it. Each part of the API is a
module of .routes; what is here is what they all share.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import traceback
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from ..log.scrubbing import describeError, scrubWithin
from ..project import UsageError
from . import checks, configfiles, evidence, routes, workspace
from .auth import LoginThrottle, endExpiredSessions, requireRole
from .db import ADMIN, OPERATOR, VIEWER, Database
from .launcher import Launcher, endInterruptedBenches
from .routes.context import Context
from .settings import Settings

STATIC_DIRECTORY = Path(__file__).parent / 'static'
# How often the manifest `run` last wrote is looked at, to keep it by run.
ARCHIVE_SECONDS = 10.0
# All a browser is told of a failure nothing expected: what went wrong is in
# Studio's log, with its message scrubbed.
UNEXPECTED_ERROR = 'something went wrong on the server; its log says what'

logger = logging.getLogger(__name__)


def _archiveLoop(settings: Settings, database: Database, stop: threading.Event) -> None:
    """Keeps each manifest `run` writes, by run, as it appears: see
    evidence.archiveManifest.
    """

    while not stop.wait(ARCHIVE_SECONDS):
        try:
            with database.open() as session:
                evidence.archiveManifest(session, workspace.loadProject(settings))
        except Exception as error:
            logger.warning('Could not keep the latest manifest: {}'.format(describeError(error)))


def _addErrorHandlers(app: FastAPI) -> None:

    @app.exception_handler(HTTPException)
    def httpError(request: Request, error: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content={'detail': error.detail}, headers=error.headers)

    @app.exception_handler(configfiles.EditConflict)
    def editConflict(request: Request, error: configfiles.EditConflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={'detail': str(error)})

    @app.exception_handler(configfiles.EditRefused)
    def editRefused(request: Request, error: configfiles.EditRefused) -> JSONResponse:
        return JSONResponse(status_code=422, content={'detail': str(error)})

    @app.exception_handler(UsageError)
    def usageError(request: Request, error: UsageError) -> JSONResponse:
        return JSONResponse(status_code=400, content={'detail': str(error)})


def _addMiddleware(app: FastAPI) -> None:

    @app.middleware('http')
    async def guarded(request: Request, callNext: Any) -> Response:
        response: Response
        try:
            response = await callNext(request)
        except Exception as error:
            # Answered here, not by an exception handler: Starlette re-raises
            # after those, for the server to log, and uvicorn would write the
            # driver's message as it stands, values and all.
            logger.error('{} {} failed:\n{}'.format(request.method, request.url.path, scrubWithin(traceback.format_exc(), error)))
            response = JSONResponse(status_code=500, content={'detail': UNEXPECTED_ERROR})
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('Referrer-Policy', 'same-origin')
        response.headers.setdefault('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                               "font-src 'self'; frame-ancestors 'none'")
        if request.url.path.startswith('/api/'):
            response.headers.setdefault('Cache-Control', 'no-store')
        return response


def _addWebApp(app: FastAPI) -> None:

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


def createApp(settings: Settings) -> FastAPI:

    database = Database(settings.databaseUrl)
    launcher = Launcher(settings)
    endInterruptedBenches(database)
    endExpiredSessions(database)

    def sessions() -> Any:
        yield from database.session()

    context = Context(settings=settings, database=database, launcher=launcher, throttle=LoginThrottle(), sessions=sessions,
                      viewer=requireRole(VIEWER, sessions), operator=requireRole(OPERATOR, sessions), admin=requireRole(ADMIN, sessions))

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
    app.state.database, app.state.launcher, app.state.settings = database, launcher, settings

    api = APIRouter(prefix='/api')
    for module in routes.MODULES:
        api.include_router(module.build(context))
    app.include_router(api)

    _addErrorHandlers(app)
    _addMiddleware(app)
    _addWebApp(app)

    return app

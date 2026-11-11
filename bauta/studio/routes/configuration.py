"""Connections, the jobs files as text, and the subset builder."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import configfiles, subsetbuilder, workspace
from ..auth import record
from ..db import User
from .context import Context, reachingDatabases


class FileEdit(BaseModel):
    text: str
    digest: str


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


def build(context: Context) -> APIRouter:

    router = APIRouter()
    sessions, viewer, operator, admin, settings, project = context.sessions, context.viewer, context.operator, context.admin, context.settings, context.project

    @router.get('/connections')
    def listConnections(user: User = Depends(viewer)) -> Dict[str, Any]:
        return {'connections': workspace.connections(project())}

    @router.post('/connections/{alias}/test')
    def test(alias: str, user: User = Depends(operator), session: Session = Depends(sessions)) -> Dict[str, Any]:
        current = project()
        if alias not in current.connections:
            raise HTTPException(status_code=404, detail='no connection {}'.format(alias))
        result = workspace.testConnection(current, alias)
        record(session, user, 'tested a connection', alias, ok=result['ok'])
        session.commit()
        return result

    @router.get('/configuration')
    def configuration(user: User = Depends(viewer)) -> Dict[str, Any]:
        try:
            current = workspace.loadProject(settings)
        except workspace.ConfigurationProblem as error:
            return {'valid': False, 'problem': str(error), 'files': []}
        return {'valid': True, 'problems': current.validationProblems(), 'files': configfiles.editableFiles(settings, current),
                'directory': str(settings.configDirectory)}

    @router.get('/configuration/file')
    def readConfigurationFile(path: str, user: User = Depends(admin)) -> Dict[str, Any]:
        try:
            return configfiles.readFile(settings, project(), path)
        except configfiles.EditRefused as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @router.put('/configuration/file')
    def writeConfigurationFile(path: str, body: FileEdit, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        written = configfiles.writeFile(settings, project(), path, body.text, body.digest)
        record(session, user, 'edited a jobs file', path, diff=written['diff'], backup=written['backup'])
        session.commit()
        return written

    @router.get('/subset/tables')
    def subsetTables(connection: str, user: User = Depends(admin)) -> Dict[str, Any]:
        with reachingDatabases('could not list its tables'):
            return {'tables': subsetbuilder.sourceTables(project(), connection)}

    @router.post('/subset/preview')
    def subsetPreview(body: SubsetRequest, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        with reachingDatabases('could not draft the subset'):
            previewed = subsetbuilder.preview(project(), body.model_dump())
        record(session, user, 'previewed a subset', body.root, connection=body.connection, where=body.where, counted=body.count)
        session.commit()
        return previewed

    @router.post('/subset/apply')
    def subsetApply(body: SubsetRequest, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        current = project()
        relative = body.file or configfiles.editableFiles(settings, current)[0]['path']
        with reachingDatabases('could not draft the subset'):
            written = subsetbuilder.apply(settings, current, body.model_dump(), relative)
        record(session, user, 'added a subset\'s jobs', relative, jobs=written['jobs'], root=body.root, where=body.where, diff=written['diff'],
               backup=written['backup'])
        session.commit()
        return written

    return router

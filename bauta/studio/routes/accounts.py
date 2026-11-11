"""Signing in, the accounts admins manage, and the activity log."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import PasswordCheckBusy, checkPassword, endSession, endSessionsOf, hashPassword, record, startSession, userFromRequest
from ..db import ADMIN, ROLES, VIEWER, Activity, User
from .context import Context

ACTIVITY_PAGE = 200
# How long a browser is asked to wait when too many sign-ins are being checked.
BUSY_RETRY_SECONDS = '5'


class Login(BaseModel):
    email: str
    password: str


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


def build(context: Context) -> APIRouter:

    router = APIRouter()
    sessions, admin, settings, throttle = context.sessions, context.admin, context.settings, context.throttle

    @router.post('/auth/login')
    def login(body: Login, request: Request, response: Response, session: Session = Depends(sessions)) -> Dict[str, Any]:
        if request.headers.get('X-Bauta-Studio') != '1':
            raise HTTPException(status_code=403, detail='missing the X-Bauta-Studio header')
        email = body.email.strip().lower()
        address = request.client.host if request.client is not None else ''
        if not throttle.allowed(email, address):
            raise HTTPException(status_code=429, detail='too many failed attempts; try again in a few minutes')
        user = session.scalar(select(User).where(User.email == email))
        try:
            correct = checkPassword(user, body.password)
        except PasswordCheckBusy as error:
            raise HTTPException(status_code=503, detail=str(error), headers={'Retry-After': BUSY_RETRY_SECONDS}) from error
        if not correct or user is None or not user.active:
            throttle.failed(email, address)
            raise HTTPException(status_code=401, detail='wrong email or password')
        throttle.succeeded(email, address)
        record(session, user, 'signed in')
        startSession(session, user, response, settings.sessionHours, settings.secureCookies)
        return user.asDict()

    @router.post('/auth/logout')
    def logout(request: Request, response: Response, session: Session = Depends(sessions)) -> Dict[str, Any]:
        endSession(session, request, response)
        return {}

    @router.get('/auth/me')
    def me(request: Request, session: Session = Depends(sessions)) -> Dict[str, Any]:
        user = userFromRequest(session, request)
        if user is None:
            hasUsers = bool(session.scalar(select(func.count()).select_from(User)))
            raise HTTPException(status_code=401, detail={'signedIn': False, 'hasUsers': hasUsers})
        return user.asDict()

    @router.get('/users')
    def listUsers(user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        return {'users': [found.asDict() for found in session.scalars(select(User).order_by(User.email))], 'roles': ROLES}

    @router.post('/users')
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

    @router.patch('/users/{userId}')
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

    @router.get('/activity')
    def activity(user: User = Depends(admin), session: Session = Depends(sessions), before: Optional[int] = None) -> Dict[str, Any]:
        query = select(Activity).order_by(Activity.id.desc()).limit(ACTIVITY_PAGE)
        if before is not None:
            query = query.where(Activity.id < before)
        return {'activity': [entry.asDict() for entry in session.scalars(query)]}

    return router

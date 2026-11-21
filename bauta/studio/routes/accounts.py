"""Signing in, the accounts admins manage, and the activity log."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import twofactor
from ..auth import (CSRF_HEADER, PasswordCheckBusy, changingAccounts, checkPassword, endSession, endSessionsOf, hashPassword, record, startSession,
                    userFromRequest)
from ..db import ADMIN, ROLES, VIEWER, Activity, User
from ..settings import PasswordLogin, RoleSource, Settings
from .context import Context

ACTIVITY_PAGE = 200
# How long a browser is asked to wait when too many sign-ins are being checked.
BUSY_RETRY_SECONDS = '5'


class Login(BaseModel):
    email: str
    password: str


class PasswordChange(BaseModel):
    current: str
    new: str


class NewUser(BaseModel):
    email: str
    name: str = ''
    role: str = VIEWER
    # None for someone who will sign in only through the identity provider.
    password: Optional[str] = None


class UserChange(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = None
    # Takes the password away, from someone who signs in through the identity
    # provider: disabling them there is then enough to keep them out.
    removePassword: bool = False


def _requireRole(role: str) -> None:

    if role not in ROLES:
        raise HTTPException(status_code=400, detail='role must be one of {}'.format(', '.join(ROLES)))


def _signInOptions(settings: Settings) -> Dict[str, Any]:
    """How the sign-in page offers to sign in: shown before anyone has."""

    return {'sso': {'label': settings.sso.label} if settings.sso is not None else None, 'passwordLogin': settings.passwordLogin}


def _rolesFromProvider(settings: Settings, user: User) -> bool:
    """Whether `user`'s role is set by the identity provider, not here."""

    return settings.sso is not None and settings.sso.roles == RoleSource.PROVIDER and user.ssoIdentity is not None


def build(context: Context) -> APIRouter:

    router = APIRouter()
    sessions, viewer, admin, settings, throttle = context.sessions, context.viewer, context.admin, context.settings, context.throttle

    @router.post('/auth/login')
    def login(body: Login, request: Request, response: Response, session: Session = Depends(sessions)) -> Dict[str, Any]:
        if request.headers.get(CSRF_HEADER) != '1':
            raise HTTPException(status_code=403, detail='missing the {} header'.format(CSRF_HEADER))
        if settings.passwordLogin == PasswordLogin.OFF:
            raise HTTPException(status_code=403, detail='signing in with a password is turned off; sign in with single sign-on')
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
        # Said only once the password is right, so it tells no one else who
        # has an account.
        if settings.passwordLogin == PasswordLogin.ADMINS and user.role != ADMIN:
            raise HTTPException(status_code=403, detail='only admins sign in with a password here; sign in with single sign-on')
        # A code from an authenticator next, or setting one up first, for an
        # account whose role needs it: not signed in until then.
        if user.totpSecret is not None or twofactor.required(settings, user):
            nextStep = twofactor.CODE if user.totpSecret is not None else twofactor.ENROL
            response.set_cookie(twofactor.PENDING_COOKIE, twofactor.sealPending(context.secretBox, user, nextStep), max_age=twofactor.PENDING_SECONDS,
                                httponly=True, secure=settings.secureCookies, samesite='strict', path=twofactor.PENDING_PATH)
            return {'twoFactorStep': nextStep}
        record(session, user, 'signed in', method='password')
        startSession(session, user, response, settings.sessionHours, settings.secureCookies, request)
        return user.asDict()

    @router.post('/auth/password')
    def changePassword(body: PasswordChange, request: Request, response: Response, user: User = Depends(viewer),
                       session: Session = Depends(sessions)) -> Dict[str, Any]:
        """Changes the signed-in user's own password, given the one they have:
        a session left open on a shared machine doesn't let someone else take
        the account. Wrong ones are counted as failed sign-ins. Every other
        session of theirs ends; this browser's starts anew.
        """

        if user.passwordHash is None:
            raise HTTPException(status_code=409, detail='you sign in through the identity provider: change your password there')
        address = request.client.host if request.client is not None else ''
        if not throttle.allowed(user.email, address):
            raise HTTPException(status_code=429, detail='too many failed attempts; try again in a few minutes')
        try:
            correct = checkPassword(user, body.current)
            same = correct and checkPassword(user, body.new)
        except PasswordCheckBusy as error:
            raise HTTPException(status_code=503, detail=str(error), headers={'Retry-After': BUSY_RETRY_SECONDS}) from error
        if not correct:
            throttle.failed(user.email, address)
            raise HTTPException(status_code=403, detail='your current password is wrong')
        throttle.succeeded(user.email, address)
        if same:
            raise HTTPException(status_code=400, detail='choose a password other than the one you have')
        try:
            user.passwordHash = hashPassword(body.new)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        endSessionsOf(session, user)
        record(session, user, 'changed their password')
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
            raise HTTPException(status_code=401, detail={'signedIn': False, 'hasUsers': hasUsers, **_signInOptions(settings)})
        return user.asDict()

    @router.get('/users')
    def listUsers(user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        sso = settings.sso
        return {'users': [found.asDict() for found in session.scalars(select(User).order_by(User.email))], 'roles': ROLES,
                'sso': {'roles': sso.roles} if sso is not None else None, 'passwordLogin': settings.passwordLogin,
                'requireTwoFactor': settings.requireTwoFactor}

    @router.post('/users')
    def addUser(body: NewUser, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        email = body.email.strip().lower()
        _requireRole(body.role)
        if session.scalar(select(User).where(User.email == email)) is not None:
            raise HTTPException(status_code=409, detail='{} already has an account'.format(email))
        if body.password is None and settings.sso is None:
            raise HTTPException(status_code=400, detail='a password is needed: single sign-on is off')
        try:
            passwordHash = hashPassword(body.password) if body.password is not None else None
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        added = User(email=email, name=body.name, role=body.role, passwordHash=passwordHash)
        session.add(added)
        record(session, user, 'added a user', email, role=body.role, password=passwordHash is not None)
        session.commit()
        return added.asDict()

    @router.patch('/users/{userId}')
    def changeUser(userId: int, body: UserChange, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        with changingAccounts:
            return _changeUser(userId, body, user, session)

    @router.delete('/users/{userId}/sso')
    def unlinkUser(userId: int, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """Forgets who `userId` is at the identity provider, for when their
        identity there was made again: their next sign-in links them anew, by
        email. They are signed out everywhere meanwhile.
        """

        with changingAccounts:
            found = session.get(User, userId)
            if found is None:
                raise HTTPException(status_code=404, detail='no such user')
            if found.ssoIdentity is None:
                raise HTTPException(status_code=409, detail='{} is not linked to single sign-on'.format(found.email))
            found.ssoIdentity = None
            endSessionsOf(session, found)
            record(session, user, 'unlinked single sign-on', found.email)
            session.commit()
            return found.asDict()

    def _changeUser(userId: int, body: UserChange, user: User, session: Session) -> Dict[str, Any]:
        found = session.get(User, userId)
        if found is None:
            raise HTTPException(status_code=404, detail='no such user')
        changes: Dict[str, Any] = {}
        if body.role is not None and body.role != found.role and _rolesFromProvider(settings, found):
            raise HTTPException(status_code=409, detail='{}\'s role is set by the identity provider, from their groups'.format(found.email))
        if body.role is not None:
            _requireRole(body.role)
            changes['role'] = found.role = body.role
        if body.name is not None:
            changes['name'] = found.name = body.name
        if body.active is not None:
            changes['active'] = found.active = body.active
        if body.removePassword:
            if body.password is not None:
                raise HTTPException(status_code=400, detail='set a password or remove it, not both')
            if found.ssoIdentity is None:
                raise HTTPException(status_code=409, detail='{} would have no way to sign in: they sign in with single sign-on first'
                                    .format(found.email))
            found.passwordHash = None
            twofactor.turnOff(found)
            changes['password'] = 'removed'
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
        if body.password is not None or body.removePassword or body.active is False:
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

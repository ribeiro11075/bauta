"""Two-factor sign-in: typing an authenticator's code after a password,
setting an app up, turning it off, new recovery codes, and an admin's reset.
See ..twofactor.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import twofactor
from ..auth import CSRF_HEADER, changingAccounts, checkPassword, endSessionsOf, record, startSession, userFromRequest
from ..db import User
from .context import Context


class Code(BaseModel):
    code: str


class TurnOff(BaseModel):
    password: str
    code: str


def build(context: Context) -> APIRouter:

    router = APIRouter()
    sessions, settings, viewer, admin = context.sessions, context.settings, context.viewer, context.admin
    box, throttle = context.secretBox, context.codeThrottle

    def requireHeader(request: Request) -> None:
        if request.headers.get(CSRF_HEADER) != '1':
            raise HTTPException(status_code=403, detail='missing the {} header'.format(CSRF_HEADER))

    def pending(request: Request, session: Session) -> Optional[Tuple[User, str]]:
        return twofactor.openPending(box, session, request.cookies.get(twofactor.PENDING_COOKIE))

    def forgetPending(response: Response) -> None:
        response.delete_cookie(twofactor.PENDING_COOKIE, path=twofactor.PENDING_PATH, secure=settings.secureCookies, httponly=True, samesite='strict')

    def settingUp(request: Request, session: Session) -> Tuple[User, bool]:
        """Who is setting an app up: someone signed in, or someone whose
        password was right and whose role needs one before they sign in.
        """

        requireHeader(request)
        signedIn = userFromRequest(session, request)
        if signedIn is not None:
            return signedIn, False
        found = pending(request, session)
        if found is not None and found[1] == twofactor.ENROL:
            return found[0], True
        raise HTTPException(status_code=401, detail='sign in first')

    def checked(request: Request, session: Session, user: User, code: str, secret: Optional[str] = None) -> str:
        """How `code` proves `user` has their second factor, from their app or
        a recovery code, or with `secret`, one being set up; throttled as
        passwords are, more tightly.
        """

        address = request.client.host if request.client is not None else ''
        if not throttle.allowed(user.email, address):
            raise HTTPException(status_code=429, detail='too many wrong codes; try again in a few minutes')
        if secret is not None:
            how = 'authenticator' if twofactor.acceptCode(user, secret, code) else None
        else:
            try:
                how = twofactor.checkSecondFactor(session, box, user, code)
            except twofactor.SecretKeyProblem as error:
                raise HTTPException(status_code=500, detail=str(error)) from error
        if how is None:
            throttle.failed(user.email, address)
            record(session, user, 'gave a wrong two-factor code')
            session.commit()
            raise HTTPException(status_code=401, detail='wrong code')
        throttle.succeeded(user.email, address)
        return how

    @router.post('/auth/2fa/verify')
    def verify(body: Code, request: Request, response: Response, session: Session = Depends(sessions)) -> Dict[str, Any]:
        requireHeader(request)
        found = pending(request, session)
        if found is None or found[1] != twofactor.CODE:
            raise HTTPException(status_code=401, detail='sign in with your password again: that sign-in has expired')
        user = found[0]
        how = checked(request, session, user, body.code)
        record(session, user, 'signed in', method='password', secondFactor=how)
        startSession(session, user, response, settings.sessionHours, settings.secureCookies, request)
        forgetPending(response)
        return {**user.asDict(), 'recoveryCodesLeft': len(user.recoveryCodes)}

    @router.post('/auth/2fa/setup')
    def setup(request: Request, session: Session = Depends(sessions)) -> Dict[str, str]:
        user, _ = settingUp(request, session)
        if user.passwordHash is None:
            raise HTTPException(status_code=409, detail='you sign in through the identity provider, which asks for its own second factor')
        if user.totpSecret is not None:
            raise HTTPException(status_code=409, detail='two-factor sign-in is already on; turn it off first to set up another app')
        secret = twofactor.newSecret()
        user.totpPendingSecret = box.encrypt(secret)
        session.commit()
        return twofactor.setupDetails(secret, user.email)

    @router.post('/auth/2fa/confirm')
    def confirm(body: Code, request: Request, response: Response, session: Session = Depends(sessions)) -> Dict[str, Any]:
        """Turns two-factor sign-in on once a code shows the app holds the
        secret. Every other session of the account ends, and this browser's
        starts anew, signed in with both factors.
        """

        user, _ = settingUp(request, session)
        if user.totpPendingSecret is None or user.totpSecret is not None:
            raise HTTPException(status_code=409, detail='set up an app first')
        try:
            secret = box.decrypt(user.totpPendingSecret)
        except twofactor.SecretKeyProblem as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        user.totpLastStep = None
        checked(request, session, user, body.code, secret=secret)
        with changingAccounts:
            user.totpSecret, user.totpPendingSecret = user.totpPendingSecret, None
            codes = twofactor.newRecoveryCodes(user)
            endSessionsOf(session, user)
            record(session, user, 'turned on two-factor sign-in')
            startSession(session, user, response, settings.sessionHours, settings.secureCookies, request)
        forgetPending(response)
        return {'recoveryCodes': codes, 'user': user.asDict()}

    @router.post('/auth/2fa/recovery-codes')
    def recoveryCodes(body: Code, request: Request, user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        if user.totpSecret is None:
            raise HTTPException(status_code=409, detail='two-factor sign-in is off')
        checked(request, session, user, body.code)
        codes = twofactor.newRecoveryCodes(user)
        record(session, user, 'made new recovery codes')
        session.commit()
        return {'recoveryCodes': codes}

    @router.post('/auth/2fa/off')
    def off(body: TurnOff, request: Request, user: User = Depends(viewer), session: Session = Depends(sessions)) -> Dict[str, Any]:
        if user.totpSecret is None:
            raise HTTPException(status_code=409, detail='two-factor sign-in is off')
        if twofactor.required(settings, user):
            raise HTTPException(status_code=409, detail='your role needs two-factor sign-in: set up another app instead, or ask an admin to reset it')
        if not checkPassword(user, body.password):
            raise HTTPException(status_code=403, detail='wrong password')
        checked(request, session, user, body.code)
        twofactor.turnOff(user)
        record(session, user, 'turned off two-factor sign-in')
        session.commit()
        return user.asDict()

    @router.delete('/users/{userId}/2fa')
    def reset(userId: int, user: User = Depends(admin), session: Session = Depends(sessions)) -> Dict[str, Any]:
        """Turns `userId`'s two-factor sign-in off, for someone who lost their
        app and their recovery codes: they set it up again, at their next
        sign-in if their role needs it. They are signed out everywhere.
        """

        with changingAccounts:
            found = session.get(User, userId)
            if found is None:
                raise HTTPException(status_code=404, detail='no such user')
            if found.totpSecret is None and found.totpPendingSecret is None:
                raise HTTPException(status_code=409, detail='{} has no two-factor sign-in'.format(found.email))
            twofactor.turnOff(found)
            endSessionsOf(session, found)
            record(session, user, 'reset two-factor sign-in', found.email)
            session.commit()
            return found.asDict()

    return router

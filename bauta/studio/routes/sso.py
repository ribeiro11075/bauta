"""Signing in through the identity provider: /auth/sso/start sends the
browser there, and /auth/sso/callback is where it comes back. See ..sso.
"""
from __future__ import annotations

import hmac
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ...log.scrubbing import describeError
from ..auth import changingAccounts, record, startSession
from ..sso import BUSY, PENDING_PATH, PENDING_SECONDS, PROVIDER, STATE, TOKEN, SsoFailure, pendingCookie, signInWith
from .context import Context

CALLBACK_PATH = '/api/auth/sso/callback'
# The longest error code from the provider written to the log.
PROVIDER_ERROR_LENGTH = 100

logger = logging.getLogger(__name__)


def _onward(target: str) -> RedirectResponse:
    """On to the web app. The session cookie is SameSite=Strict, and this
    redirect continues the provider's cross-site navigation, so the browser
    doesn't send the cookie for the page itself; it doesn't need to: the page
    is the same for everyone, and the app then asks /api/auth/me from Studio's
    own origin, which it is sent with.
    """

    return RedirectResponse(target, status_code=303)


class CallbackQueryFilter(logging.Filter):
    """Drops the query from the callback's line in uvicorn's access log: it
    holds the authorization code, which belongs in no log.
    """

    def filter(self, record: logging.LogRecord) -> bool:

        arguments: Any = record.args
        if isinstance(arguments, tuple) and len(arguments) >= 3 and isinstance(arguments[2], str) and arguments[2].startswith(CALLBACK_PATH + '?'):
            record.args = arguments[:2] + (CALLBACK_PATH + '?…',) + arguments[3:]

        return True


def build(context: Context) -> APIRouter:

    router = APIRouter()
    sessions, settings, oidc, throttle = context.sessions, context.settings, context.oidc, context.ssoThrottle

    @router.get('/auth/sso/start')
    def start() -> Response:
        if oidc is None:
            raise HTTPException(status_code=404, detail='single sign-on is off')
        login = oidc.newLogin()
        try:
            url = oidc.authorizationUrl(login)
        except SsoFailure as failure:
            logger.warning('Single sign-on could not start: {}'.format(failure))
            return _onward('/?sso_error={}'.format(failure.code))
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(pendingCookie(login.state), oidc.seal(login), max_age=PENDING_SECONDS, httponly=True, secure=settings.secureCookies,
                            samesite='lax', path=PENDING_PATH)
        return response

    @router.get(CALLBACK_PATH.removeprefix('/api'))
    def callback(request: Request, session: Session = Depends(sessions), code: Optional[str] = None, state: Optional[str] = None,
                 error: Optional[str] = None) -> Response:
        if oidc is None:
            raise HTTPException(status_code=404, detail='single sign-on is off')
        address = request.client.host if request.client is not None else ''
        cookie = pendingCookie(state) if state else None
        pending = oidc.unseal(request.cookies.get(cookie)) if cookie else None
        try:
            # Failures are counted by address: each that gets as far as the
            # provider costs a request to its token endpoint.
            if not throttle.allowed('', address):
                raise SsoFailure(BUSY, 'too many failed sign-ins from {}'.format(address))
            if pending is None or state is None or not hmac.compare_digest(state.encode('utf-8', 'replace'), pending.state.encode('ascii')) \
                    or not oidc.takeState(pending.state):
                raise SsoFailure(STATE, 'the sign-in came back without the state it left with: expired, used, started in another browser, '
                                        'or forged')
            if error is not None or not code:
                raise SsoFailure(PROVIDER, 'the identity provider answered {!r} rather than a code'.format((error or '')[:PROVIDER_ERROR_LENGTH]))
            claims = oidc.claimsFor(code, pending)
            with changingAccounts:
                user = signInWith(session, settings, claims)
                # An account made just now has its id once it is written.
                session.flush()
                record(session, user, 'signed in', method='sso')
                response = _onward('/')
                startSession(session, user, response, oidc.settings.sessionHours, settings.secureCookies, request)
        except SsoFailure as failure:
            session.rollback()
            logger.warning('A single sign-on was refused: {}'.format(failure))
            if failure.code in (STATE, PROVIDER, TOKEN):
                throttle.failed('', address)
            if failure.who:
                record(session, None, 'refused a single sign-on', failure.who, reason=failure.code)
                session.commit()
            response = _onward('/?sso_error={}'.format(failure.code))
        except Exception as unexpected:
            session.rollback()
            logger.error('A single sign-on failed: {}'.format(describeError(unexpected)))
            response = _onward('/?sso_error=unexpected')
        if cookie:
            response.delete_cookie(cookie, path=PENDING_PATH, secure=settings.secureCookies, httponly=True, samesite='lax')
        return response

    return router

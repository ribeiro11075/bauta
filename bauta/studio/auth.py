"""Signing in: passwords hashed with argon2, a session per browser held in
an HttpOnly cookie, and the role each request needs.

A request that changes anything must carry the X-Bauta-Studio header. A
browser sends a custom header cross-site only after a CORS preflight, which
Studio never answers, so another site can't make a signed-in browser act.
"""
from __future__ import annotations

import datetime
import hashlib
import secrets
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Activity, User, UserSession, utcnow

SESSION_COOKIE = 'bauta_studio_session'
CSRF_HEADER = 'X-Bauta-Studio'
SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
MINIMUM_PASSWORD_LENGTH = 12
# Failed sign-ins allowed in the window before more are refused: for an email
# from one address, which is what a person mistyping hits; from one address
# across every email, which stops trying many accounts; and for one email
# across every address, which stops many machines trying one account while
# leaving it too high for a stranger to lock its owner out cheaply.
LOGIN_ATTEMPTS = 5
LOGIN_ATTEMPTS_PER_ADDRESS = 30
LOGIN_ATTEMPTS_PER_EMAIL = 100
LOGIN_WINDOW_SECONDS = 300
# Each check takes argon2's 64 MiB and tens of milliseconds, and needs no
# account to ask for, so only so many run at once; past the wait, it is refused.
PASSWORD_CHECKS_AT_ONCE = 4
PASSWORD_CHECK_WAIT_SECONDS = 10.0

hasher = PasswordHasher()
# Checked against when an email is unknown, so a wrong email takes as long as
# a wrong password and the time says nothing about who has an account.
_DUMMY_HASH = hasher.hash(secrets.token_urlsafe(16))
_passwordChecks = threading.BoundedSemaphore(PASSWORD_CHECKS_AT_ONCE)


class PasswordCheckBusy(Exception):
    """Too many sign-ins are being checked at once to check another."""


def hashPassword(password: str) -> str:

    if len(password) < MINIMUM_PASSWORD_LENGTH:
        raise ValueError('a password must be at least {} characters'.format(MINIMUM_PASSWORD_LENGTH))

    return hasher.hash(password)


def checkPassword(user: Optional[User], password: str) -> bool:

    if not _passwordChecks.acquire(timeout=PASSWORD_CHECK_WAIT_SECONDS):
        raise PasswordCheckBusy('too many sign-ins at once; try again shortly')
    try:
        return hasher.verify(user.passwordHash if user is not None else _DUMMY_HASH, password) and user is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    finally:
        _passwordChecks.release()


def _tokenHash(token: str) -> str:

    return hashlib.sha256(token.encode('ascii')).hexdigest()


class LoginThrottle:
    """Counts failed sign-ins by email and address together, by address, and
    by email: see LOGIN_ATTEMPTS. An email alone was the whole key, so anyone
    could keep an admin locked out with one request a minute. Held in memory:
    Studio runs as one process. Only a failure is kept, and only for the
    window, so what it holds can't grow with who asks.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._failures: Dict[Tuple[str, ...], List[float]] = {}
        self._lock = threading.Lock()
        self._clock = clock
        self._swept = clock()


    @staticmethod
    def _limits(email: str, address: str) -> List[Tuple[Tuple[str, ...], int]]:

        return [(('both', email, address), LOGIN_ATTEMPTS), (('address', address), LOGIN_ATTEMPTS_PER_ADDRESS), (('email', email), LOGIN_ATTEMPTS_PER_EMAIL)]


    def _recent(self, key: Tuple[str, ...]) -> int:
        """How many of `key`'s failures are in the window, forgetting the rest."""

        now = self._clock()
        recent = [at for at in self._failures.get(key, []) if now - at < LOGIN_WINDOW_SECONDS]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)

        return len(recent)


    def _sweep(self) -> None:
        """Forgets keys nobody has come back to, once a window."""

        now = self._clock()
        if now - self._swept >= LOGIN_WINDOW_SECONDS:
            self._swept = now
            for key in list(self._failures):
                self._recent(key)


    def allowed(self, email: str, address: str) -> bool:

        with self._lock:
            return all(self._recent(key) < limit for key, limit in self._limits(email, address))


    def failed(self, email: str, address: str) -> None:

        with self._lock:
            self._sweep()
            for key, _ in self._limits(email, address):
                self._failures.setdefault(key, []).append(self._clock())


    def succeeded(self, email: str, address: str) -> None:
        """Clears only what this person's own typing counted: signing in to
        one account must not reset what an address has tried against others.
        """

        with self._lock:
            self._failures.pop(self._limits(email, address)[0][0], None)


def startSession(session: Session, user: User, response: Response, hours: int, secure: bool) -> None:

    token = secrets.token_urlsafe(32)
    session.add(UserSession(tokenHash=_tokenHash(token), userId=user.id, expiresAt=utcnow() + datetime.timedelta(hours=hours)))
    user.lastLoginAt = utcnow()
    session.commit()
    response.set_cookie(SESSION_COOKIE, token, max_age=hours * 3600, httponly=True, secure=secure, samesite='strict', path='/')


def endSession(session: Session, request: Request, response: Response) -> None:

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        found = session.get(UserSession, _tokenHash(token))
        if found is not None:
            session.delete(found)
            session.commit()
    response.delete_cookie(SESSION_COOKIE, path='/')


def endSessionsOf(session: Session, user: User) -> None:
    """Signs a user out everywhere: after a password change, or deactivation."""

    for found in session.scalars(select(UserSession).where(UserSession.userId == user.id)):
        session.delete(found)


def userFromRequest(session: Session, request: Request) -> Optional[User]:

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None

    found = session.get(UserSession, _tokenHash(token))
    if found is None:
        return None
    expires = found.expiresAt if found.expiresAt.tzinfo else found.expiresAt.replace(tzinfo=datetime.timezone.utc)
    if expires < utcnow():
        session.delete(found)
        session.commit()
        return None

    user = session.get(User, found.userId)

    return user if user is not None and user.active else None


def record(session: Session, user: Optional[User], action: str, target: str = '', **detail: object) -> None:
    """Adds an Activity for the caller to commit with whatever it changed."""

    session.add(Activity(userId=user.id if user else None, email=user.email if user else '', action=action, target=target, detail=detail))


def requireRole(role: str, sessions: Callable[..., object]) -> Callable[..., User]:
    """A dependency: the signed-in user, who must have `role`, on a request
    that, if it changes anything, carries the CSRF header.
    """

    def dependency(request: Request, session: Session = Depends(sessions)) -> User:
        if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != '1':
            raise HTTPException(status_code=403, detail='missing the {} header'.format(CSRF_HEADER))
        user = userFromRequest(session, request)
        if user is None:
            raise HTTPException(status_code=401, detail='sign in first')
        if not user.can(role):
            raise HTTPException(status_code=403, detail='this needs the {} role'.format(role))
        return user

    return dependency

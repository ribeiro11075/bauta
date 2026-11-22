"""Two-factor sign-in with an authenticator app: a time-based one-time code
(TOTP, RFC 6238) after a right password, for the accounts that sign in with
one. Signing in through the identity provider never asks for it: the provider
asks for its own second factor.

A person sets it up by scanning a QR code and typing the code their app then
shows: it is on only once a code proves the app holds the secret. They get
ten one-time recovery codes for when they lose the app; an admin can also
turn it off for them, after which they set it up again.

Each secret is kept encrypted with a key held apart from Studio's database,
so a copy of the database, or a backup of it, isn't enough to make codes. A
code is taken once: each account keeps the last 30-second step it took one
for.

A password that needs a code next doesn't sign in yet: it gets a short-lived
cookie, signed by Studio, naming the account and what it must do next, which
only the endpoints under /api/auth/2fa read.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pyotp
import segno
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from .db import ADMIN, RecoveryCode, User
from .sealing import seal, unseal
from .settings import Settings, TwoFactorPolicy

# DRIFT_STEPS: a code from the step before or after the current one is taken
#   too: phones' clocks drift, and a code typed as it changes arrives in the
#   next step.
# RECOVERY_CODE_LENGTH: ten characters of base32: 50 bits, grouped in fives to
#   be read aloud.
# PENDING_COOKIE, PENDING_PATH, PENDING_SECONDS: the cookie a password that
#   needs a code next is given, and how long it lasts.
# CODE: what a pending sign-in must do next: type a code, or set the app up
#   first, for someone whose role needs two-factor sign-in and who hasn't yet.
ISSUER_NAME = 'bauta Studio'
STEP_SECONDS = 30
DRIFT_STEPS = 1
CODE_DIGITS = 6
RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_LENGTH = 10
RECOVERY_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
PENDING_COOKIE = 'bauta_studio_2fa'
PENDING_PATH = '/api/auth/2fa'
PENDING_SECONDS = 300
CODE = 'code'
ENROL = 'enrol'
KEY_FILE_MODE = 0o600


class SecretKeyProblem(Exception):
    """The key secrets are encrypted with can't be read, or isn't the one
    they were encrypted with.
    """


class SecretBox:
    """Encrypts authenticator secrets with Fernet (AES-128-CBC with an
    HMAC-SHA256), and signs pending sign-ins with a key derived from the same
    file. The file is made the first time, readable by its owner alone.
    """

    def __init__(self, key: bytes) -> None:
        """Encrypts with `key` under Fernet, and derives from it the key
        pending sign-ins are signed with.
        """

        self._fernet = Fernet(key)
        self.signingKey = hmac.new(key, b'bauta-studio pending two-factor sign-in', hashlib.sha256).digest()


    @classmethod
    def load(cls, path: Path) -> 'SecretBox':
        """The box for the key file at `path`, making the file, readable by its
        owner alone, if it isn't there. Raises SecretKeyProblem for a key that
        can't be used.
        """

        if not path.exists():
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, KEY_FILE_MODE)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, 'wb') as file:
                    file.write(Fernet.generate_key() + b'\n')
        try:
            key = path.read_bytes().strip()
            return cls(key)
        except (OSError, ValueError) as error:
            raise SecretKeyProblem('the secret key in {} can\'t be used: {}'.format(path, error)) from error


    def encrypt(self, text: str) -> str:
        """`text` encrypted."""

        return self._fernet.encrypt(text.encode()).decode('ascii')


    def decrypt(self, token: str) -> str:
        """A token decrypted. Raises SecretKeyProblem where it doesn't decrypt
        with this key, as when the key file was replaced.
        """

        try:
            return self._fernet.decrypt(token.encode('ascii')).decode()
        except InvalidToken as error:
            raise SecretKeyProblem('an authenticator secret doesn\'t decrypt with this secret key: was the key file replaced? '
                                   'An admin can reset two-factor sign-in for the account') from error


def required(settings: Settings, user: User) -> bool:
    """Whether `user`'s sign-in with a password needs a code."""

    return settings.requireTwoFactor == TwoFactorPolicy.ALL or (settings.requireTwoFactor == TwoFactorPolicy.ADMINS and user.role == ADMIN)


def newSecret() -> str:
    """A new authenticator secret, in base32."""

    return pyotp.random_base32()


def setupDetails(secret: str, email: str) -> Dict[str, str]:
    """What an app is set up from: the secret, to type, and the same as an
    otpauth URI and a QR code of it, to scan.
    """

    uri = pyotp.TOTP(secret, digits=CODE_DIGITS, interval=STEP_SECONDS).provisioning_uri(name=email, issuer_name=ISSUER_NAME)

    return {'secret': secret, 'uri': uri, 'qr': segno.make(uri, error='m').svg_data_uri(scale=5, border=2)}


def _digits(code: str) -> str:
    """A typed code without the spaces people put in it."""

    return ''.join(character for character in code if not character.isspace())


def acceptCode(user: User, secret: str, code: str, now: Optional[float] = None) -> bool:
    """Whether `code` is the app's for a step around now that no code was
    taken for yet; if so, that step is the last taken.
    """

    code = _digits(code)
    if len(code) != CODE_DIGITS or not code.isdigit():
        return False
    totp = pyotp.TOTP(secret, digits=CODE_DIGITS, interval=STEP_SECONDS)
    current = int((time.time() if now is None else now) // STEP_SECONDS)
    for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
        if (user.totpLastStep is None or step > user.totpLastStep) and hmac.compare_digest(totp.at(step * STEP_SECONDS), code):
            user.totpLastStep = step
            return True

    return False


def _recoveryHash(code: str) -> str:
    """What a recovery code is kept as: its SHA-256."""

    return hashlib.sha256(code.encode('ascii')).hexdigest()


def _normalRecovery(code: str) -> str:
    """A typed recovery code in the form it was made in: upper case, separators
    and anything outside its alphabet dropped.
    """

    return ''.join(character for character in code.upper() if character in RECOVERY_ALPHABET)


def newRecoveryCodes(user: User) -> List[str]:
    """Replaces `user`'s recovery codes, returning the new ones: the only time
    anyone sees them.
    """

    codes = [''.join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH)) for _ in range(RECOVERY_CODE_COUNT)]
    user.recoveryCodes = [RecoveryCode(codeHash=_recoveryHash(code)) for code in codes]
    half = RECOVERY_CODE_LENGTH // 2

    return ['{}-{}'.format(code[:half], code[half:]) for code in codes]


def useRecoveryCode(session: Session, user: User, code: str) -> bool:
    """Whether `code` is one of `user`'s recovery codes; if so, it is used up."""

    code = _normalRecovery(code)
    if len(code) != RECOVERY_CODE_LENGTH:
        return False
    found = session.get(RecoveryCode, _recoveryHash(code))
    if found is None or found.userId != user.id:
        return False
    user.recoveryCodes.remove(found)

    return True


def checkSecondFactor(session: Session, box: SecretBox, user: User, code: str) -> Optional[str]:
    """How `code` proves `user` has their second factor -- 'authenticator' or
    'recovery code' -- or None if it doesn't.
    """

    if user.totpSecret is None:
        return None
    if acceptCode(user, box.decrypt(user.totpSecret), code):
        return 'authenticator'
    if useRecoveryCode(session, user, code):
        return 'recovery code'

    return None


def turnOff(user: User) -> None:
    """Removes the account's authenticator, pending setup and recovery codes."""

    user.totpSecret = user.totpPendingSecret = user.totpLastStep = None
    user.recoveryCodes = []


def _passwordTag(user: User) -> str:
    """A tag of `user`'s password as it is now: a pending sign-in made before
    the password changed is no longer good.
    """

    return hashlib.sha256((user.passwordHash or '').encode()).hexdigest()[:16]


def sealPending(box: SecretBox, user: User, next: str) -> str:
    """The pending sign-in's cookie for `user`, whose next step is `next`, tied
    to the password it was given with, so changing the password voids it.
    """

    return seal(box.signingKey, {'u': user.id, 'n': next, 'p': _passwordTag(user)}, time.time() + PENDING_SECONDS)


def openPending(box: SecretBox, session: Session, value: Optional[str]) -> Optional[Tuple[User, str]]:
    """The account a pending sign-in is for and what it must do next, if
    Studio sealed it, it hasn't expired, the account is still active, and its
    password hasn't changed since.
    """

    data: Optional[Dict[str, Any]] = unseal(box.signingKey, value, time.time())
    if data is None or data.get('n') not in (CODE, ENROL) or not isinstance(data.get('u'), int):
        return None
    user = session.get(User, data['u'])
    if user is None or not user.active or not hmac.compare_digest(str(data.get('p', '')), _passwordTag(user)):
        return None

    return user, data['n']

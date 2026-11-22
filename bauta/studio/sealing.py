"""Small values a browser holds for Studio and gives back, signed so that only
Studio could have made them, and good for so long: a sign-in on its way
through the identity provider, or one waiting for an authenticator's code.
They are signed, not encrypted: nothing in one is a secret from the browser
holding it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Dict, Optional


def b64(data: bytes) -> str:
    """Bytes as unpadded URL-safe base64."""

    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _unb64(text: str) -> bytes:
    """Unpadded URL-safe base64 back to bytes."""

    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _signature(key: bytes, payload: str) -> str:
    """The HMAC-SHA256 of a sealed payload, as base64."""

    return b64(hmac.new(key, payload.encode('ascii', 'replace'), hashlib.sha256).digest())


def seal(key: bytes, data: Dict[str, Any], expires: float) -> str:
    """`data`, good until `expires`, a time.time(), signed with `key`."""

    payload = b64(json.dumps({'d': data, 'e': int(expires)}).encode())

    return payload + '.' + _signature(key, payload)


def unseal(key: bytes, value: Optional[str], now: float) -> Optional[Dict[str, Any]]:
    """What `value` holds, if `key` signed it and it hasn't expired by `now`."""

    if not value or value.count('.') != 1:
        return None
    payload, signature = value.split('.')
    if not hmac.compare_digest(signature.encode('ascii', 'replace'), _signature(key, payload).encode('ascii')):
        return None
    try:
        sealed = json.loads(_unb64(payload))
        data, expires = sealed['d'], int(sealed['e'])
    except (ValueError, KeyError, TypeError):
        return None

    return data if isinstance(data, dict) and now < expires else None

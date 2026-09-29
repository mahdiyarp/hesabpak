# -*- coding: utf-8 -*-
"""Application-secret encryption helpers.

Secrets are encrypted at rest with a Fernet key derived from a stable
credential key. A separate environment variable is preferred so rotating the
Flask session key does not invalidate stored AI credentials.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


PREFIX = "fernet:v1:"


def _fernet(app):
    explicit = (app.config.get("CREDENTIAL_ENCRYPTION_KEY") or "").strip()
    source = explicit or (app.config.get("SECRET_KEY") or os.environ.get("SECRET_KEY", ""))
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def is_encrypted(value: str) -> bool:
    return str(value or "").startswith(PREFIX)


def encrypt_secret(app, value: str | None) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    if is_encrypted(raw):
        return raw
    token = _fernet(app).encrypt(raw.encode("utf-8")).decode("ascii")
    return PREFIX + token


def decrypt_secret(app, value: str | None) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    if not is_encrypted(raw):
        # Backward compatibility for one-time migration of legacy plaintext.
        return raw
    try:
        token = raw[len(PREFIX):]
        return _fernet(app).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return ""

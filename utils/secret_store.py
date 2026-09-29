# -*- coding: utf-8 -*-
"""Small at-rest encryption layer for sensitive application credentials."""
from __future__ import annotations

import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "fernet:v1:"


def _fernet(app) -> Fernet:
    source = (
        (app.config.get("CREDENTIAL_ENCRYPTION_KEY") or "").strip()
        or (app.config.get("SECRET_KEY") or "").strip()
    )
    if not source:
        raise RuntimeError("کلید رمزنگاری اعتبارنامه‌ها تنظیم نشده است.")
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def is_encrypted(value: Optional[str]) -> bool:
    return str(value or "").startswith(PREFIX)


def encrypt_secret(app, value: Optional[str]) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    if is_encrypted(raw):
        return raw
    token = _fernet(app).encrypt(raw.encode("utf-8")).decode("ascii")
    return PREFIX + token


def decrypt_secret(app, value: Optional[str]) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    if not is_encrypted(raw):
        # Legacy plaintext values remain readable so installations can migrate
        # without losing access. New writes are always encrypted.
        return raw
    try:
        token = raw[len(PREFIX):]
        return _fernet(app).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return ""

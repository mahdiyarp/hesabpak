# -*- coding: utf-8 -*-
"""Small SMTP helper used for user-requested backup delivery.

SMTP credentials stay in the environment; no mail password is persisted in the
application database.
"""
from __future__ import annotations

import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path


_EMAIL_RE = re.compile(r"^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$")


def _as_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def send_backup_email(app, recipient: str, attachment_path: str, reason: str = "manual"):
    """Send a backup ZIP as an email attachment.

    Configuration:
      SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD,
      SMTP_FROM (defaults to SMTP_USER), SMTP_USE_TLS (default true),
      SMTP_TIMEOUT (default 20).
    """
    recipient = (recipient or "").strip()
    if not recipient or not _EMAIL_RE.match(recipient):
        raise ValueError("نشانی ایمیل مقصد معتبر نیست.")

    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        raise RuntimeError("SMTP_HOST تنظیم نشده است.")

    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = (os.environ.get("SMTP_FROM", "") or username or recipient).strip()
    use_tls = _as_bool(os.environ.get("SMTP_USE_TLS", "true"), default=True)
    timeout = int(os.environ.get("SMTP_TIMEOUT", "20"))

    path = Path(attachment_path)
    if not path.is_file():
        raise FileNotFoundError("فایل بکاپ برای ارسال پیدا نشد.")

    subject = f"حساب پاک — نسخه پشتیبان {path.name}"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(
        "نسخه پشتیبان حساب پاک به پیوست ارسال شد.\\n"
        f"علت ایجاد: {reason or 'manual'}\\n"
        f"نام فایل: {path.name}"
    )
    message.add_attachment(
        path.read_bytes(),
        maintype="application",
        subtype="zip",
        filename=path.name,
    )

    smtp = smtplib.SMTP(host, port, timeout=timeout)
    try:
        smtp.ehlo()
        if use_tls:
            smtp.starttls()
            smtp.ehlo()
        if username:
            smtp.login(username, password)
        smtp.send_message(message)
    finally:
        try:
            smtp.quit()
        except Exception:
            smtp.close()

    app.logger.info("backup email sent recipient=%s file=%s", recipient, path.name)

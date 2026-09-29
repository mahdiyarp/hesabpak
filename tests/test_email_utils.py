from pathlib import Path

import pytest

import utils.email_utils as email_utils


class FakeSMTP:
    sent = None

    def __init__(self, host, port, timeout):
        assert host == "smtp.example.test"
        assert port == 587
        assert timeout == 20

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, username, password):
        assert username == "user"
        assert password == "secret"

    def send_message(self, message):
        FakeSMTP.sent = message

    def quit(self):
        pass

    def close(self):
        pass


def test_send_backup_email(monkeypatch, tmp_path):
    class Logger:
        def info(self, *args, **kwargs):
            pass

    class App:
        logger = Logger()

    attachment = Path(tmp_path) / "backup_2026-09-29_12-00-00_deadbeef.zip"
    attachment.write_bytes(b"zip-content")

    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "user")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.test")
    monkeypatch.setenv("SMTP_USE_TLS", "true")
    monkeypatch.setattr(email_utils.smtplib, "SMTP", FakeSMTP)

    email_utils.send_backup_email(
        App(),
        "owner@example.test",
        str(attachment),
        reason="manual",
    )

    assert FakeSMTP.sent["To"] == "owner@example.test"
    assert FakeSMTP.sent["From"] == "noreply@example.test"
    assert FakeSMTP.sent.get_payload()[1].get_filename() == attachment.name


def test_send_backup_email_rejects_bad_recipient(monkeypatch, tmp_path):
    attachment = Path(tmp_path) / "backup_test.zip"
    attachment.write_bytes(b"x")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    with pytest.raises(ValueError):
        email_utils.send_backup_email(None, "not-an-email", str(attachment))

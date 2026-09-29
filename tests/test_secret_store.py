from flask import Flask

from utils.secret_store import decrypt_secret, encrypt_secret, is_encrypted


def test_secret_store_roundtrip():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "session-secret"
    app.config["CREDENTIAL_ENCRYPTION_KEY"] = "stable-credential-secret"
    token = encrypt_secret(app, "sk-sensitive-value")
    assert is_encrypted(token)
    assert token != "sk-sensitive-value"
    assert decrypt_secret(app, token) == "sk-sensitive-value"


def test_secret_store_legacy_plaintext_migration_read():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "session-secret"
    app.config["CREDENTIAL_ENCRYPTION_KEY"] = "stable-credential-secret"
    assert decrypt_secret(app, "legacy-value") == "legacy-value"

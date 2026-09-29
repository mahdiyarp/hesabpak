import os
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="hesabpak-test-data-"))

import app as app_module


def test_numeric_parser_accepts_persian_and_arabic_digits():
    assert app_module._to_float("۱٬۲۳۴٬۵۶۷") == 1234567
    assert app_module._to_float("١،٢٣٤٬٥٦٧") == 1234567
    assert app_module._to_float("۱٫۵") == 1.5
    assert app_module._to_float("1,234.50") == 1234.5


def test_password_hashing_and_legacy_upgrade():
    plain = "Strong-Test-Password"
    assert not app_module._password_is_hashed(plain)

    hashed = app_module.generate_password_hash(plain)
    assert app_module._password_is_hashed(hashed)
    assert app_module.check_password_hash(hashed, plain)
    assert not app_module.check_password_hash(hashed, "wrong")


def test_record_ledger_creates_one_valid_entry():
    with app_module.app.app_context():
        app_module.db.drop_all()
        app_module.db.create_all()
        entry = app_module.record_ledger(
            "test",
            "1",
            "create",
            {"amount": 1250, "note": "integrity"},
        )
        assert entry.id is not None
        assert entry.hash
        assert entry.prev_hash is None
        assert entry.payload == '{"amount": 1250, "note": "integrity"}'
        stored = app_module.LedgerEntry.query.get(entry.id)
        assert stored is not None
        assert stored.hash == entry.hash
def test_search_permissions_scope_targets():
    assert app_module._search_targets_for_permissions(
        {"item", "person", "invoice", "receive", "payment"},
        {"dashboard", "entities"},
        admin=False,
    ) == {"item", "person"}

    assert app_module._search_targets_for_permissions(
        {"invoice", "receive", "payment"},
        {"dashboard", "sales", "reports"},
        admin=False,
    ) == {"invoice", "receive", "payment"}

    assert app_module._search_targets_for_permissions(
        {"item", "person", "invoice", "receive", "payment"},
        {"dashboard"},
        admin=False,
    ) == set()

    assert app_module._search_targets_for_permissions(
        {"item", "person", "invoice", "receive", "payment"},
        set(),
        admin=True,
    ) == {"item", "person", "invoice", "receive", "payment"}

from utils.bank_utils import (
    detect_bank,
    detect_bank_from_bin,
    detect_bank_from_iban,
    validate_card,
    validate_iban,
)


def test_bank_card_validation_and_bin_detection():
    valid_card = "6037991234567893"
    assert validate_card(valid_card) is True
    assert validate_card("6037991234567894") is False
    info = detect_bank(valid_card)
    assert info["type"] == "card"
    assert info["valid"] is True
    assert info["bin"] == "603799"
    assert info["bank"]["bank"] == "بانک ملی ایران"


def test_sheba_validation_and_bank_code_detection():
    valid_iban = "IR270171234567890123456789"
    assert validate_iban(valid_iban) is True
    assert validate_iban(valid_iban[:-1] + "0") is False
    info = detect_bank(valid_iban)
    assert info["type"] == "shaba"
    assert info["valid"] is True
    assert info["bank"]["code"] == "017"
    assert info["bank"]["bank"] == "بانک ملی ایران"
    assert detect_bank_from_iban(valid_iban)["code"] == "017"


def test_bank_detection_normalizes_persian_digits():
    assert detect_bank_from_bin("۶۰۳۷۹۹")["bank"] == "بانک ملی ایران"
    assert detect_bank("۶۰۳۷۹۹۱۲۳۴۵۶۷۸۹۳")["valid"] is True

from utils.rates import _parse_number


def test_rate_parser_accepts_persian_and_arabic_digits():
    assert _parse_number("۱٬۲۳۴٬۵۶۷") == 1234567
    assert _parse_number("١،٢٣٤٫٥") == 1234.5
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


def test_secret_store_keeps_legacy_plaintext_readable_for_migration():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "session-secret"
    app.config["CREDENTIAL_ENCRYPTION_KEY"] = "stable-credential-secret"
    assert decrypt_secret(app, "legacy-value") == "legacy-value"

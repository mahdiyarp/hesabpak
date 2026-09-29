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

import os
import json
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


def test_reports_fallback_escapes_query_and_date_values(monkeypatch):
    def noop_permission(*args, **kwargs):
        return None

    def fake_render(template_name, **kwargs):
        if template_name == "reports.html":
            raise RuntimeError("force fallback")
        return kwargs["content"]

    monkeypatch.setattr(app_module, "ensure_permission", noop_permission)
    monkeypatch.setattr(app_module, "render_template", fake_render)

    malicious_q = "<img src=x onerror=alert(1)>"
    malicious_from = "'><script>alert(1)</script>"
    malicious_to = "\" onfocus=\"alert(2)"

    with app_module.app.test_request_context(
        "/reports",
        query_string={"q": malicious_q, "from": malicious_from, "to": malicious_to},
    ):
        rendered = str(app_module.reports())

    assert malicious_q not in rendered
    assert malicious_from not in rendered
    assert malicious_to not in rendered
    assert "&lt;img" in rendered
    assert "&lt;script&gt;" in rendered


def test_record_ledger_serializes_concurrent_appends():
    from concurrent.futures import ThreadPoolExecutor

    with app_module.app.app_context():
        app_module.db.drop_all()
        app_module.db.create_all()

    def write_entry(index):
        with app_module.app.app_context():
            entry = app_module.record_ledger(
                "concurrency",
                str(index),
                "create",
                {"index": index},
            )
            return entry.id, entry.hash, entry.prev_hash

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write_entry, (1, 2)))

    ordered = sorted(results, key=lambda row: row[0])
    assert ordered[0][2] is None
    assert ordered[1][2] == ordered[0][1]


def test_security_headers_are_applied():
    with app_module.app.test_request_context("/", base_url="https://example.test"):
        response = app_module.app.make_response("ok")
        response = app_module._security_headers(response)

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"
    assert "max-age=31536000" in response.headers["Strict-Transport-Security"]


def test_security_headers_skip_hsts_on_http():
    with app_module.app.test_request_context("/", base_url="http://example.test"):
        response = app_module.app.make_response("ok")
        response = app_module._security_headers(response)

    assert "Strict-Transport-Security" not in response.headers


def test_assistant_upload_rejects_non_image_without_touching_disk(monkeypatch):
    monkeypatch.setattr(app_module, "ensure_permission", lambda *args, **kwargs: None)
    with app_module.app.test_request_context(
        "/assistant/api/parse",
        method="POST",
        data={"image": (b"not an image", "note.txt", "text/plain")},
        content_type="multipart/form-data",
    ):
        response = app_module.assistant_parse()

    assert response[1] == 400


def test_security_headers_include_compatible_csp():
    with app_module.app.test_request_context("/", base_url="https://example.test"):
        response = app_module.app.make_response("ok")
        response = app_module._security_headers(response)

    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "https://cdn.jsdelivr.net" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp


def test_logout_route_requires_post():
    rules = app_module.app.url_map.iter_rules()
    logout_rules = [r for r in rules if r.endpoint == "logout"]
    assert logout_rules
    assert all("POST" in r.methods and "GET" not in r.methods for r in logout_rules)


def test_payment_route_is_owned_by_unified_cash_only():
    rules = [
        r for r in app_module.app.url_map.iter_rules()
        if r.rule == "/payment"
    ]
    assert len(rules) == 1
    assert rules[0].endpoint == "unified_cash"


def test_csv_export_cells_are_safe_for_spreadsheet_formulas():
    assert app_module._csv_safe_cell("=HYPERLINK(\"http://evil.test\", \"click\")").startswith("'=")
    assert app_module._csv_safe_cell("  +SUM(A1)").startswith("'")
    assert app_module._csv_safe_cell("normal text") == "normal text"


def test_last_active_admin_cannot_be_removed_or_demoted():
    catalog = {
        "admin": {"role": "admin", "is_active": True},
    }
    assert app_module._would_remove_last_active_admin(
        catalog, "admin", deleting=True
    ) is True
    assert app_module._would_remove_last_active_admin(
        catalog, "admin", new_role="staff", new_is_active=True
    ) is True
    assert app_module._would_remove_last_active_admin(
        {
            "admin": {"role": "admin", "is_active": True},
            "second": {"role": "admin", "is_active": True},
        },
        "admin",
        new_role="staff",
        new_is_active=True,
    ) is False


def test_inactive_admin_is_not_counted_as_active_admin():
    catalog = {
        "admin": {"role": "admin", "is_active": False},
        "second": {"role": "staff", "is_active": True},
    }
    assert app_module._active_admin_usernames(catalog) == set()


def test_save_users_catalog_writes_atomically(monkeypatch, tmp_path):
    target = tmp_path / "users.json"
    monkeypatch.setattr(app_module, "USERS_FILE", str(target))
    app_module.save_users_catalog({
        "admin": {
            "password": app_module.generate_password_hash("secret"),
            "role": "admin",
            "permissions": app_module.ADMIN_PERMISSIONS,
            "is_active": True,
            "email": "admin@example.test",
        }
    })
    assert target.is_file()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["users"][0]["username"] == "admin"
    assert app_module._password_is_hashed(data["users"][0]["password"])


def test_audit_log_payload_is_size_limited(monkeypatch):
    monkeypatch.setattr(app_module, "ensure_permission", lambda *args, **kwargs: None)
    from types import SimpleNamespace
    monkeypatch.setattr(
        app_module,
        "current_user",
        SimpleNamespace(username="tester"),
    )
    huge = "x" * 40000
    with app_module.app.test_request_context(
        "/api/audit/log",
        method="POST",
        json={"context": "test", "action": "write", "payload": huge},
    ):
        response = app_module.api_audit_log()

    assert response.status_code == 413

def test_legacy_receive_route_is_get_only_compat_redirect():
    rules = [r for r in app_module.app.url_map.iter_rules() if r.rule == "/receive_old"]
    assert len(rules) == 1
    assert "GET" in rules[0].methods
    assert "POST" not in rules[0].methods

def test_rates_save_is_atomic_and_requires_object(tmp_path, monkeypatch):
    import pytest
    import utils.rates as rates

    target = tmp_path / "rates.json"
    monkeypatch.setattr(rates, "DATA_FILE", target)

    rates.save_rates({"updated_at": None})
    assert target.read_text(encoding="utf-8").strip().startswith("{")
    assert not list(tmp_path.glob(".rates-*.json.tmp"))

    with pytest.raises(ValueError):
        rates.save_rates(["invalid"])


def test_rates_updater_interval_is_bounded(monkeypatch):
    import utils.rates as rates

    observed = {}

    class FakeThread:
        def __init__(self, *args, **kwargs):
            observed["args"] = args
            observed["kwargs"] = kwargs
        def start(self):
            pass
        def is_alive(self):
            return False

    monkeypatch.setattr(rates.threading, "Thread", FakeThread)
    monkeypatch.setattr(rates, "fetch_and_update", lambda save=True: {})
    rates._updater_thread = None
    rates._stop_event = None

    rates.start_background_updater(interval_seconds=-100, run_on_start=False)
    assert observed["args"][1] == (10,)
    assert rates._stop_event is not None

    rates._stop_event = None
    rates._updater_thread = None

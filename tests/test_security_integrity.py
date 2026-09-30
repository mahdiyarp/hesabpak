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
    assert "https://cdn.jsdelivr.net" not in csp
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


def test_cash_edit_method_options_are_not_mutable():
    source = Path(app_module.__file__).read_text(encoding="utf-8")
    assert "روش سند هنگام ویرایش قابل تغییر نیست" in source
    assert 'value="{escape(CASH_METHOD_LABELS.get(current_method, \'نامشخص\'))}" disabled' in source

def test_rate_snapshot_rejects_negative_and_non_numeric_values(monkeypatch, tmp_path):
    import pytest
    import utils.rates as rates
    monkeypatch.setattr(rates, "DATA_FILE", tmp_path / "rates.json")

    with pytest.raises(ValueError):
        rates.save_rates({"currencies": {"USD": {"rate": -1, "unit": "تومان"}}})
    with pytest.raises(ValueError):
        rates.save_rates({"currencies": {"USD": {"rate": "bad", "unit": "تومان"}}})
    with pytest.raises(ValueError):
        rates.save_rates({"gold": []})

def test_rate_snapshot_normalizes_currency_codes_and_units(monkeypatch, tmp_path):
    import utils.rates as rates
    target = tmp_path / "rates.json"
    monkeypatch.setattr(rates, "DATA_FILE", target)
    rates.save_rates({"updated_at": "x", "currencies": {"usd": {"rate": "123", "unit": "تومان"}}})
    snapshot = rates.load_rates()
    assert snapshot["currencies"]["USD"]["rate"] == 123.0

def test_developer_delete_respects_entity_reference_protection():
    with app_module.app.app_context():
        app_module.db.drop_all()
        app_module.db.create_all()

        person = app_module.Entity(
            type="person",
            code="101",
            name="مشتری",
            level=1,
        )
        app_module.db.session.add(person)
        app_module.db.session.commit()

        cash = app_module.CashDoc(
            doc_type="receive",
            number="R-TEST-1",
            date=app_module.datetime.utcnow().date(),
            person_id=person.id,
            amount=1000,
            method="cash",
        )
        app_module.db.session.add(cash)
        app_module.db.session.commit()

        messages = app_module._run_script_lines(["DELETE person 101"])

        assert any("حذف فیزیکی مجاز نیست" in message for message in messages)
        assert app_module.Entity.query.get(person.id) is not None

def test_sensitive_query_values_are_redacted_from_request_logs():
    with app_module.app.test_request_context(
        "/test?token=secret-token&api_key=secret-key&name=Mahdi&authorization=bearer-secret"
    ):
        safe = app_module._safe_request_args_for_log()
    assert safe["token"] == "***"
    assert safe["api_key"] == "***"
    assert safe["authorization"] == "***"
    assert safe["name"] == "Mahdi"

def test_global_search_actions_do_not_use_dynamic_html_sink():
    source = (Path(app_module.__file__).resolve().parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "quickLinks.map" not in source
    assert "actions.innerHTML = quickLinks" not in source

def test_unified_form_routes_authorize_posted_invoice_and_cash_kinds(monkeypatch):
    observed = []
    monkeypatch.setattr(app_module, "ensure_permission", lambda perm: observed.append(perm))
    monkeypatch.setattr(app_module, "parse_gregorian_date", lambda *args, **kwargs: None)

    with app_module.app.test_request_context(
        "/sales", method="POST", data={"invoice_kind": "purchase", "inv_date_greg": "bad"}
    ):
        app_module.unified_invoice.__wrapped__()
    assert observed[-1] == "purchase"

    observed.clear()
    with app_module.app.test_request_context(
        "/receive", method="POST", data={"cash_kind": "payment", "doc_date_greg": "bad"}
    ):
        app_module.unified_cash.__wrapped__()
    assert observed[-1] == "payment"
def test_production_requires_explicit_secret_key_and_admin_password(tmp_path):
    import re
    source = (Path(app_module.__file__).resolve()).read_text(encoding="utf-8")
    assert 'FLASK_ENV == "production"' in source
    assert 'SECRET_KEY باید در محیط production تنظیم شود.' in source
    assert 'ADMIN_PASSWORD باید قبل از bootstrap مدیر در production تنظیم شود.' in source

def test_development_config_has_nonempty_secret_without_environment_secret():
    import secrets as secrets_module
    assert app_module.SECRET_KEY
    assert app_module.ADMIN_PASSWORD
def test_production_admin_password_fallback_is_disabled():
    source = Path(app_module.__file__).resolve().read_text(encoding="utf-8")
    assert 'if not ADMIN_PASSWORD and FLASK_ENV != "production":' in source
    assert 'ADMIN_PASSWORD = "admin123"' in source
def test_legacy_purchase_and_sales_routes_infer_document_kind():
    with app_module.app.test_request_context("/purchase", method="GET"):
        assert app_module._invoice_kind_from_request() == "purchase"
    with app_module.app.test_request_context("/sales", method="GET"):
        assert app_module._invoice_kind_from_request() == "sales"
    with app_module.app.test_request_context("/invoice", method="GET", query_string={"kind": "purchase"}):
        assert app_module._invoice_kind_from_request() == "purchase"
def test_admin_git_update_requires_exact_official_remote():
    source = Path(app_module.__file__).resolve().read_text(encoding="utf-8")
    assert "normalized_remote = out.strip().rstrip(\"/\")" in source
    assert "if normalized_remote != expected_remote:" in source

def test_admin_git_update_does_not_report_ok_after_dependency_or_restart_failure():
    source = Path(app_module.__file__).resolve().read_text(encoding="utf-8")
    assert 'return jsonify(result), 500' in source
    assert 'result["ok"] = all(bool(s.get("ok")) for s in result["steps"])' in source

def test_document_view_requires_actual_module_permission(monkeypatch):
    monkeypatch.setattr(app_module, "render_template", lambda *args, **kwargs: "ok")
    with app_module.app.app_context():
        app_module.db.drop_all()
        app_module.db.create_all()

        person = app_module.Entity(
            type="person", code="901", name="مشتری تست", unit="شرکت", level=1
        )
        app_module.db.session.add(person)
        app_module.db.session.commit()

        purchase = app_module.Invoice(
            number="P-SEC-1",
            date=app_module.datetime.utcnow().date(),
            person_id=person.id,
            kind="purchase",
            total=1000,
        )
        payment = app_module.CashDoc(
            doc_type="payment",
            number="PAY-SEC-1",
            date=app_module.datetime.utcnow().date(),
            person_id=person.id,
            amount=1000,
            method="cash",
        )
        app_module.db.session.add_all([purchase, payment])
        app_module.db.session.commit()

        monkeypatch.setattr(
            app_module,
            "has_permission",
            lambda perm: perm == "sales",
        )
        with app_module.app.test_request_context(f"/invoice/{purchase.id}"):
            response = app_module.invoice_view.__wrapped__(purchase.id)
        assert getattr(response, "status_code", None) == 403

        monkeypatch.setattr(
            app_module,
            "has_permission",
            lambda perm: perm == "receive",
        )
        with app_module.app.test_request_context(f"/cash/{payment.id}"):
            response = app_module.cash_view.__wrapped__(payment.id)
        assert getattr(response, "status_code", None) == 403

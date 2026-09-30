from pathlib import Path

import app as app_module

ROOT = Path(__file__).resolve().parents[1]


def test_paki_brand_assets_exist_and_are_nonempty():
    for rel in [
        "static/icons/paki.svg",
        "static/favicon.svg",
        "static/manifest.webmanifest",
        "static/paki.css",
        "static/paki.js",
        "sw.js",
    ]:
        path = ROOT / rel
        assert path.is_file(), rel
        assert path.stat().st_size > 100, rel


def test_paki_interaction_hooks_are_present():
    css = (ROOT / "static/paki.css").read_text(encoding="utf-8")
    js = (ROOT / "static/paki.js").read_text(encoding="utf-8")
    assert "@keyframes paki-float" in css
    assert 'data-paki-mood="happy"' in css
    assert "serviceWorker.register" in js
    assert 'p+"/sw.js"' in js


def test_paki_service_worker_is_exposed_at_root():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    assert '@app.route(URL_PREFIX + "/sw.js", methods=["GET"])' in app
    assert 'send_from_directory(PROJECT_ROOT, "sw.js"' in app


def test_demo_mode_has_a_production_data_guard():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'if DEMO_MODE and Path(DATA_DIR).name.strip().lower()' in app
    assert '"data", "production", "prod"' in app

def test_demo_share_landing_and_start_flow_are_explicitly_split():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    template = (ROOT / "templates" / "demo_landing.html").read_text(encoding="utf-8")
    assert '@app.route(URL_PREFIX + "/demo", methods=["GET"])' in app
    assert '@app.route(URL_PREFIX + "/demo/start", methods=["GET", "POST"])' in app
    assert 'if request.method == "GET":' in app
    assert 'return redirect(URL_PREFIX + "/demo")' in app
    assert 'روش اشتراک‌گذاری' not in template
    assert "نسخه نمایشی" in template
    assert "share_url" in template
    assert "navigator.share" in template
    assert "کپی لینک" in template


def test_demo_visibility_is_persistent_inside_authenticated_ui():
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    assert 'session.get("demo_session")' in base
    assert "نسخه نمایشی" in base
    assert "اطلاعات واقعی وارد نکنید" in base


def test_transactions_landing_and_search_ui_exist():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    landing = (ROOT / "templates" / "transactions.html").read_text(encoding="utf-8")
    search = (ROOT / "templates" / "reports.html").read_text(encoding="utf-8")
    assert '@app.route(URL_PREFIX + "/transactions")' in app
    assert 'templates/transactions.html' not in app  # route uses render_template name, not filesystem path
    assert 'render_template("transactions.html"' in app
    assert "جستجو در تراکنش‌ها" in landing
    assert "جستجوی پیشرفته" in landing
    assert "جستجوی تراکنش‌ها" in search
    assert "حذف فیلترها" in search
    assert "نتیجه در این جستجو" in search

def test_demo_share_link_can_use_configured_public_base_url():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")' in app
    assert 'base_url = PUBLIC_BASE_URL or request.url_root.rstrip("/")' in app
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "PUBLIC_BASE_URL=" in env


def test_demo_share_link_preserves_forwarded_https_when_public_base_is_unset(monkeypatch):
    monkeypatch.setattr(app_module, "DEMO_MODE", True)
    monkeypatch.setattr(app_module, "PUBLIC_BASE_URL", "")
    response = app_module.app.test_client().get(
        "/demo",
        base_url="http://example.com",
        headers={"X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 200
    assert "https://example.com/demo" in response.get_data(as_text=True)

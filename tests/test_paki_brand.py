from pathlib import Path

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

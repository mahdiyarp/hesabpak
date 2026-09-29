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

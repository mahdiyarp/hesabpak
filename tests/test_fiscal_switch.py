import sqlite3
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="hesabpak-fiscal-test-"))

import app as app_module
from blueprints.backup import (
    FISCAL_ONLY_SETTING_KEYS,
    _capture_global_db_state,
    _restore_global_state_preserving_fiscal_settings,
)
from models.backup_models import Setting, UserSettings


def test_global_state_preserves_runtime_settings_when_switching():
    with app_module.app.app_context():
        app_module.db.drop_all()
        app_module.db.create_all()

        Setting.set("fiscal_year_current", "2025-03-21")
        Setting.set("fiscal_year_start", "2025-03-21")
        Setting.set("theme", "dark")
        UserSettings.get_for_user("admin").openai_model = "test-model"
        app_module.db.session.commit()

        state = _capture_global_db_state()

        Setting.set("fiscal_year_current", "2026-03-21")
        Setting.set("fiscal_year_start", "2026-03-21")
        Setting.set("theme", "light")
        app_module.db.session.commit()

        _restore_global_state_preserving_fiscal_settings(state)
        app_module.db.session.commit()

        assert Setting.get("theme") == "dark"
        assert Setting.get("fiscal_year_current") == "2026-03-21"
        assert Setting.get("fiscal_year_start") == "2026-03-21"
        assert UserSettings.get_for_user("admin").openai_model == "test-model"
        assert "theme" not in FISCAL_ONLY_SETTING_KEYS
        assert "fiscal_year_current" in FISCAL_ONLY_SETTING_KEYS
        assert "fiscal_year_start" in FISCAL_ONLY_SETTING_KEYS


def test_snapshot_current_year_uses_valid_sqlite_backup(tmp_path):
    with app_module.app.app_context():
        app_module.db.drop_all()
        app_module.db.create_all()
        Setting.set("fiscal_year_current", "2026-03-21")
        Setting.set("fiscal_year_start", "2026-03-21")
        Setting.set(
            "fiscal_years",
            '[{"start":"2026-03-21","label":"۱۴۰۵-۰۱-۰۱","key":"۱۴۰۵-۰۱-۰۱"}]',
        )
        app_module.db.session.commit()

        account = app_module.Account(code="101", name="صندوق", level=1)
        app_module.db.session.add(account)
        app_module.db.session.commit()

        from blueprints.backup import _snapshot_current_year, _load_fiscal_years
        folder = _snapshot_current_year(_load_fiscal_years())

        snapshot_db = folder / "data.sqlite3"
        assert snapshot_db.is_file()
        con = sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)
        try:
            assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert con.execute("select code from accounts where code='101'").fetchone()[0] == "101"
        finally:
            con.close()


def test_fiscal_year_key_is_filesystem_safe():
    from blueprints.backup import _year_key

    key = _year_key(r"../../..\\outside")
    assert ".." not in key
    assert "/" not in key
    assert "\\" not in key
    assert key != "outside"

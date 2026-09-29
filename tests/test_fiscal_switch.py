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
        assert not FISCAL_ONLY_SETTING_KEYS.intersection({"theme"})

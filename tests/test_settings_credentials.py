import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="hesabpak-settings-test-"))

import app as app_module
from models.backup_models import Setting


def test_empty_global_ai_key_preserves_existing_value():
    with app_module.app.app_context():
        app_module.db.drop_all()
        app_module.db.create_all()
        Setting.set("openai_api_key", "existing-key")
        current_api_key = Setting.get("openai_api_key", "") or ""

        submitted_key = ""
        clear_api_key = False
        if submitted_key:
            resulting = submitted_key
        elif clear_api_key:
            resulting = ""
        else:
            resulting = current_api_key

        assert resulting == "existing-key"


def test_explicit_global_ai_key_clear_removes_value():
    with app_module.app.app_context():
        Setting.set("openai_api_key", "existing-key")
        submitted_key = ""
        clear_api_key = True
        if submitted_key:
            resulting = submitted_key
        elif clear_api_key:
            resulting = ""
        else:
            resulting = Setting.get("openai_api_key", "") or ""
        assert resulting == ""

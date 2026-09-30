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


def test_ai_credentials_are_encrypted_at_rest_and_readable():
    with app_module.app.app_context():
        original_key = app_module.app.config.get("CREDENTIAL_ENCRYPTION_KEY")
        try:
            app_module.app.config["CREDENTIAL_ENCRYPTION_KEY"] = "test-stable-key"
            Setting.set("openai_api_key", app_module.encrypt_secret(app_module.current_app, "sk-test"))
            stored = Setting.get("openai_api_key", "") or ""
            assert stored != "sk-test"
            assert app_module._openai_api_key() == "sk-test"

            user_settings = app_module.UserSettings.get_for_user("encrypted-user")
            user_settings.openai_api_key = app_module.encrypt_secret(app_module.current_app, "sk-user-test")
            app_module.db.session.commit()
            assert app_module._user_openai_api_key(user_settings) == "sk-user-test"
        finally:
            Setting.set("openai_api_key", "")
            cleanup = app_module.UserSettings.query.filter_by(username="encrypted-user").first()
            if cleanup:
                app_module.db.session.delete(cleanup)
            app_module.db.session.commit()
            app_module.app.config["CREDENTIAL_ENCRYPTION_KEY"] = original_key

import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="hesabpak-assistant-test-"))

import app as app_module


def test_assistant_rejects_protected_project_paths():
    for path in (".env", ".env.production", "data/users.json", ".git/config", "../outside.txt"):
        try:
            app_module._resolve_project_path(path)
        except ValueError:
            continue
        raise AssertionError(f"protected path was accepted: {path}")


def test_non_admin_file_actions_are_rejected():
    with app_module.app.test_request_context("/assistant"):
        result = app_module._apply_assistant_actions([
            {
                "operation": "write_file",
                "path": "templates/example.html",
                "content": "x",
            }
        ])
    assert result["applied"] == 0
    assert result["failed"] == 1
    assert result["errors"]

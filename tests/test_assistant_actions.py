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
            {"operation": "write_file", "path": "templates/example.html", "content": "x"}
        ])
    assert result["applied"] == 0
    assert result["failed"] == 1
    assert result["errors"]


def test_build_openai_messages_uses_input_content_types():
    messages = app_module._build_openai_messages([
        {
            "role": "user",
            "text": "این یک فاکتور است",
            "attachments": [
                {
                    "type": "image",
                    "mime_type": "image/png",
                    "data": "aGVsbG8=",
                }
            ],
        }
    ])

    assert messages[0]["content"][0] == {
        "type": "input_text",
        "text": "این یک فاکتور است",
    }
    assert messages[0]["content"][1]["type"] == "input_image"
    assert messages[0]["content"][1]["image_url"].startswith("data:image/png;base64,")
    assert all(
        part.get("type") != "output_text"
        for part in messages[0]["content"]
        if isinstance(part, dict)
    )


def test_assistant_schema_is_strict_compatible():
    schema = app_module.AI_RESPONSE_SCHEMA["schema"]

    def check(node):
        if isinstance(node, list):
            for item in node:
                check(item)
            return
        if not isinstance(node, dict):
            return

        if node.get("type") == "object":
            properties = node.get("properties", {})
            assert node.get("additionalProperties") is False
            assert set(node.get("required", [])) == set(properties)
            for value in properties.values():
                check(value)

        check(node.get("items"))
    check(schema)

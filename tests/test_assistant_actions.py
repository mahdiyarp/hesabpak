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


def test_prepare_invoice_plan_rejects_unknown_kind():
    import pytest
    with pytest.raises(ValueError):
        app_module._prepare_invoice_plan({
            "kind": "unknown",
            "partner": {"name": "مشتری"},
            "items": [{"name": "کالا", "qty": 1, "unit_price": 100}],
        })


def test_prepare_invoice_plan_rejects_invalid_date():
    import pytest
    with pytest.raises(ValueError):
        app_module._prepare_invoice_plan({
            "kind": "sales",
            "partner": {"name": "مشتری"},
            "date": "not-a-date",
            "items": [{"name": "کالا", "qty": 1, "unit_price": 100}],
        })


def test_prepare_invoice_plan_rejects_negative_price_and_nonpositive_qty():
    import pytest
    with pytest.raises(ValueError):
        app_module._prepare_invoice_plan({
            "kind": "sales",
            "partner": {"name": "مشتری"},
            "items": [{"name": "کالا", "qty": 1, "unit_price": -100}],
        })
    with pytest.raises(ValueError):
        app_module._prepare_invoice_plan({
            "kind": "sales",
            "partner": {"name": "مشتری"},
            "items": [{"name": "کالا", "qty": 0, "unit_price": 100}],
        })


def test_prepare_cash_plan_rejects_unknown_type_and_invalid_dates():
    import pytest
    with pytest.raises(ValueError):
        app_module._prepare_cash_plan({
            "doc_type": "unknown",
            "person": {"name": "مشتری"},
            "amount": 100,
        })
    with pytest.raises(ValueError):
        app_module._prepare_cash_plan({
            "doc_type": "receive",
            "person": {"name": "مشتری"},
            "amount": 100,
            "date": "not-a-date",
        })
    with pytest.raises(ValueError):
        app_module._prepare_cash_plan({
            "doc_type": "receive",
            "person": {"name": "مشتری"},
            "amount": 100,
            "cheque_due": "not-a-date",
        })


def test_assistant_model_migrates_deprecated_alias():
    with app_module.app.app_context():
        original = app_module.Setting.get("openai_model")
        try:
            app_module.Setting.set("openai_model", "o4-mini")
            assert app_module._assistant_model() == "gpt-5-mini"
        finally:
            app_module.Setting.set("openai_model", original or "gpt-5-mini")
            app_module.db.session.rollback()


def test_reasoning_model_is_marked_for_temperature_omission():
    assert "gpt-5-mini" in app_module.ASSISTANT_REASONING_MODELS
    assert "o4-mini" not in {key for key, _ in app_module.ASSISTANT_MODEL_CHOICES}


def test_current_assistant_default_is_gpt56_luna():
    with app_module.app.app_context():
        original = app_module.Setting.get("openai_model")
        try:
            app_module.Setting.set("openai_model", "")
            assert app_module._assistant_model() == "gpt-5.6-luna"
        finally:
            app_module.Setting.set("openai_model", original or "gpt-5.6-luna")
            app_module.db.session.rollback()


def test_all_current_gpt56_models_are_reasoning_models():
    assert {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"} <= app_module.ASSISTANT_REASONING_MODELS


def test_assistant_api_ready_uses_personal_key(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(app_module, "OpenAI", object())
    monkeypatch.setattr(app_module, "_openai_api_key", lambda: "")
    monkeypatch.setattr(
        app_module,
        "current_user",
        SimpleNamespace(is_authenticated=True, username="personal-user"),
    )

    with app_module.app.app_context():
        app_module.db.drop_all()
        app_module.db.create_all()
        settings = app_module.UserSettings.get_for_user("personal-user")
        settings.openai_api_key = "personal-test-key"
        app_module.db.session.commit()

        assert app_module._assistant_api_ready() is True


def test_assistant_chat_uses_personal_key_gate(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(app_module, "OpenAI", object())
    monkeypatch.setattr(app_module, "_assistant_api_ready", lambda: True)
    monkeypatch.setattr(app_module, "ensure_permission", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        app_module,
        "current_user",
        SimpleNamespace(is_authenticated=True, username="personal-user"),
    )
    monkeypatch.setattr(
        app_module,
        "_call_openai_assistant",
        lambda messages: {
            "reply": "ok",
            "needs_confirmation": False,
            "invoice": None,
            "cash": None,
            "actions": [],
        },
    )

    with app_module.app.test_request_context(
        "/assistant/api/chat",
        method="POST",
        json={"messages": [{"role": "user", "text": "سلام"}]},
    ):
        response = app_module.assistant_chat.__wrapped__()

    assert response.status_code == 200
    assert response.get_json()["reply"] == "ok"


def test_prepare_invoice_plan_rejects_empty_partner_and_items():
    import pytest

    with pytest.raises(ValueError):
        app_module._prepare_invoice_plan({
            "kind": "sales",
            "partner": {},
            "items": [{"name": "کالا", "qty": 1, "unit_price": 100}],
        })

    with pytest.raises(ValueError):
        app_module._prepare_invoice_plan({
            "kind": "sales",
            "partner": {"name": "مشتری"},
            "items": [],
        })


def test_prepare_cash_plan_rejects_empty_person():
    import pytest

    with pytest.raises(ValueError):
        app_module._prepare_cash_plan({
            "doc_type": "receive",
            "person": {},
            "amount": 100,
        })


def test_assistant_chat_returns_400_for_invalid_invoice_plan(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(app_module, "ensure_permission", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "OpenAI", object())
    monkeypatch.setattr(app_module, "_assistant_api_ready", lambda: True)
    monkeypatch.setattr(
        app_module,
        "current_user",
        SimpleNamespace(is_authenticated=True, username="test-user"),
    )
    monkeypatch.setattr(
        app_module,
        "_call_openai_assistant",
        lambda messages: {
            "reply": "bad invoice",
            "needs_confirmation": False,
            "invoice": {
                "kind": "unknown",
                "partner": {"name": "مشتری"},
                "items": [{"name": "کالا", "qty": 1, "unit_price": 100}],
            },
            "cash": None,
            "actions": [],
        },
    )

    with app_module.app.test_request_context(
        "/assistant/api/chat",
        method="POST",
        json={"messages": [{"role": "user", "text": "ثبت فاکتور"}]},
    ):
        response = app_module.assistant_chat.__wrapped__()

    assert response.status_code == 400
    assert "نوع فاکتور" in response.get_json()["message"]

def test_ai_plan_permission_maps_invoice_and_cash_modules():
    assert app_module._assistant_plan_permission({
        "kind": "sales",
        "items": [{"name": "کالا", "qty": 1}],
    }) == "sales"
    assert app_module._assistant_plan_permission({
        "kind": "purchase",
        "items": [{"name": "کالا", "qty": 1}],
    }) == "purchase"
    assert app_module._assistant_plan_permission({
        "doc_type": "receive",
        "person": {"name": "مشتری"},
        "amount": 100,
    }) == "receive"
    assert app_module._assistant_plan_permission({
        "doc_type": "payment",
        "person": {"name": "مشتری"},
        "amount": 100,
    }) == "payment"

def test_ai_plan_permission_rejects_unknown_type():
    assert app_module._assistant_plan_permission({"kind": "unknown", "items": []}) is None
    assert app_module._assistant_plan_permission({"doc_type": "unknown", "person": {}, "amount": 1}) is None
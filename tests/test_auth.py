import app as app_module


def test_password_hash_roundtrip():
    stored = app_module._hash_password("correct horse battery")
    assert stored.startswith(app_module.PASSWORD_HASH_PREFIX)
    ok, needs_upgrade = app_module._verify_password(stored, "correct horse battery")
    assert ok is True
    assert needs_upgrade is False
    bad, _ = app_module._verify_password(stored, "wrong")
    assert bad is False


def test_legacy_plaintext_password_is_verified_and_marked_for_upgrade():
    ok, needs_upgrade = app_module._verify_password("legacy-secret", "legacy-secret")
    assert ok is True
    assert needs_upgrade is True
    bad, needs_upgrade = app_module._verify_password("legacy-secret", "other")
    assert bad is False
    assert needs_upgrade is True


def test_generated_hash_uses_fresh_salt():
    first = app_module._hash_password("same-secret")
    second = app_module._hash_password("same-secret")
    assert first != second
    assert app_module._verify_password(first, "same-secret")[0] is True
    assert app_module._verify_password(second, "same-secret")[0] is True

from pathlib import Path
import sqlite3
import zipfile
import json
import os

import pytest
from flask import Flask

from utils.backup_utils import create_full_backup, resolve_backup_path, restore_backup, autosave_record


def make_app(tmp_path):
    app = Flask(__name__)
    app.config["DATA_DIR"] = str(tmp_path / "data")
    app.config["DB_FILE"] = "hesabpak.sqlite3"
    app.config["APP_VERSION"] = "test"
    app.config["INCLUDE_UPLOADS_IN_BACKUP"] = "true"
    return app


def test_create_backup_has_unique_name_and_sqlite(tmp_path):
    app = make_app(tmp_path)
    data = Path(app.config["DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    db = data / app.config["DB_FILE"]
    con = sqlite3.connect(db)
    con.execute("create table t (id integer primary key, value text)")
    con.execute("insert into t(value) values ('ok')")
    con.commit()
    con.close()

    first = Path(create_full_backup(app))
    second = Path(create_full_backup(app))

    assert first.is_file()
    assert second.is_file()
    assert first.name != second.name

    with zipfile.ZipFile(first) as zf:
        assert f"db/{db.name}" in zf.namelist()
        assert "metadata.json" in zf.namelist()


def test_resolve_backup_rejects_traversal(tmp_path):
    app = make_app(tmp_path)
    with pytest.raises(ValueError):
        resolve_backup_path(app, "../backup_2026-09-29_12-00-00_deadbeef.zip")
    with pytest.raises(ValueError):
        resolve_backup_path(app, "2026/../../backup_2026-09-29_12-00-00_deadbeef.zip")
    with pytest.raises(ValueError):
        resolve_backup_path(app, "/tmp/backup_2026-09-29_12-00-00_deadbeef.zip")


def test_restore_uses_safe_relative_path(tmp_path):
    app = make_app(tmp_path)
    data = Path(app.config["DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    backup_dir = data / "backups" / "1405"
    backup_dir.mkdir(parents=True, exist_ok=True)
    db = data / app.config["DB_FILE"]

    source = tmp_path / "source.sqlite3"
    con = sqlite3.connect(source)
    con.execute("create table t (value text)")
    con.execute("insert into t(value) values ('restored')")
    con.commit()
    con.close()

    archive = backup_dir / "backup_2026-09-29_12-00-00_deadbeef.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(source, f"db/{db.name}")

    restore_backup(app, "1405/" + archive.name)

    con = sqlite3.connect(db)
    try:
        assert con.execute("select value from t").fetchone()[0] == "restored"
    finally:
        con.close()


def test_restore_restores_uploads(tmp_path):
    app = make_app(tmp_path)
    data = Path(app.config["DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    uploads = data / "uploads" / "assistant"
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / "old.txt").write_text("old", encoding="utf-8")

    backup_dir = data / "backups" / "1405"
    backup_dir.mkdir(parents=True, exist_ok=True)
    db = data / app.config["DB_FILE"]

    source = tmp_path / "source.sqlite3"
    con = sqlite3.connect(source)
    con.execute("create table t (value text)")
    con.execute("insert into t(value) values ('restored') ")
    con.commit()
    con.close()

    archive = backup_dir / "backup_2026-09-29_12-00-01_deadbeef.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(source, f"db/{db.name}")
        zf.writestr("uploads/assistant/restored.txt", b"restored")

    restore_backup(app, "1405/" + archive.name)

    assert (uploads / "restored.txt").read_text(encoding="utf-8") == "restored"
    assert not (uploads / "old.txt").exists()


def test_restore_rejects_upload_path_traversal_before_replacing_live_data(tmp_path):
    app = make_app(tmp_path)
    data = Path(app.config["DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    db = data / app.config["DB_FILE"]

    con = sqlite3.connect(db)
    con.execute("create table t (value text)")
    con.execute("insert into t(value) values ('live')")
    con.commit()
    con.close()

    backup_dir = data / "backups" / "1405"
    backup_dir.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.sqlite3"
    con = sqlite3.connect(source)
    con.execute("create table t (value text)")
    con.execute("insert into t(value) values ('restored')")
    con.commit()
    con.close()

    archive = backup_dir / "backup_2026-09-29_12-00-02_deadbeef.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(source, f"db/{db.name}")
        zf.writestr("uploads/../../escape.txt", b"blocked")

    with pytest.raises(ValueError):
        restore_backup(app, "1405/" + archive.name)

    con = sqlite3.connect(db)
    try:
        assert con.execute("select value from t").fetchone()[0] == "live"
    finally:
        con.close()
    assert not (data / "escape.txt").exists()


def test_autosave_does_not_overwrite_same_second(tmp_path):
    app = make_app(tmp_path)
    first = Path(autosave_record(app, "Invoice", 7, {"value": "first"}))
    second = Path(autosave_record(app, "Invoice", 7, {"value": "second"}))

    assert first.is_file()
    assert second.is_file()
    assert first != second
    assert len(list(first.parent.glob("*.json.gz"))) == 2


def test_restore_empty_upload_backup_clears_existing_uploads(tmp_path):
    app = make_app(tmp_path)
    data = Path(app.config["DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    uploads = data / "uploads" / "assistant"
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / "stale.txt").write_text("stale", encoding="utf-8")

    backup_dir = data / "backups" / "1405"
    backup_dir.mkdir(parents=True, exist_ok=True)
    db = data / app.config["DB_FILE"]

    source = tmp_path / "source.sqlite3"
    con = sqlite3.connect(source)
    con.execute("create table t (value text)")
    con.execute("insert into t(value) values ('restored')")
    con.commit()
    con.close()

    archive = backup_dir / "backup_2026-09-29_12-00-03_deadbeef.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(source, f"db/{db.name}")
        zf.writestr(
            "metadata.json",
            '{"include_uploads": "true", "uploads_count": 0}'
        )

    restore_backup(app, "1405/" + archive.name)

    assert not uploads.exists()


def test_create_full_backup_includes_runtime_user_and_fiscal_state(tmp_path):
    app = make_app(tmp_path)
    data = Path(app.config["DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "users.json").write_text('{"users":[]}', encoding="utf-8")
    case_dir = data / "fiscal_cases" / "1405"
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "snapshot.json").write_text('{"fiscal_year":"1405"}', encoding="utf-8")

    db = data / app.config["DB_FILE"]
    con = sqlite3.connect(db)
    con.execute("create table t (value text)")
    con.execute("insert into t(value) values ('ok')")
    con.commit()
    con.close()

    archive = Path(create_full_backup(app))
    with zipfile.ZipFile(archive) as zf:
        assert "runtime/users.json" in zf.namelist()
        assert "runtime/fiscal_cases/1405/snapshot.json" in zf.namelist()
        metadata = zf.read("metadata.json").decode("utf-8")
        assert '"include_users_file": true' in metadata
        assert '"include_fiscal_cases": true' in metadata
        assert '"fiscal_case_files": 1' in metadata


def test_restore_restores_runtime_user_and_fiscal_state(tmp_path):
    app = make_app(tmp_path)
    data = Path(app.config["DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "users.json").write_text('{"users":[{"username":"old"}]}', encoding="utf-8")
    old_case = data / "fiscal_cases" / "old"
    old_case.mkdir(parents=True, exist_ok=True)
    (old_case / "snapshot.json").write_text('{"old":true}', encoding="utf-8")

    backup_dir = data / "backups" / "1405"
    backup_dir.mkdir(parents=True, exist_ok=True)
    db = data / app.config["DB_FILE"]

    source = tmp_path / "source.sqlite3"
    con = sqlite3.connect(source)
    con.execute("create table t (value text)")
    con.execute("insert into t(value) values ('restored')")
    con.commit()
    con.close()

    archive = backup_dir / "backup_2026-09-29_12-00-04_deadbeef.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(source, f"db/{db.name}")
        zf.writestr("runtime/users.json", '{"users":[{"username":"new"}]}')
        zf.writestr("runtime/fiscal_cases/1405/snapshot.json", '{"new":true}')
        zf.writestr(
            "metadata.json",
            '{"include_uploads": "false", "include_users_file": true, "include_fiscal_cases": true, "fiscal_case_files": 1}'
        )

    restore_backup(app, "1405/" + archive.name)

    assert '"new"' in (data / "users.json").read_text(encoding="utf-8")
    assert (data / "fiscal_cases" / "1405" / "snapshot.json").exists()
    assert not old_case.exists()


def test_create_full_backup_includes_runtime_json_users_and_fiscal_cases(tmp_path):
    app = make_app(tmp_path)
    data = Path(app.config["DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "users.json").write_text('{"users":[]}', encoding="utf-8")
    (data / "rates.json").write_text('{"currencies":{}}', encoding="utf-8")
    case_dir = data / "fiscal_cases" / "1405"
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "snapshot.json").write_text('{"fiscal_year":"1405"}', encoding="utf-8")

    db = data / app.config["DB_FILE"]
    con = sqlite3.connect(db)
    con.execute("create table t (value text)")
    con.execute("insert into t(value) values ('ok')")
    con.commit()
    con.close()

    archive = Path(create_full_backup(app))
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert "runtime/users.json" in names
        assert "runtime/json/rates.json" in names
        assert "runtime/fiscal_cases/1405/snapshot.json" in names
        metadata = json.loads(zf.read("metadata.json").decode("utf-8"))
        assert metadata["format_version"] == 3
        assert metadata["include_users_file"] is True
        assert metadata["include_runtime_json"] is True
        assert metadata["include_fiscal_cases"] is True
        assert metadata["runtime_json_files"] == 1
        assert metadata["fiscal_case_files"] == 1


def test_restore_full_runtime_state_replaces_stale_files(tmp_path):
    app = make_app(tmp_path)
    data = Path(app.config["DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "users.json").write_text('{"users":[{"username":"old"}]}', encoding="utf-8")
    (data / "rates.json").write_text('{"old":true}', encoding="utf-8")
    stale_case = data / "fiscal_cases" / "old"
    stale_case.mkdir(parents=True, exist_ok=True)
    (stale_case / "snapshot.json").write_text('{"old":true}', encoding="utf-8")

    backup_dir = data / "backups" / "1405"
    backup_dir.mkdir(parents=True, exist_ok=True)
    db = data / app.config["DB_FILE"]

    source = tmp_path / "source.sqlite3"
    con = sqlite3.connect(source)
    con.execute("create table t (value text)")
    con.execute("insert into t(value) values ('restored')")
    con.commit()
    con.close()

    archive = backup_dir / "backup_2026-09-29_12-00-05_deadbeef.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(source, f"db/{db.name}")
        zf.writestr("runtime/users.json", '{"users":[{"username":"new"}]}')
        zf.writestr("runtime/json/rates.json", '{"new":true}')
        zf.writestr("runtime/fiscal_cases/1405/snapshot.json", '{"new":true}')
        zf.writestr(
            "metadata.json",
            '{"format_version": 3, "include_uploads": "false", "include_users_file": true, '
            '"include_runtime_json": true, "include_fiscal_cases": true, '
            '"runtime_json_files": 1, "fiscal_case_files": 1}'
        )

    restore_backup(app, "1405/" + archive.name)

    assert '"new"' in (data / "users.json").read_text(encoding="utf-8")
    assert '"new"' in (data / "rates.json").read_text(encoding="utf-8")
    assert not stale_case.exists()
    assert (data / "fiscal_cases" / "1405" / "snapshot.json").exists()


def test_restore_rollback_removes_new_users_file_when_original_was_absent(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    data = Path(app.config["DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    db = data / app.config["DB_FILE"]
    con = sqlite3.connect(db)
    con.execute("create table t (value text)")
    con.execute("insert into t(value) values ('live')")
    con.commit()
    con.close()

    backup_dir = data / "backups" / "1405"
    backup_dir.mkdir(parents=True, exist_ok=True)
    archive = backup_dir / "backup_2026-09-29_12-00-06_deadbeef.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(db, f"db/{db.name}")
        zf.writestr("runtime/users.json", '{"users":[{"username":"new"}]}')
        zf.writestr(
            "metadata.json",
            '{"format_version": 3, "include_uploads": "false", "include_users_file": true, '
            '"include_runtime_json": false, "include_fiscal_cases": false}'
        )

    original_replace = os.replace
    calls = {"count": 0}
    def fail_on_users(src, dst):
        calls["count"] += 1
        if str(dst).endswith("users.json"):
            raise OSError("forced users replacement failure")
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_on_users)

    with pytest.raises(OSError):
        restore_backup(app, "1405/" + archive.name)

    assert not (data / "users.json").exists()
    con = sqlite3.connect(db)
    try:
        assert con.execute("select value from t").fetchone()[0] == "live"
    finally:
        con.close()

def test_empty_runtime_trees_are_authoritative_in_full_backup(tmp_path):
    app = make_app(tmp_path)
    data = Path(app.config["DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)

    db = data / app.config["DB_FILE"]
    con = sqlite3.connect(db)
    con.execute("create table t (value text)")
    con.execute("insert into t(value) values ('ok')")
    con.commit()
    con.close()

    archive = Path(create_full_backup(app))
    with zipfile.ZipFile(archive) as zf:
        metadata = json.loads(zf.read("metadata.json").decode("utf-8"))
        assert metadata["format_version"] == 3
        assert metadata["include_runtime_json"] is True
        assert metadata["include_fiscal_cases"] is True
        assert metadata["runtime_json_files"] == 0
        assert metadata["fiscal_case_files"] == 0

def test_restore_authoritative_empty_runtime_state_removes_stale_files(tmp_path):
    app = make_app(tmp_path)
    data = Path(app.config["DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    db = data / app.config["DB_FILE"]
    con = sqlite3.connect(db)
    con.execute("create table t (value text)")
    con.execute("insert into t(value) values ('restored')")
    con.commit()
    con.close()

    (data / "rates.json").write_text('{"stale":true}', encoding="utf-8")
    stale_case = data / "fiscal_cases" / "old"
    stale_case.mkdir(parents=True, exist_ok=True)
    (stale_case / "snapshot.json").write_text('{"stale":true}', encoding="utf-8")

    backup_dir = data / "backups" / "1405"
    backup_dir.mkdir(parents=True, exist_ok=True)
    archive = backup_dir / "backup_2026-09-29_12-00-07_deadbeef.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(db, f"db/{db.name}")
        zf.writestr("metadata.json", '{"format_version":3,"include_uploads":false,"include_users_file":false,"include_runtime_json":true,"include_fiscal_cases":true,"runtime_json_files":0,"fiscal_case_files":0}')

    restore_backup(app, "1405/" + archive.name)
    assert not (data / "rates.json").exists()
    assert not stale_case.exists()
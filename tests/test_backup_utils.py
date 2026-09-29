from pathlib import Path
import sqlite3
import zipfile

import pytest
from flask import Flask

from utils.backup_utils import create_full_backup, resolve_backup_path, restore_backup


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

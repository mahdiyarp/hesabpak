# utils/backup_utils.py
import os, io, json, gzip, shutil, datetime, zipfile, tempfile
from pathlib import Path

def ensure_dirs(app):
    data_dir = Path(app.config.get("DATA_DIR", "data"))
    backup_dir = data_dir / app.config.get("BACKUP_DIR", "backups")
    autosave_dir = backup_dir / "autosave"
    uploads_dir = data_dir / "uploads"
    for p in [data_dir, backup_dir, autosave_dir, uploads_dir]:
        p.mkdir(parents=True, exist_ok=True)
    return data_dir, backup_dir, autosave_dir, uploads_dir


def autosave_marker_path(app):
    _, _, autosave_dir, _ = ensure_dirs(app)
    return autosave_dir / "_last_autosave.json"


def touch_autosave_marker(app, ts=None):
    ts = ts or datetime.datetime.now().isoformat(timespec="seconds")
    marker = autosave_marker_path(app)
    marker.parent.mkdir(parents=True, exist_ok=True)
    with open(marker, "w", encoding="utf-8") as fh:
        json.dump({"ts": ts}, fh, ensure_ascii=False)
    return ts


def read_autosave_marker(app):
    marker = autosave_marker_path(app)
    if not marker.exists():
        return None
    try:
        with open(marker, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data.get("ts")
    except Exception:
        return None

def now_stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

def db_path(app):
    data_dir = Path(app.config.get("DATA_DIR", "data"))
    return data_dir / app.config.get("DB_FILE", "app.db")

def create_full_backup(app, user="system", reason="manual"):
    """
    می‌سازد: ZIP شامل DB + uploads/ (اختیاری) + metadata.json
    خروجی: مسیر فایل بکاپ
    """
    data_dir, backup_dir, autosave_dir, uploads_dir = ensure_dirs(app)
    stamp = now_stamp()
    fn = f"backup_{stamp}.zip"
    out = backup_dir / fn

    meta = {
        "created_at": stamp,
        "user": user,
        "reason": reason,
        "db_file": str(db_path(app).name),
        "include_uploads": str(app.config.get("INCLUDE_UPLOADS_IN_BACKUP", "true")).lower(),
        "app_version": app.config.get("APP_VERSION", "unknown"),
    }

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        # DB
        dbfile = db_path(app)
        if dbfile.exists():
            z.write(dbfile, arcname=f"db/{dbfile.name}")
        # uploads (اختیاری)
        if str(app.config.get("INCLUDE_UPLOADS_IN_BACKUP", "true")).lower() == "true":
            if uploads_dir.exists():
                for root, dirs, files in os.walk(uploads_dir):
                    for f in files:
                        p = Path(root)/f
                        rel = p.relative_to(data_dir)
                        z.write(p, arcname=str(rel))
        # metadata
        z.writestr("metadata.json", json.dumps(meta, ensure_ascii=False, indent=2))
    return str(out)

def list_backups(app):
    _, backup_dir, _, _ = ensure_dirs(app)
    items = []
    for p in sorted(backup_dir.glob("backup_*.zip"), reverse=True):
        items.append({
            "name": p.name,
            "size": p.stat().st_size,
            "mtime": datetime.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            "path": str(p)
        })
    return items

def restore_backup(app, zip_filename):
    """
    ری‌استور امن برای SQLite:
    - zip را باز می‌کند
    - db را به صورت امن جایگزین می‌کند (app.db -> app.db.before-restore)
    - نیاز به ری‌استارت سرویس دارد
    """
    data_dir, backup_dir, _, _ = ensure_dirs(app)
    zpath = backup_dir / zip_filename
    if not zpath.exists():
        raise FileNotFoundError("بکاپ پیدا نشد")

    dbfile = db_path(app)
    with zipfile.ZipFile(zpath, "r") as z:
        # پیدا کردن db داخل zip
        db_inside = None
        expected_name = f"db/{dbfile.name}"
        for info in z.infolist():
            if info.is_dir():
                continue
            if info.filename == expected_name:
                db_inside = info.filename
                break
            if info.filename.startswith("db/") and info.filename.lower().endswith((".db", ".sqlite", ".sqlite3")):
                db_inside = info.filename
                # به جستجو ادامه نمی‌دهیم چون احتمالاً همین فایل موردنظر است
                break
        if not db_inside:
            raise RuntimeError("DB داخل بکاپ پیدا نشد")

        # استخراج به temp
        tmpdir = Path(tempfile.mkdtemp())
        z.extract(db_inside, tmpdir)
        extracted = tmpdir / db_inside

        # جایگزینی امن
        if dbfile.exists():
            backup_old = dbfile.with_suffix(".before-restore")
            shutil.copy2(dbfile, backup_old)
        shutil.copy2(extracted, dbfile)
    # یادداشت: برای اعمال کامل، بهتر است سرویس را ری‌استارت کنی.
    return str(dbfile)

def autosave_record(app, model_name: str, pk_value, payload: dict):
    """
    برای هر «سند» ذخیرهٔ JSON فشرده در autosave/
    """
    _, backup_dir, autosave_dir, _ = ensure_dirs(app)
    d = datetime.datetime.now()
    day_dir = autosave_dir / d.strftime("%Y-%m-%d") / model_name
    day_dir.mkdir(parents=True, exist_ok=True)
    fn = f"{d.strftime('%H-%M-%S')}_{pk_value}.json.gz"
    path = day_dir / fn
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    try:
        touch_autosave_marker(app, d.isoformat(timespec="seconds"))
    except Exception:
        pass
    return str(path)

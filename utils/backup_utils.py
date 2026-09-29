# utils/backup_utils.py
import os, io, json, gzip, shutil, datetime, zipfile, tempfile, decimal, uuid, re, sqlite3
from pathlib import Path
from typing import Optional

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


def resolve_backup_path(app, zip_filename):
    """Resolve a backup path without allowing traversal outside the backup root."""
    _, backup_dir, _, _ = ensure_dirs(app)
    raw = str(zip_filename or "").strip().replace("\\", "/")
    rel = Path(raw)
    if not raw or rel.is_absolute() or ".." in rel.parts or rel.name != raw.split("/")[-1]:
        raise ValueError("مسیر فایل بکاپ نامعتبر است.")
    if rel.suffix.lower() != ".zip" or not rel.name.startswith("backup_") or len(rel.name) > 180:
        raise ValueError("نام فایل بکاپ نامعتبر است.")
    target = (backup_dir / rel).resolve()
    root = backup_dir.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("مسیر فایل بکاپ خارج از پوشه مجاز است.") from exc
    return target


def create_full_backup(app, user="system", reason="manual"):
    """
    می‌سازد: ZIP شامل DB + uploads/ (اختیاری) + metadata.json
    خروجی: مسیر فایل بکاپ
    """
    data_dir, backup_dir, autosave_dir, uploads_dir = ensure_dirs(app)
    stamp = now_stamp()
    fn = f"backup_{stamp}_{uuid.uuid4().hex[:8]}.zip"
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
        # DB: use SQLite's online backup API so active databases are copied consistently.
        dbfile = db_path(app)
        if dbfile.exists():
            fd, tmp_name = tempfile.mkstemp(prefix="hesabpak_backup_", suffix=".sqlite3")
            os.close(fd)
            temp_db = Path(tmp_name)
            try:
                if dbfile.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                    src = sqlite3.connect(str(dbfile))
                    dst = sqlite3.connect(str(temp_db))
                    try:
                        src.backup(dst)
                    finally:
                        dst.close()
                        src.close()
                else:
                    shutil.copy2(dbfile, temp_db)
                z.write(temp_db, arcname=f"db/{dbfile.name}")
            finally:
                try:
                    temp_db.unlink(missing_ok=True)
                except TypeError:
                    if temp_db.exists():
                        temp_db.unlink()
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


def list_backups(app, year_key: Optional[str] = None):
    """Return backup metadata for the requested fiscal year.

    If year_key is provided the lookup is restricted to the matching
    sub-directory inside the backup folder. When the key is missing or empty
    all top-level files plus the newest entry from each known fiscal-year folder
    are returned. The helper keeps the previous public contract (list of dict
    objects containing name/size/mtime) while also exposing the year folder via
    the year field so that callers can label the origin when required.
    """

    _, backup_dir, _, _ = ensure_dirs(app)

    def _collect(directory: Path, *, year: Optional[str]):
        rows = []
        for p in sorted(directory.glob("backup_*.zip"), reverse=True):
            rows.append({
                "name": p.name,
                "size": p.stat().st_size,
                "mtime": datetime.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
                "path": str(p),
                "year": year,
            })
        return rows

    items = []

    if year_key:
        target = backup_dir / year_key
        if target.exists():
            items.extend(_collect(target, year=year_key))
        else:
            return []
    else:
        items.extend(_collect(backup_dir, year=None))
        for sub in sorted([p for p in backup_dir.iterdir() if p.is_dir()], reverse=True):
            items.extend(_collect(sub, year=sub.name))

    return items


def _safe_archive_relative_path(name: str) -> Path:
    raw = str(name or "").replace("\\", "/")
    rel = Path(raw)
    if not raw or rel.is_absolute() or ".." in rel.parts:
        raise ValueError("مسیر عضو بکاپ نامعتبر است.")
    return rel


def _extract_backup_uploads(z: zipfile.ZipFile, staging_root: Path) -> bool:
    """Extract upload members into an isolated staging directory.

    Never uses ZipFile.extractall(); every member path is validated before the
    file is created, which prevents archive path traversal.
    """
    found = False
    upload_prefix = "uploads/"
    for info in z.infolist():
        name = info.filename.replace("\\", "/")
        if not name.startswith(upload_prefix) or info.is_dir():
            continue
        rel = _safe_archive_relative_path(name[len(upload_prefix):])
        if not rel.parts:
            continue
        target = (staging_root / rel).resolve()
        root = staging_root.resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("مسیر فایل آپلود خارج از پوشه مجاز است.") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        with z.open(info, "r") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        found = True
    return found


def restore_backup(app, zip_filename):
    """
    Restore SQLite plus upload files from a full backup.

    The current DB and uploads are preserved as .before-restore siblings so
    the operation can be rolled back if any later replacement step fails.
    The service should still be restarted after restore.
    """
    data_dir, backup_dir, _, uploads_dir = ensure_dirs(app)
    zpath = resolve_backup_path(app, zip_filename)
    if not zpath.exists():
        raise FileNotFoundError("بکاپ پیدا نشد")

    dbfile = db_path(app)
    temp_root = Path(tempfile.mkdtemp(prefix="hesabpak_restore_"))
    extracted_db = temp_root / dbfile.name
    staged_live_db = temp_root / (dbfile.name + ".restored")
    staged_uploads = temp_root / "uploads"
    old_db = Path(str(dbfile) + ".before-restore")
    old_uploads = Path(str(uploads_dir) + ".before-restore")
    db_replaced = False
    uploads_replaced = False
    uploads_moved_aside = False
    uploads_found = False

    try:
        with zipfile.ZipFile(zpath, "r") as z:
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
                    break
            if not db_inside:
                raise RuntimeError("DB داخل بکاپ پیدا نشد")

            with z.open(db_inside, "r") as src, extracted_db.open("wb") as dst:
                shutil.copyfileobj(src, dst)

            con = sqlite3.connect(f"file:{extracted_db}?mode=ro", uri=True)
            try:
                check = con.execute("PRAGMA integrity_check").fetchone()
                if not check or str(check[0]).lower() != "ok":
                    raise RuntimeError("بررسی سلامت SQLite بکاپ موفق نبود.")
            finally:
                con.close()

            uploads_found = _extract_backup_uploads(z, staged_uploads)

        if dbfile.exists():
            shutil.copy2(dbfile, old_db)
        shutil.copy2(extracted_db, staged_live_db)
        os.replace(staged_live_db, dbfile)
        db_replaced = True

        if uploads_found:
            if old_uploads.exists():
                if old_uploads.is_dir():
                    shutil.rmtree(old_uploads)
                else:
                    old_uploads.unlink()
            if uploads_dir.exists():
                shutil.move(str(uploads_dir), str(old_uploads))
                uploads_moved_aside = True
            shutil.move(str(staged_uploads), str(uploads_dir))
            uploads_replaced = True
    except Exception:
        try:
            if db_replaced and old_db.exists():
                shutil.copy2(old_db, dbfile)
            if uploads_replaced and uploads_dir.exists():
                shutil.rmtree(uploads_dir)
            if uploads_moved_aside and old_uploads.exists():
                shutil.move(str(old_uploads), str(uploads_dir))
        except Exception:
            try:
                app.logger.exception("backup restore rollback failed")
            except Exception:
                pass
        raise
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    return str(dbfile)


def autosave_record(app, model_name: str, pk_value, payload: dict):
    """
    برای هر «سند» ذخیرهٔ JSON فشرده در autosave/
    """
    _, backup_dir, autosave_dir, _ = ensure_dirs(app)
    d = datetime.datetime.now()
    day_dir = autosave_dir / d.strftime("%Y-%m-%d") / model_name
    day_dir.mkdir(parents=True, exist_ok=True)
    safe_pk = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(pk_value))
    fn = f"{d.strftime('%H-%M-%S')}_{safe_pk}_{uuid.uuid4().hex[:8]}.json.gz"
    path = day_dir / fn

    def _sanitize(obj):
        """Recursively convert non-JSON-serializable objects into JSON-friendly types."""
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, (datetime.datetime, datetime.date)):
            try:
                return obj.isoformat()
            except Exception:
                return str(obj)
        if isinstance(obj, decimal.Decimal):
            try:
                return float(obj)
            except Exception:
                return str(obj)
        if isinstance(obj, (bytes, bytearray)):
            try:
                return obj.decode("utf-8")
            except Exception:
                return repr(obj)
        if isinstance(obj, dict):
            return {str(k): _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [_sanitize(v) for v in obj]
        if isinstance(obj, uuid.UUID):
            return str(obj)
        try:
            return str(obj)
        except Exception:
            return None

    safe_payload = _sanitize(payload)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(safe_payload, f, ensure_ascii=False, indent=2)
    try:
        touch_autosave_marker(app, d.isoformat(timespec="seconds"))
    except Exception:
        pass
    return str(path)

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
    ZIP کامل: DB + uploads/ (اختیاری) + کاربران + پرونده‌های سال مالی +
    سایر JSONهای runtime + metadata.
    """
    data_dir, backup_dir, autosave_dir, uploads_dir = ensure_dirs(app)
    stamp = now_stamp()
    out = backup_dir / f"backup_{stamp}_{uuid.uuid4().hex[:8]}.zip"

    users_path = data_dir / "users.json"
    fiscal_cases_dir = data_dir / "fiscal_cases"
    runtime_json = sorted(
        p for p in data_dir.glob("*.json")
        if p.is_file() and p.name not in {"users.json", "users.json.example"}
    )

    include_uploads = str(app.config.get("INCLUDE_UPLOADS_IN_BACKUP", "true")).lower() == "true"
    include_users = users_path.is_file()
    include_fiscal_cases = fiscal_cases_dir.exists()

    meta = {
        "format_version": 3,
        "created_at": stamp,
        "user": user,
        "reason": reason,
        "db_file": db_path(app).name,
        "include_uploads": include_uploads,
        "uploads_count": 0,
        "include_users_file": include_users,
        "include_fiscal_cases": include_fiscal_cases,
        "fiscal_case_files": 0,
        "include_runtime_json": bool(runtime_json),
        "runtime_json_files": 0,
        "app_version": app.config.get("APP_VERSION", "unknown"),
    }

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        dbfile = db_path(app)
        if dbfile.exists():
            fd, tmp_name = tempfile.mkstemp(prefix="hesabpak_backup_", suffix=".sqlite3")
            os.close(fd)
            temp_db = Path(tmp_name)
            try:
                if dbfile.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                    src_db = sqlite3.connect(str(dbfile))
                    dst_db = sqlite3.connect(str(temp_db))
                    try:
                        src_db.backup(dst_db)
                    finally:
                        dst_db.close()
                        src_db.close()
                else:
                    shutil.copy2(dbfile, temp_db)
                z.write(temp_db, arcname=f"db/{dbfile.name}")
            finally:
                try:
                    temp_db.unlink(missing_ok=True)
                except TypeError:
                    if temp_db.exists():
                        temp_db.unlink()

        if include_uploads and uploads_dir.exists():
            for root, dirs, files in os.walk(uploads_dir):
                for f in files:
                    p = Path(root) / f
                    z.write(p, arcname=str(p.relative_to(data_dir)))
                    meta["uploads_count"] += 1

        if include_users:
            z.write(users_path, arcname="runtime/users.json")

        for p in runtime_json:
            z.write(p, arcname=str(Path("runtime") / "json" / p.name))
            meta["runtime_json_files"] += 1

        if include_fiscal_cases:
            for root, dirs, files in os.walk(fiscal_cases_dir):
                for f in files:
                    p = Path(root) / f
                    rel = p.relative_to(fiscal_cases_dir)
                    z.write(p, arcname=str(Path("runtime") / "fiscal_cases" / rel))
                    meta["fiscal_case_files"] += 1

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


def _extract_archive_tree(z: zipfile.ZipFile, archive_prefix: str, staging_root: Path) -> bool:
    """Extract one archive directory safely into an isolated staging directory."""
    found = False
    prefix = archive_prefix.rstrip("/") + "/"
    for info in z.infolist():
        name = info.filename.replace("\\", "/")
        if not name.startswith(prefix) or info.is_dir():
            continue
        rel = _safe_archive_relative_path(name[len(prefix):])
        if not rel.parts:
            continue
        target = (staging_root / rel).resolve()
        root = staging_root.resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("مسیر عضو بکاپ خارج از پوشه مجاز است.") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        with z.open(info, "r") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        found = True
    return found


def _extract_backup_uploads(z: zipfile.ZipFile, staging_root: Path) -> bool:
    return _extract_archive_tree(z, "uploads", staging_root)


def restore_backup(app, zip_filename):
    """
    Restore the full runtime state stored by a backup.
    Every archive tree is staged and validated before live replacement.
    """
    data_dir, backup_dir, _, uploads_dir = ensure_dirs(app)
    zpath = resolve_backup_path(app, zip_filename)
    if not zpath.exists():
        raise FileNotFoundError("بکاپ پیدا نشد")

    dbfile = db_path(app)
    users_path = data_dir / "users.json"
    fiscal_cases_dir = data_dir / "fiscal_cases"

    temp_root = Path(tempfile.mkdtemp(prefix="hesabpak_restore_"))
    extracted_db = temp_root / dbfile.name
    staged_live_db = temp_root / (dbfile.name + ".restored")
    staged_uploads = temp_root / "uploads"
    runtime_stage = temp_root / "runtime"
    staged_runtime_json = runtime_stage / "json"
    staged_fiscal_cases = runtime_stage / "fiscal_cases"
    runtime_json_backup = temp_root / "runtime-json-before-restore"

    old_db = Path(str(dbfile) + ".before-restore")
    old_uploads = Path(str(uploads_dir) + ".before-restore")
    old_users = Path(str(users_path) + ".before-restore")
    old_fiscal_cases = Path(str(fiscal_cases_dir) + ".before-restore")

    live_db_had_previous = dbfile.is_file()
    uploads_had_previous = uploads_dir.exists()
    users_had_previous = users_path.is_file()
    fiscal_cases_had_previous = fiscal_cases_dir.exists()

    db_replaced = False
    uploads_replaced = False
    uploads_moved_aside = False
    users_replaced = False
    fiscal_cases_replaced = False
    fiscal_cases_moved_aside = False
    runtime_json_replaced = False
    runtime_json_moved_aside = False
    restore_uploads = False
    restore_users = False
    restore_fiscal_cases = False
    restore_runtime_json = False

    try:
        with zipfile.ZipFile(zpath, "r") as z:
            metadata = {}
            try:
                with z.open("metadata.json", "r") as meta_src:
                    metadata = json.load(meta_src)
            except Exception:
                metadata = {}

            expected_name = f"db/{dbfile.name}"
            db_inside = None
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

            include_uploads = str(metadata.get("include_uploads", "")).strip().lower() == "true"
            restore_uploads = include_uploads or any(
                info.filename.replace("\\", "/").startswith("uploads/") and not info.is_dir()
                for info in z.infolist()
            )
            if restore_uploads:
                staged_uploads.mkdir(parents=True, exist_ok=True)
                _extract_backup_uploads(z, staged_uploads)

            include_users = str(metadata.get("include_users_file", "")).strip().lower() == "true"
            include_fiscal_cases = str(metadata.get("include_fiscal_cases", "")).strip().lower() == "true"
            runtime_members_present = any(
                info.filename.replace("\\", "/").startswith("runtime/") and not info.is_dir()
                for info in z.infolist()
            )
            if runtime_members_present or include_users or include_fiscal_cases:
                _extract_archive_tree(z, "runtime", runtime_stage)

            staged_users = runtime_stage / "users.json"
            if include_users:
                if not staged_users.is_file():
                    raise RuntimeError("فایل users.json داخل بکاپ وجود ندارد.")
                restore_users = True
            elif staged_users.is_file():
                restore_users = True

            if include_fiscal_cases:
                staged_fiscal_cases.mkdir(parents=True, exist_ok=True)
                restore_fiscal_cases = True
            elif staged_fiscal_cases.exists():
                restore_fiscal_cases = True

            include_runtime_json = str(metadata.get("include_runtime_json", "")).strip().lower() == "true"
            if include_runtime_json:
                staged_runtime_json.mkdir(parents=True, exist_ok=True)
                restore_runtime_json = True
            elif staged_runtime_json.exists():
                restore_runtime_json = True

        if live_db_had_previous:
            shutil.copy2(dbfile, old_db)
        shutil.copy2(extracted_db, staged_live_db)
        os.replace(staged_live_db, dbfile)
        db_replaced = True

        if restore_uploads:
            if old_uploads.exists():
                if old_uploads.is_dir():
                    shutil.rmtree(old_uploads)
                else:
                    old_uploads.unlink()
            if uploads_had_previous and uploads_dir.exists():
                shutil.move(str(uploads_dir), str(old_uploads))
                uploads_moved_aside = True
            staged_uploads.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged_uploads), str(uploads_dir))
            uploads_replaced = True

        if restore_runtime_json:
            runtime_json_backup.mkdir(parents=True, exist_ok=True)
            current_json = {
                p.name: p for p in data_dir.glob("*.json")
                if p.is_file() and p.name != "users.json.example"
            }
            for name, p in current_json.items():
                shutil.copy2(p, runtime_json_backup / name)
            runtime_json_moved_aside = bool(current_json)
            runtime_json_replaced = True

            staged_json = {
                p.name: p for p in staged_runtime_json.glob("*.json")
                if p.is_file()
            }
            for name in set(current_json) - set(staged_json):
                (data_dir / name).unlink(missing_ok=True)
            for name, p in staged_json.items():
                os.replace(p, data_dir / name)

        if restore_users:
            if old_users.exists():
                old_users.unlink()
            if users_had_previous:
                shutil.copy2(users_path, old_users)
            os.replace(staged_users, users_path)
            users_replaced = True

        if restore_fiscal_cases:
            if old_fiscal_cases.exists():
                if old_fiscal_cases.is_dir():
                    shutil.rmtree(old_fiscal_cases)
                else:
                    old_fiscal_cases.unlink()
            if fiscal_cases_had_previous and fiscal_cases_dir.exists():
                shutil.move(str(fiscal_cases_dir), str(old_fiscal_cases))
                fiscal_cases_moved_aside = True
            staged_fiscal_cases.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged_fiscal_cases), str(fiscal_cases_dir))
            fiscal_cases_replaced = True

    except Exception:
        try:
            if db_replaced:
                if old_db.exists():
                    shutil.copy2(old_db, dbfile)
                elif not live_db_had_previous and dbfile.exists():
                    dbfile.unlink()

            if uploads_replaced and uploads_dir.exists():
                shutil.rmtree(uploads_dir)
            if uploads_moved_aside and old_uploads.exists():
                shutil.move(str(old_uploads), str(uploads_dir))

            if runtime_json_replaced:
                for p in data_dir.glob("*.json"):
                    if p.is_file() and p.name != "users.json.example":
                        p.unlink()
                if runtime_json_moved_aside:
                    for p in runtime_json_backup.glob("*.json"):
                        shutil.copy2(p, data_dir / p.name)

            if users_replaced:
                if users_path.exists():
                    users_path.unlink()
                if old_users.exists():
                    shutil.copy2(old_users, users_path)

            if fiscal_cases_replaced and fiscal_cases_dir.exists():
                shutil.rmtree(fiscal_cases_dir)
            if fiscal_cases_moved_aside and old_fiscal_cases.exists():
                shutil.move(str(old_fiscal_cases), str(fiscal_cases_dir))
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

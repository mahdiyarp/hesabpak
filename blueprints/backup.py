# blueprints/backup.py
import os
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Blueprint, render_template, request, send_from_directory, redirect, url_for, flash, current_app
from flask_login import login_required, current_user
from sqlalchemy import text

from models.backup_models import BackupLog, Setting
from extensions import db
from utils.backup_utils import create_full_backup, list_backups, restore_backup, ensure_dirs, read_autosave_marker
from utils.date_utils import parse_gregorian_date, parse_jalali_date, to_jdate_str, fa_digits

backup_bp = Blueprint("backup", __name__, template_folder="../templates")


def _year_key(value: Optional[str]) -> str:
    raw = (value or "").strip()
    if not raw:
        return "unknown"
    return raw.replace("/", "-").replace(" ", "-")


def _cases_dir() -> Path:
    base = Path(current_app.config.get("DATA_DIR", "data"))
    folder = base / "fiscal_cases"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _load_fiscal_years():
    raw = Setting.get("fiscal_years", "[]")
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        data = []

    cleaned = []
    if isinstance(data, list):
        for item in data:
            start = None
            label = None
            jalali = None
            key = None
            folder = None
            if isinstance(item, dict):
                start = item.get("start") or item.get("gregorian") or item.get("value") or item.get("date") or item.get("id")
                label = item.get("label") or item.get("title")
                jalali = item.get("jalali") or item.get("fa")
                key = item.get("key")
                folder = item.get("folder")
            else:
                start = str(item) if item is not None else None
            if not start:
                continue
            entry = {
                "start": str(start),
                "label": str(label) if label else str(start),
            }
            if jalali:
                entry["jalali"] = str(jalali)
            if key:
                entry["key"] = str(key)
            if folder:
                entry["folder"] = str(folder)
            cleaned.append(entry)
    elif data:
        cleaned.append({"start": str(data), "label": str(data)})

    seen = set()
    ordered = []
    for item in sorted(cleaned, key=lambda x: x["start"], reverse=True):
        if item["start"] in seen:
            continue
        seen.add(item["start"])
        if "key" not in item:
            jalali_display = item.get("jalali") or item.get("label")
            item["key"] = _year_key(jalali_display or item["start"])
        ordered.append(item)
    return ordered


def _save_fiscal_years(years):
    Setting.set("fiscal_years", json.dumps(years, ensure_ascii=False))


def _find_year_entry(years, start_value: Optional[str]):
    if not start_value:
        return None
    for item in years:
        if item.get("start") == start_value or item.get("gregorian") == start_value:
            return item
    return None


def _case_folder(year_entry) -> Path:
    key = _year_key((year_entry or {}).get("key") or (year_entry or {}).get("jalali") or (year_entry or {}).get("label"))
    folder = _cases_dir() / key
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _snapshot_current_year(years):
    """Persist master data of the active fiscal year into its case folder."""

    current_value = Setting.get("fiscal_year_current") or Setting.get("fiscal_year_start")
    current_entry = _find_year_entry(years, current_value)
    folder = _case_folder(current_entry or {"label": current_value or "current"})

    payload = {
        "exported_at": datetime.utcnow().isoformat(timespec="seconds"),
        "fiscal_year": current_value,
        "accounts": [],
        "entities": [],
        "cashboxes": [],
        "price_history": [],
    }

    accounts = db.session.execute(text(
        """
        SELECT a.code, a.name, a.level, a.locked,
               COALESCE(p.code, '') AS parent_code
        FROM accounts AS a
        LEFT JOIN accounts AS p ON p.id = a.parent_id
        ORDER BY a.level ASC, a.code ASC
        """
    )).mappings()
    payload["accounts"] = [
        {
            "code": row["code"],
            "name": row["name"],
            "level": row["level"],
            "locked": bool(row["locked"]),
            "parent_code": row["parent_code"] or None,
        }
        for row in accounts
    ]

    entities = db.session.execute(text(
        """
        SELECT e.type, e.code, e.name, e.unit, e.serial_no, e.level,
               COALESCE(p.code,'') AS parent_code,
               e.stock_qty, e.balance
        FROM entities AS e
        LEFT JOIN entities AS p ON p.id = e.parent_id
        ORDER BY e.type ASC, e.level ASC, e.code ASC
        """
    )).mappings()
    payload["entities"] = [
        {
            "type": row["type"],
            "code": row["code"],
            "name": row["name"],
            "unit": row["unit"],
            "serial_no": row["serial_no"],
            "level": row["level"],
            "parent_code": row["parent_code"] or None,
            "stock_qty": row["stock_qty"],
            "balance": row["balance"],
        }
        for row in entities
    ]

    cashboxes = db.session.execute(text(
        """
        SELECT name, kind, bank_name, account_no, iban, description, is_active
        FROM cash_boxes
        ORDER BY name ASC
        """
    )).mappings()
    payload["cashboxes"] = [dict(row) for row in cashboxes]

    prices = db.session.execute(text(
        """
        SELECT ph.last_price,
               person.code AS person_code,
               item.code AS item_code
        FROM price_history AS ph
        JOIN entities AS person ON person.id = ph.person_id
        JOIN entities AS item   ON item.id = ph.item_id
        WHERE person.type = 'person' AND item.type = 'item'
        """
    )).mappings()
    payload["price_history"] = [dict(row) for row in prices]

    with open(folder / "snapshot.json", "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    db_path = Path(current_app.config.get("DATA_DIR", "data")) / current_app.config.get("DB_FILE", "app.db")
    if db_path.exists():
        shutil.copy2(db_path, folder / "data.sqlite3")

    meta = {
        "saved_at": datetime.utcnow().isoformat(timespec="seconds"),
        "fiscal_year": current_value,
        "label": (current_entry or {}).get("label") or current_value,
    }
    with open(folder / "meta.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)

    return folder


def _load_snapshot_by_start(years, start_value: Optional[str]):
    entry = _find_year_entry(years, start_value)
    if not entry:
        return None
    folder = _case_folder(entry)
    snap = folder / "snapshot.json"
    if not snap.exists():
        return None
    try:
        with open(snap, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _apply_accounts(accounts_data):
    db.session.execute(text("DELETE FROM accounts"))
    db.session.flush()
    code_to_id: dict[str, int] = {}
    now = datetime.utcnow().isoformat(timespec="seconds")
    for row in sorted(accounts_data, key=lambda r: (r.get("level", 1), r.get("code") or "")):
        parent_id = code_to_id.get(row.get("parent_code"))
        db.session.execute(
            text(
                """
                INSERT INTO accounts (code, name, level, parent_id, locked, created_at, updated_at)
                VALUES (:code, :name, :level, :parent_id, :locked, :created_at, :updated_at)
                """
            ),
            {
                "code": row.get("code"),
                "name": row.get("name"),
                "level": row.get("level", 1),
                "parent_id": parent_id,
                "locked": 1 if row.get("locked") else 0,
                "created_at": now,
                "updated_at": now,
            },
        )
        new_id = db.session.execute(text("SELECT id FROM accounts WHERE code = :code"), {"code": row.get("code")}).scalar()
        if new_id:
            code_to_id[row.get("code")] = new_id


def _apply_entities(entities_data, *, keep_balances: bool):
    db.session.execute(text("DELETE FROM entities"))
    db.session.flush()
    now = datetime.utcnow().isoformat(timespec="seconds")
    code_to_id: dict[tuple[str, str], int] = {}
    for row in sorted(entities_data, key=lambda r: (r.get("type") or "", r.get("level", 1), r.get("code") or "")):
        parent_key = None
        if row.get("parent_code"):
            parent_key = (row.get("type"), row.get("parent_code"))
        parent_id = code_to_id.get(parent_key)
        stock_qty = row.get("stock_qty") if keep_balances else 0
        balance = row.get("balance") if keep_balances else 0
        db.session.execute(
            text(
                """
                INSERT INTO entities (type, code, name, unit, serial_no, parent_id, level, stock_qty, balance, created_at, updated_at)
                VALUES (:type, :code, :name, :unit, :serial_no, :parent_id, :level, :stock_qty, :balance, :created_at, :updated_at)
                """
            ),
            {
                "type": row.get("type"),
                "code": row.get("code"),
                "name": row.get("name"),
                "unit": row.get("unit"),
                "serial_no": row.get("serial_no"),
                "parent_id": parent_id,
                "level": row.get("level", 1),
                "stock_qty": stock_qty or 0,
                "balance": balance or 0,
                "created_at": now,
                "updated_at": now,
            },
        )
        new_id = db.session.execute(
            text("SELECT id FROM entities WHERE type = :type AND code = :code"),
            {"type": row.get("type"), "code": row.get("code")},
        ).scalar()
        if new_id:
            code_to_id[(row.get("type"), row.get("code"))] = new_id
    return code_to_id


def _apply_cashboxes(cashboxes_data):
    db.session.execute(text("DELETE FROM cash_boxes"))
    db.session.flush()
    now = datetime.utcnow().isoformat(timespec="seconds")
    for row in cashboxes_data:
        db.session.execute(
            text(
                """
                INSERT INTO cash_boxes (name, kind, bank_name, account_no, iban, description, is_active, created_at)
                VALUES (:name, :kind, :bank_name, :account_no, :iban, :description, :is_active, :created_at)
                """
            ),
            {
                "name": row.get("name"),
                "kind": row.get("kind", "cash"),
                "bank_name": row.get("bank_name"),
                "account_no": row.get("account_no"),
                "iban": row.get("iban"),
                "description": row.get("description"),
                "is_active": 1 if row.get("is_active", True) else 0,
                "created_at": now,
            },
        )


def _apply_price_history(price_data, *, code_map):
    db.session.execute(text("DELETE FROM price_history"))
    db.session.flush()
    now = datetime.utcnow().isoformat(timespec="seconds")
    for row in price_data:
        person_id = code_map.get(("person", row.get("person_code")))
        item_id = code_map.get(("item", row.get("item_code")))
        if not person_id or not item_id:
            continue
        db.session.execute(
            text(
                """
                INSERT INTO price_history (person_id, item_id, last_price, updated_at)
                VALUES (:person_id, :item_id, :last_price, :updated_at)
                """
            ),
            {
                "person_id": person_id,
                "item_id": item_id,
                "last_price": row.get("last_price", 0),
                "updated_at": now,
            },
        )


def _reset_transactions():
    for table in ["invoice_lines", "invoices", "cash_docs", "price_history", "audit_events"]:
        db.session.execute(text(f"DELETE FROM {table}"))
    db.session.execute(text("UPDATE entities SET stock_qty = 0, balance = 0"))


@backup_bp.route("/")
@login_required
def index():
    fiscal_years = _load_fiscal_years()
    current_value = Setting.get("fiscal_year_current") or Setting.get("fiscal_year_start")
    current_entry = _find_year_entry(fiscal_years, current_value)
    year_label = None
    if current_entry:
        year_label = current_entry.get("label") or current_entry.get("jalali")
    if not year_label and current_value:
        parsed = parse_gregorian_date(current_value, allow_none=True)
        if parsed:
            year_label = fa_digits(to_jdate_str(parsed))
    if not year_label:
        year_label = "نامشخص"

    year_key = (current_entry or {}).get("key") or _year_key(year_label if year_label != "نامشخص" else current_value)
    items = list_backups(current_app, year_key=year_key if year_label != "نامشخص" else None)
    last_auto = read_autosave_marker(current_app)
    case_info = {}
    for entry in fiscal_years:
        folder = _case_folder(entry)
        snapshot_path = folder / "snapshot.json"
        case_info[entry.get("start")] = {
            "snapshot": snapshot_path.exists(),
            "folder": str(folder),
        }
    return render_template(
        "backup/index.html",
        backups=items,
        fiscal_year_start=year_label,
        fiscal_years=fiscal_years,
        last_auto_backup=last_auto,
        case_info=case_info,
        current_year_key=year_key if year_label != "نامشخص" else None,
        current_year_value=current_value,
    )


@backup_bp.route("/create", methods=["POST"])
@login_required
def create():
    reason = request.form.get("reason", "manual")
    path = create_full_backup(current_app, user=getattr(current_user, "username", "admin"), reason=reason)
    size = os.path.getsize(path)
    log = BackupLog(user=getattr(current_user, "username", "admin"), reason=reason, filename=os.path.basename(path), size=size)
    db.session.add(log)

    fiscal_years = _load_fiscal_years()
    current_value = Setting.get("fiscal_year_current") or Setting.get("fiscal_year_start")
    current_entry = _find_year_entry(fiscal_years, current_value)
    year_label = (current_entry or {}).get("label") or (current_entry or {}).get("jalali") or current_value
    year_key = (current_entry or {}).get("key") or _year_key(year_label)

    data_dir, backup_dir, _, _ = ensure_dirs(current_app)
    year_dir = backup_dir / year_key if year_key else backup_dir
    year_dir.mkdir(parents=True, exist_ok=True)
    try:
        target_path = year_dir / os.path.basename(path)
        if Path(path) != target_path:
            shutil.move(path, target_path)
        log.filename = target_path.name
    except Exception as exc:
        current_app.logger.exception(f"Failed to move backup into fiscal folder: {exc}")

    db.session.commit()
    flash("✅ بکاپ کامل ساخته شد.", "success")
    return redirect(url_for("backup.index"))


@backup_bp.route("/download/<name>")
@login_required
def download(name):
    year = request.args.get("year")
    _, backup_dir, _, _ = ensure_dirs(current_app)
    candidates = []
    if year:
        candidates.append(backup_dir / year / name)
    candidates.append(backup_dir / name)
    for sub in backup_dir.iterdir():
        if sub.is_dir():
            candidates.append(sub / name)
    target = None
    for candidate in candidates:
        if candidate.exists():
            target = candidate
            break
    if not target:
        flash("فایل بکاپ موردنظر یافت نشد.", "danger")
        return redirect(url_for("backup.index"))
    return send_from_directory(directory=str(target.parent), path=target.name, as_attachment=True)


@backup_bp.route("/restore", methods=["POST"])
@login_required
def restore():
    name = request.form.get("name")
    year = request.form.get("year")
    if not name:
        flash("نام فایل بکاپ لازم است.", "danger")
        return redirect(url_for("backup.index"))
    _, backup_dir, _, _ = ensure_dirs(current_app)
    candidate_names = []
    if year:
        candidate_names.append(Path(year) / name)
    candidate_names.append(Path(name))
    for sub in backup_dir.iterdir():
        if sub.is_dir():
            candidate_names.append(sub.name + "/" + name)
    selected = None
    for rel in candidate_names:
        rel_path = Path(rel)
        full = backup_dir / rel_path
        if full.exists():
            selected = rel_path.as_posix()
            break
    if not selected:
        flash("فایل بکاپ موردنظر یافت نشد.", "danger")
        return redirect(url_for("backup.index"))
    try:
        restore_backup(current_app, selected)
        flash("♻️ ری‌استور انجام شد. لطفاً سرویس را ری‌استارت کنید تا کاملاً اعمال شود.", "warning")
    except Exception as e:
        current_app.logger.exception(e)
        flash(f"خطا در ری‌استور: {e}", "danger")
    return redirect(url_for("backup.index"))


@backup_bp.route("/new-year", methods=["POST"])
@login_required
def new_year():
    start_greg_raw = request.form.get("start_date")
    start_jalali = request.form.get("start_date_fa")
    start_greg = parse_gregorian_date(start_greg_raw, allow_none=True)
    if not start_greg:
        start_greg = parse_jalali_date(start_jalali, allow_none=False)
    if not start_greg:
        flash("تاریخ شروع سال مالی لازم است.", "danger")
        return redirect(url_for("backup.index"))

    jalali_label = fa_digits(start_jalali.strip()) if start_jalali else fa_digits(to_jdate_str(start_greg))
    start_iso = start_greg.isoformat()

    mode = request.form.get("mode", "reset")
    carry_from = request.form.get("carry_from")
    carry_options = set(request.form.getlist("carry_options"))

    path = create_full_backup(current_app, user=getattr(current_user, "username", "admin"), reason=f"pre-fiscal-{start_iso}")
    size = os.path.getsize(path)
    log_entry = BackupLog(user=getattr(current_user, "username", "admin"), reason=f"pre-fiscal-{start_iso}", filename=os.path.basename(path), size=size)
    db.session.add(log_entry)

    fiscal_years = _load_fiscal_years()

    current_value = Setting.get("fiscal_year_current") or Setting.get("fiscal_year_start")
    current_entry = _find_year_entry(fiscal_years, current_value)
    current_key = (current_entry or {}).get("key") or _year_key((current_entry or {}).get("label") or current_value)
    data_dir, backup_dir, _, _ = ensure_dirs(current_app)
    if current_key:
        year_dir = backup_dir / current_key
        year_dir.mkdir(parents=True, exist_ok=True)
        try:
            moved = year_dir / os.path.basename(path)
            if Path(path) != moved:
                shutil.move(path, moved)
            log_entry.filename = moved.name
        except Exception as exc:
            current_app.logger.exception(f"Failed moving pre-fiscal backup: {exc}")

    _snapshot_current_year(fiscal_years)

    _reset_transactions()

    snapshot = None
    if mode == "carry":
        if not carry_from:
            flash("لطفاً سال مرجع برای انتقال اطلاعات را انتخاب کنید.", "danger")
            db.session.rollback()
            return redirect(url_for("backup.index"))
        snapshot = _load_snapshot_by_start(fiscal_years, carry_from)
        if not snapshot:
            flash("پروندهٔ سال انتخابی برای انتقال یافت نشد.", "danger")
            db.session.rollback()
            return redirect(url_for("backup.index"))

    if snapshot:
        code_map = {}
        if "accounts" in carry_options and snapshot.get("accounts"):
            _apply_accounts(snapshot.get("accounts"))
        if "entities" in carry_options and snapshot.get("entities"):
            code_map = _apply_entities(snapshot.get("entities"), keep_balances=("balances" in carry_options))
        if "cashboxes" in carry_options and snapshot.get("cashboxes"):
            _apply_cashboxes(snapshot.get("cashboxes"))
        if "prices" in carry_options and snapshot.get("price_history") and code_map:
            _apply_price_history(snapshot.get("price_history"), code_map=code_map)
    else:
        db.session.execute(text("UPDATE entities SET stock_qty = 0, balance = 0"))

    Setting.set("fiscal_year_start", start_iso)
    Setting.set("fiscal_year_current", start_iso)
    Setting.set("fiscal_year_label", jalali_label)
    years = _load_fiscal_years()
    if not any(item["start"] == start_iso for item in years):
        years.append({
            "start": start_iso,
            "label": jalali_label,
            "jalali": jalali_label,
            "key": _year_key(jalali_label),
        })
    years = sorted(years, key=lambda x: x["start"], reverse=True)
    _save_fiscal_years(years)

    Setting.set("seq_invoice", "1")
    Setting.set("seq_voucher", "1")
    Setting.set("seq_purchase", "1")

    db.session.commit()

    flash_message = "✅ سال مالی جدید تنظیم شد."
    if mode == "carry":
        flash_message += " اطلاعات انتخاب‌شده منتقل گردید."
    else:
        flash_message += " شمارنده‌ها و مانده‌ها ریست شدند."

    flash(flash_message, "success")
    return redirect(url_for("backup.index"))


@backup_bp.route("/switch-year", methods=["POST"])
@login_required
def switch_year():
    year = request.form.get("year")
    years = _load_fiscal_years()
    valid_years = {item["start"] for item in years}

    if not year:
        flash("سال مالی انتخاب نشده است.", "danger")
        return redirect(url_for("backup.index"))

    if year not in valid_years:
        flash("سال مالی انتخاب‌شده معتبر نیست.", "danger")
        return redirect(url_for("backup.index"))

    Setting.set("fiscal_year_start", year)
    Setting.set("fiscal_year_current", year)
    entry = _find_year_entry(years, year)
    if entry and entry.get("label"):
        Setting.set("fiscal_year_label", entry.get("label"))
    db.session.commit()
    flash(f"✅ سال مالی {year} فعال شد.", "success")
    return redirect(url_for("backup.index"))

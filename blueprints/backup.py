# blueprints/backup.py
import os
import json
from flask import Blueprint, render_template, request, send_from_directory, redirect, url_for, flash, current_app
from flask_login import login_required, current_user
from models.backup_models import BackupLog, Setting
from extensions import db
from utils.backup_utils import create_full_backup, list_backups, restore_backup, ensure_dirs, read_autosave_marker

backup_bp = Blueprint("backup", __name__, template_folder="../templates")

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
            if isinstance(item, dict):
                start = item.get("start") or item.get("value") or item.get("date") or item.get("id")
                label = item.get("label") or item.get("title")
            else:
                start = str(item) if item is not None else None
            if not start:
                continue
            cleaned.append({
                "start": str(start),
                "label": str(label) if label else str(start),
            })
    elif data:
        # اگر قبلاً مقدار منفرد ذخیره شده باشد
        cleaned.append({"start": str(data), "label": str(data)})

    # حذف موارد تکراری با حفظ ترتیب (جدیدتر اول)
    seen = set()
    ordered = []
    for item in sorted(cleaned, key=lambda x: x["start"], reverse=True):
        if item["start"] in seen:
            continue
        seen.add(item["start"])
        ordered.append(item)
    return ordered


def _save_fiscal_years(years):
    Setting.set("fiscal_years", json.dumps(years, ensure_ascii=False))


@backup_bp.route("/")
@login_required
def index():
    items = list_backups(current_app)
    fy = Setting.get("fiscal_year_start", "نامشخص")
    last_auto = read_autosave_marker(current_app)
    fiscal_years = _load_fiscal_years()
    return render_template(
        "backup/index.html",
        backups=items,
        fiscal_year_start=fy,
        fiscal_years=fiscal_years,
        last_auto_backup=last_auto,
    )

@backup_bp.route("/create", methods=["POST"])
@login_required
def create():
    reason = request.form.get("reason", "manual")
    path = create_full_backup(current_app, user=getattr(current_user, "username", "admin"), reason=reason)
    size = os.path.getsize(path)
    log = BackupLog(user=getattr(current_user, "username", "admin"), reason=reason, filename=os.path.basename(path), size=size)
    db.session.add(log)
    db.session.commit()
    flash("✅ بکاپ کامل ساخته شد.", "success")
    return redirect(url_for("backup.index"))

@backup_bp.route("/download/<name>")
@login_required
def download(name):
    data_dir, backup_dir, _, _ = ensure_dirs(current_app)
    return send_from_directory(directory=str(backup_dir), path=name, as_attachment=True)

@backup_bp.route("/restore", methods=["POST"])
@login_required
def restore():
    name = request.form.get("name")
    if not name:
        flash("نام فایل بکاپ لازم است.", "danger")
        return redirect(url_for("backup.index"))
    try:
        restore_backup(current_app, name)
        flash("♻️ ری‌استور انجام شد. لطفاً سرویس را ری‌استارت کنید تا کاملاً اعمال شود.", "warning")
    except Exception as e:
        current_app.logger.exception(e)
        flash(f"خطا در ری‌استور: {e}", "danger")
    return redirect(url_for("backup.index"))

@backup_bp.route("/new-year", methods=["POST"])
@login_required
def new_year():
    """
    شروع سال مالی جدید:
    - بکاپ قبل از شروع
    - تنظیم تاریخ شروع سال مالی
    - ریست شمارنده‌ها (مثلاً شماره فاکتور/سند)
    - (اختیاری) ثبت اسناد افتتاحیه بر اساس مانده‌ها — این قسمت را بسته به مدل‌هایت می‌توانیم گسترش دهیم
    """
    start = request.form.get("start_date")  # "YYYY-MM-DD"
    if not start:
        flash("تاریخ شروع سال مالی لازم است.", "danger")
        return redirect(url_for("backup.index"))

    # 1) بکاپ ایمن
    path = create_full_backup(current_app, user=getattr(current_user, "username", "admin"), reason=f"pre-fiscal-{start}")
    size = os.path.getsize(path)
    db.session.add(BackupLog(user=getattr(current_user, "username", "admin"), reason=f"pre-fiscal-{start}", filename=os.path.basename(path), size=size))

    # 2) تنظیم Setting
    Setting.set("fiscal_year_start", start)
    Setting.set("fiscal_year_current", start)

    years = _load_fiscal_years()
    if not any(item["start"] == start for item in years):
        years.append({"start": start, "label": start})
    years = sorted(years, key=lambda x: x["start"], reverse=True)
    _save_fiscal_years(years)

    # 3) ریست شمارنده‌ها (اگر Settings برای شمارنده‌ها داری، اینجا صفر/۱ کن)
    # مثال:
    Setting.set("seq_invoice", "1")
    Setting.set("seq_voucher", "1")
    Setting.set("seq_purchase", "1")

    db.session.commit()
    flash("✅ سال مالی جدید تنظیم شد. شمارنده‌ها ریست شدند.", "success")
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
    db.session.commit()
    flash(f"✅ سال مالی {year} فعال شد.", "success")
    return redirect(url_for("backup.index"))

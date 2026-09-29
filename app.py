# -*- coding: utf-8 -*-
import os, json, logging, secrets, base64, tempfile
from pathlib import Path
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional

from flask import Flask, render_template, redirect, request, flash, session, jsonify, abort, current_app, send_from_directory
import subprocess, shlex, traceback
import shutil
from flask_login import LoginManager, login_user, logout_user, login_required, UserMixin, current_user
from dotenv import load_dotenv
from markupsafe import Markup, escape
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy import func, or_, UniqueConstraint, text, text   # <- مهم

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

from extensions import db
from utils.backup_utils import ensure_dirs, autosave_record, create_full_backup
from blueprints.backup import backup_bp
from autobackup import init_autobackup, register_autobackup_for
from models.backup_models import Setting, BackupLog, UserSettings
from utils.num_words_fa import amount_to_toman_words
from utils.date_utils import (
    to_jdate_str,
    now_info as date_now_info,
    parse_gregorian_date,
    parse_jalali_date,
    jalali_reference,
    fa_digits,
)
from utils import rates as rates_utils
from utils import bank_utils
from utils.secret_store import encrypt_secret, decrypt_secret, is_encrypted
import hashlib

# --- Simple append-only ledger for traceability (blockchain-like) ----------
class LedgerEntry(db.Model):
    __tablename__ = "ledger_entries"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    object_type = db.Column(db.String(64), nullable=False)
    object_id = db.Column(db.String(64), nullable=True)
    action = db.Column(db.String(64), nullable=False)
    payload = db.Column(db.Text, nullable=True)
    prev_hash = db.Column(db.String(128), nullable=True)
    hash = db.Column(db.String(128), nullable=False, unique=True, index=True)


def _compute_entry_hash(prev_hash: Optional[str], payload_text: str, ts_iso: str) -> str:
    m = hashlib.sha256()
    prev = (prev_hash or "")
    m.update(prev.encode("utf-8"))
    m.update(ts_iso.encode("utf-8"))
    m.update((payload_text or "").encode("utf-8"))
    return m.hexdigest()


def record_ledger(
    object_type: str,
    object_id: Optional[str],
    action: str,
    payload: Dict[str, Any],
) -> LedgerEntry:
    """Append a tamper-evident ledger entry with a serialized SQLite write."""
    conn = None
    try:
        # SQLite BEGIN IMMEDIATE serializes competing writers before the chain
        # head is read, preventing two concurrent requests from sharing prev_hash.
        conn = db.engine.connect()
        conn.exec_driver_sql("BEGIN IMMEDIATE")

        last = conn.execute(
            text("SELECT id, hash FROM ledger_entries ORDER BY id DESC LIMIT 1")
        ).first()
        prev = last.hash if last else None

        ts = datetime.utcnow().isoformat(timespec="microseconds")
        payload_text = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        digest = _compute_entry_hash(prev, payload_text, ts)

        result = conn.execute(
            text(
                """
                INSERT INTO ledger_entries
                    (created_at, object_type, object_id, action, payload, prev_hash, hash)
                VALUES
                    (:created_at, :object_type, :object_id, :action, :payload, :prev_hash, :hash)
                """
            ),
            {
                "created_at": datetime.utcnow(),
                "object_type": str(object_type or "unknown"),
                "object_id": str(object_id) if object_id is not None else None,
                "action": str(action or "unknown"),
                "payload": payload_text,
                "prev_hash": prev,
                "hash": digest,
            },
        )
        entry_id = result.lastrowid
        conn.exec_driver_sql("COMMIT")

        entry = db.session.get(LedgerEntry, entry_id)
        if entry is None:
            raise RuntimeError("رکورد ledger ایجاد شد اما قابل بازیابی نبود.")
        return entry
    except Exception:
        if conn is not None:
            try:
                conn.exec_driver_sql("ROLLBACK")
            except Exception:
                pass
        app.logger.exception("ledger record failed")
        raise
    finally:
        if conn is not None:
            conn.close()

# ----------------- Config -----------------
load_dotenv()
PROJECT_ROOT = Path(__file__).resolve().parent
VERSION_FILE = PROJECT_ROOT / "VERSION"
APP_VERSION = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else "0.9.0"
PORT = int(os.environ.get("PORT", "8000"))
FLASK_ENV = os.environ.get("FLASK_ENV", "development").strip().lower()
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
URL_PREFIX = os.environ.get("URL_PREFIX", "") or ""   # مثلا: /hesabpak
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
DATA_DIR = os.environ.get("DATA_DIR", "data")
CREDENTIAL_ENCRYPTION_KEY = os.environ.get("CREDENTIAL_ENCRYPTION_KEY", "").strip()
DEMO_MODE = os.environ.get("DEMO_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
DEMO_USERNAME = os.environ.get("DEMO_USERNAME", "demo").strip() or "demo"
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "demo123") or "demo123"

# Development may start with ephemeral credentials for local testing. Production
# must always receive explicit credentials from the environment/configuration.
if FLASK_ENV == "production":
    if not SECRET_KEY:
        raise RuntimeError("SECRET_KEY باید در محیط production تنظیم شود.")
    if not ADMIN_PASSWORD:
        # Existing installations may already have users.json, so do not break
        # startup when the admin credential has already been provisioned.
        _existing_users_file = Path(DATA_DIR).resolve() / "users.json"
        if not _existing_users_file.exists():
            raise RuntimeError("ADMIN_PASSWORD باید قبل از bootstrap مدیر در production تنظیم شود.")
elif not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)

if not ADMIN_PASSWORD and FLASK_ENV != "production":
    ADMIN_PASSWORD = "admin123"

ALLOWED_CMDS = {"ADD_ITEM","ADD_PERSON","RENAME","DELETE","SEED_ITEMS","SEED_ACCOUNTS"}

POS_DEVICE_CHOICES = [
    ("none", "بدون اتصال خودکار"),
    ("pax-s90", "PAX S90 (ایران کیش / به‌پرداخت)"),
    ("pax-s58", "PAX S58 (پرداخت الکترونیک سامان)"),
    ("pax-d210", "PAX D210 (به‌پرداخت ملت)"),
    ("verifone-vx520", "Verifone VX520 (پرداخت نوین آرین)"),
    ("saman-s2100", "Saman S2100 (سپاس)"),
]

THEME_CHOICES = [
    ("light", "روشن"),
    ("dark", "تاریک"),
    ("slate", "طوسی مورب"),
]

SEARCH_SORT_CHOICES = [
    ("recent", "جدیدترین"),
    ("code", "بر اساس کد"),
    ("name", "بر اساس نام"),
    ("balance", "بر اساس مانده/موجودی"),
]

PRICE_DISPLAY_MODES = [
    ("last", "آخرین قیمت"),
    ("average", "میانگین قیمت"),
]

DASHBOARD_WIDGET_CHOICES = [
    ("hero", "سربرگ"),
    ("stats", "کارت‌های آماری"),
    ("cash", "خلاصه صندوق"),
    ("charts", "نمودارها"),
    ("cheques", "چک‌های آتی"),
]

ASSISTANT_MODEL_CHOICES = [
    ("gpt-5.6-luna", "GPT-5.6 Luna (کم‌هزینه)"),
    ("gpt-5.6-terra", "GPT-5.6 Terra"),
    ("gpt-5.6-sol", "GPT-5.6 Sol"),
    ("gpt-4.1-mini", "GPT-4.1 mini"),
    ("gpt-4o-mini", "GPT-4o mini"),
]

# GPT-5.6 reasoning models should use the reasoning controls instead of sampling
# temperature. Older sampling-based models keep the temperature setting.
ASSISTANT_REASONING_MODELS = {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"}
ASSISTANT_MODEL_ALIASES = {
    # Previously saved model IDs migrate to the current low-cost default.
    "gpt-5-mini": "gpt-5.6-luna",
    "o4-mini": "gpt-5.6-luna",
}

CASH_METHOD_LABELS = {
    "cash": "نقدی",
    "pos": "دستگاه پوز",
    "bank": "بانکی",
    "cheque": "چک",
}

PERMISSION_LABELS = {
    "dashboard": "داشبورد",
    "sales": "فروش",
    "purchase": "خرید",
    "receive": "دریافت وجه",
    "payment": "پرداخت وجه",
    "reports": "گزارشات",
    "entities": "اشخاص و کالاها",
    "developer": "ابزار فنی",
    "admin": "مدیریت سیستم",
    "assistant": "دستیار هوشمند",
}

DEFAULT_PERMISSIONS = [
    "dashboard",
    "sales",
    "purchase",
    "receive",
    "payment",
    "reports",
    "entities",
    "assistant",
]

ADMIN_PERMISSIONS = sorted(set(DEFAULT_PERMISSIONS + ["developer", "admin"]))

USER_ROLE_LABELS = {
    "staff": "کاربر عادی",
    "limited": "کاربر محدود",
    "admin": "مدیر سیستم",
}

ASSIGNABLE_PERMISSIONS = [p for p in DEFAULT_PERMISSIONS]


def _permissions_for_role(role: str, requested) -> list:
    role = (role or "staff").strip().lower()
    if role == "admin":
        return ADMIN_PERMISSIONS
    allowed = []
    seen = set()
    for p in requested or []:
        if p in PERMISSION_LABELS and p in ASSIGNABLE_PERMISSIONS and p not in seen:
            allowed.append(p)
            seen.add(p)
    if role == "staff":
        base = sorted(allowed) if allowed else list(ASSIGNABLE_PERMISSIONS)
    else:
        base = sorted(allowed)
    if "dashboard" not in base:
        base.insert(0, "dashboard")
    return base

# ----------------- Flask & DB -----------------
app = Flask(__name__, static_url_path=(URL_PREFIX + "/static") if URL_PREFIX else "/static")


def _request_origin_is_trusted():
    """Allow same-origin requests and reject definite cross-origin state changes."""
    sec_fetch_site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if sec_fetch_site == "cross-site":
        return False

    origin = (request.headers.get("Origin") or "").strip()
    if not origin:
        return True

    try:
        from urllib.parse import urlparse
        parsed = urlparse(origin)
        origin_host = parsed.netloc.lower()
        request_host = request.host.lower()
        return bool(origin_host) and origin_host == request_host
    except Exception:
        return False


@app.before_request
def _csrf_origin_guard():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if request.endpoint in {"login"}:
        return None
    if not _request_origin_is_trusted():
        app.logger.warning(
            "CSRF_ORIGIN_REJECTED method=%s path=%s origin=%s sec_fetch_site=%s ip=%s",
            request.method,
            request.path,
            request.headers.get("Origin"),
            request.headers.get("Sec-Fetch-Site"),
            request.remote_addr,
        )
        abort(403)
    return None
app.config["SECRET_KEY"] = SECRET_KEY
app.config["CREDENTIAL_ENCRYPTION_KEY"] = CREDENTIAL_ENCRYPTION_KEY
app.config["DEMO_MODE"] = DEMO_MODE
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes", "on"}
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "15")) * 1024 * 1024


@app.after_request
def _security_headers(response):
    """Apply low-risk browser security headers to every response."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self' https:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'self'",
    )
    if request.is_secure:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response

if os.environ.get("FLASK_ENV", "").strip().lower() == "production":
    if SECRET_KEY == "change-me-please":
        raise RuntimeError("SECRET_KEY must be configured in production.")
    if ADMIN_PASSWORD == "admin123":
        raise RuntimeError("ADMIN_PASSWORD must be configured in production.")

DB_DIR = Path(DATA_DIR).resolve(); DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "hesabpak.sqlite3"
DB_URI = "sqlite:///" + str(DB_PATH).replace("\\", "/")
app.config["DATA_DIR"] = str(DB_DIR)
app.config["DB_FILE"] = DB_PATH.name
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)
print(f"[DB] Using: {DB_PATH}")
# Optionally start rates background updater on app startup if enabled via env
try:
    RATES_AUTO_START = os.environ.get('RATES_AUTO_START', '').strip().lower()
    if RATES_AUTO_START in ('1','true','yes','on'):
        try:
            rates_utils.start_background_updater(interval_seconds=int(os.environ.get('RATES_INTERVAL') or 60), run_on_start=True)
            app.logger.info('rates background updater started')
        except Exception:
            app.logger.exception('failed to start rates background updater')
except Exception:
    pass

USERS_FILE = str((DB_DIR / "users.json").resolve())
LOG_FILE   = str((DB_DIR / "activity.log").resolve())
# assistant uploads directory
ASSISTANT_UPLOAD_DIR = DB_DIR / "uploads" / "assistant"
ASSISTANT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.config["ASSISTANT_UPLOAD_DIR"] = str(ASSISTANT_UPLOAD_DIR)

# ----------------- Logging -----------------
class LocalTimeFormatter(logging.Formatter):
    converter = datetime.fromtimestamp
    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        return ct.strftime(datefmt or "%Y-%m-%d %H:%M:%S")

app.logger.setLevel(logging.INFO)
_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_handler.setFormatter(LocalTimeFormatter("%(asctime)s  %(levelname)s  %(message)s"))
app.logger.addHandler(_handler)

# ----------------- Models -----------------
class Account(db.Model):
    __tablename__ = "accounts"
    id        = db.Column(db.Integer, primary_key=True)
    code      = db.Column(db.String(16), nullable=False, unique=True, index=True)
    name      = db.Column(db.String(255), nullable=False)
    level     = db.Column(db.Integer, nullable=False, default=1)  # 1:3رقمی, 2:6رقمی, 3:9رقمی
    parent_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    locked    = db.Column(db.Boolean, nullable=False, default=False)
    created_at= db.Column(db.DateTime, nullable=False, default=datetime.now)
    updated_at= db.Column(db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    parent    = db.relationship("Account", remote_side=[id], lazy="joined")

class Entity(db.Model):
    __tablename__ = "entities"
    id        = db.Column(db.Integer, primary_key=True)
    type      = db.Column(db.String(16), nullable=False)     # person / item
    code      = db.Column(db.String(16), nullable=False, index=True)
    name      = db.Column(db.String(255), nullable=False, index=True)
    unit      = db.Column(db.String(64), nullable=True)      # برای person: خانم/آقا/شرکت/...
    serial_no = db.Column(db.String(255), nullable=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("entities.id"), nullable=True)
    level     = db.Column(db.Integer, nullable=False, default=1)
    created_at= db.Column(db.DateTime, nullable=False, default=datetime.now)
    updated_at= db.Column(db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
    stock_qty = db.Column(db.Float, nullable=False, default=0.0)   # فقط برای type=item معنی‌دار است
    balance   = db.Column(db.Float, nullable=False, default=0.0)   # فقط برای type=person

    parent    = db.relationship("Entity", remote_side=[id], lazy="joined")
    __table_args__ = (UniqueConstraint("type","code", name="uq_entity_type_code"),)

class Invoice(db.Model):
    __tablename__ = "invoices"
    id        = db.Column(db.Integer, primary_key=True)
    number    = db.Column(db.String(32), nullable=False, unique=True)
    date      = db.Column(db.Date, nullable=False)
    person_id = db.Column(db.Integer, db.ForeignKey("entities.id"), nullable=False)  # فقط type=person
    kind      = db.Column(db.String(16), nullable=False, default="sales")  # sales | purchase
    discount  = db.Column(db.Float, nullable=False, default=0.0)
    tax       = db.Column(db.Float, nullable=False, default=0.0)
    total     = db.Column(db.Float, nullable=False, default=0.0)
    created_at= db.Column(db.DateTime, nullable=False, default=datetime.now)

    person    = db.relationship("Entity", lazy="joined")

class InvoiceLine(db.Model):
    __tablename__ = "invoice_lines"
    id         = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    item_id    = db.Column(db.Integer, db.ForeignKey("entities.id"), nullable=False)  # فقط type=item
    qty        = db.Column(db.Float,  nullable=False, default=1.0)
    unit_price = db.Column(db.Float,  nullable=False, default=0.0)
    line_total = db.Column(db.Float,  nullable=False, default=0.0)

    invoice    = db.relationship("Invoice", backref=db.backref("lines", lazy=True))
    item       = db.relationship("Entity", lazy="joined")

class PriceHistory(db.Model):
    __tablename__ = "price_history"
    id        = db.Column(db.Integer, primary_key=True)
    person_id = db.Column(db.Integer, db.ForeignKey("entities.id"), nullable=False)
    item_id   = db.Column(db.Integer, db.ForeignKey("entities.id"), nullable=False)
    last_price= db.Column(db.Float, nullable=False, default=0.0)
    updated_at= db.Column(db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
    __table_args__ = (UniqueConstraint("person_id", "item_id", name="uq_price_person_item"),)

class CashBox(db.Model):
    __tablename__ = "cash_boxes"
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(128), nullable=False, unique=True)
    kind       = db.Column(db.String(16), nullable=False, default="cash")  # cash | bank
    bank_name  = db.Column(db.String(128), nullable=True)
    account_no = db.Column(db.String(64), nullable=True)
    iban       = db.Column(db.String(64), nullable=True)
    description= db.Column(db.String(255), nullable=True)
    is_active  = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class CashDoc(db.Model):
    __tablename__ = "cash_docs"
    id        = db.Column(db.Integer, primary_key=True)
    doc_type  = db.Column(db.String(16), nullable=False)  # receive | payment
    number    = db.Column(db.String(32), nullable=False, unique=True)
    date      = db.Column(db.Date, nullable=False)
    person_id = db.Column(db.Integer, db.ForeignKey("entities.id"), nullable=False)  # فقط type=person
    amount    = db.Column(db.Float, nullable=False, default=0.0)
    method    = db.Column(db.String(64), nullable=True)   # نقد، کارت، حواله...
    note      = db.Column(db.String(255), nullable=True)
    cashbox_id= db.Column(db.Integer, db.ForeignKey("cash_boxes.id"), nullable=True)
    cheque_number = db.Column(db.String(32), nullable=True, index=True)
    cheque_bank   = db.Column(db.String(128), nullable=True)
    cheque_branch = db.Column(db.String(128), nullable=True)
    cheque_account= db.Column(db.String(64), nullable=True)
    cheque_owner  = db.Column(db.String(128), nullable=True)
    cheque_due_date = db.Column(db.Date, nullable=True)
    created_at= db.Column(db.DateTime, nullable=False, default=datetime.now)

    person    = db.relationship("Entity", lazy="joined")
    cashbox   = db.relationship("CashBox", lazy="joined")

class AuditEvent(db.Model):
    __tablename__ = "audit_events"
    id         = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    user       = db.Column(db.String(64), nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    context    = db.Column(db.String(64), nullable=False)
    action     = db.Column(db.String(64), nullable=False)
    payload    = db.Column(db.Text, nullable=True)


class SiteView(db.Model):
    __tablename__ = "site_views"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    ip = db.Column(db.String(64), nullable=True)
    path = db.Column(db.String(255), nullable=False)
    method = db.Column(db.String(8), nullable=False)
    user = db.Column(db.String(64), nullable=True)


# --- Backup wiring (once) ---
ensure_dirs(app)
register_autobackup_for([Invoice, CashDoc])
init_autobackup(app)
app.register_blueprint(backup_bp, url_prefix=f"{URL_PREFIX}/backup")

# ----------------- Users bootstrap -----------------
if not os.path.exists(USERS_FILE):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "users": [
                    {
                        "username": ADMIN_USERNAME,
                        "password": generate_password_hash(ADMIN_PASSWORD) if ADMIN_PASSWORD else "",
                        "role": "admin",
                        "permissions": ADMIN_PERMISSIONS,
                        "is_active": True,
                        "email": os.environ.get("ADMIN_EMAIL", "").strip(),
                    }
                ]
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


def _password_is_hashed(password: str) -> bool:
    value = (password or "").strip()
    return value.startswith(("scrypt:", "pbkdf2:", "argon2:"))


def _normalize_user_entry(username: str, data: dict) -> dict:
    password = (data.get("password") or "").strip()
    email = (data.get("email") or "").strip()
    role = (data.get("role") or ("admin" if username == ADMIN_USERNAME else "staff")).strip()
    perms_raw = data.get("permissions")
    if not isinstance(perms_raw, (list, tuple, set)):
        perms_raw = []
    perms = _permissions_for_role(role, perms_raw)
    is_active = bool(data.get("is_active", True))
    return {
        "username": username,
        "password": password,
        "role": role or "staff",
        "permissions": perms,
        "is_active": is_active,
        "email": email,
    }


def load_users_catalog() -> dict:
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = {"users": []}

    catalog = {}
    for entry in raw.get("users", []):
        username = (entry or {}).get("username")
        if not username:
            continue
        catalog[username] = _normalize_user_entry(username, entry)

    if ADMIN_USERNAME not in catalog:
        catalog[ADMIN_USERNAME] = _normalize_user_entry(
            ADMIN_USERNAME,
            {
                "password": generate_password_hash(ADMIN_PASSWORD) if ADMIN_PASSWORD else "",
                "role": "admin",
                "permissions": ADMIN_PERMISSIONS,
                "is_active": True,
                        "email": os.environ.get("ADMIN_EMAIL", "").strip(),
            },
        )
    return catalog


def save_users_catalog(catalog: dict) -> None:
    """Atomically persist the user catalog and keep the credentials file private."""
    payload = {
        "users": [
            {
                "username": username,
                "password": (
                    data.get("password", "")
                    if _password_is_hashed(data.get("password", ""))
                    else generate_password_hash(data.get("password", "")) if data.get("password", "") else ""
                ),
                "role": data.get("role", "staff"),
                "permissions": _permissions_for_role(data.get("role", "staff"), data.get("permissions", [])),
                "is_active": bool(data.get("is_active", True)),
                "email": (data.get("email") or "").strip(),
            }
            for username, data in sorted(catalog.items(), key=lambda kv: kv[0].lower())
        ]
    }
    target = Path(USERS_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".users-", suffix=".json.tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except TypeError:
            if tmp_path.exists():
                tmp_path.unlink()

# ----------------- Auth -----------------
login_manager = LoginManager(app)
login_manager.login_view = "login"


class User(UserMixin):
    def __init__(
        self,
        username: str,
        *,
        role: str = "staff",
        permissions=None,
        is_active: bool = True,
        email: str = "",
    ):
        self.id = username
        self.username = username
        self.role = role or "staff"
        self.permissions = set(permissions or [])
        self._active = bool(is_active)
        self.email = (email or "").strip()

    def has_permission(self, perm: str) -> bool:
        if self.role == "admin":
            return True
        return perm in self.permissions

    @property
    def is_active(self) -> bool:  # type: ignore[override]
        return self._active


@login_manager.user_loader
def load_user(user_id):
    catalog = load_users_catalog()
    entry = catalog.get(user_id)
    if not entry or not entry.get("is_active", True):
        return None
    return User(
        user_id,
        role=entry.get("role", "staff"),
        permissions=entry.get("permissions", []),
        is_active=entry.get("is_active", True),
        email=entry.get("email", ""),
    )


def is_admin() -> bool:
    return current_user.is_authenticated and getattr(current_user, "role", "") == "admin"


def _active_admin_usernames(catalog: Dict[str, Any]) -> set:
    return {
        username
        for username, entry in (catalog or {}).items()
        if isinstance(entry, dict)
        and str(entry.get("role", "")).strip().lower() == "admin"
        and bool(entry.get("is_active", True))
    }


def _would_remove_last_active_admin(
    catalog: Dict[str, Any],
    username: str,
    *,
    new_role: Optional[str] = None,
    new_is_active: Optional[bool] = None,
    deleting: bool = False,
) -> bool:
    """Return True when the requested change would leave no active admin."""
    entry = (catalog or {}).get(username) or {}
    current_is_admin = (
        str(entry.get("role", "")).strip().lower() == "admin"
        and bool(entry.get("is_active", True))
    )
    if not current_is_admin:
        return False
    if deleting:
        becomes_active_admin = False
    else:
        role = str(new_role if new_role is not None else entry.get("role", "")).strip().lower()
        active = bool(new_is_active if new_is_active is not None else entry.get("is_active", True))
        becomes_active_admin = role == "admin" and active
    if becomes_active_admin:
        return False
    return len(_active_admin_usernames(catalog)) <= 1


def admin_required():
    if not is_admin():
        abort(403)


def user_permissions() -> set:
    if not current_user.is_authenticated:
        return set()
    if is_admin():
        return set(ADMIN_PERMISSIONS)
    return set(getattr(current_user, "permissions", set()))


def has_permission(perm: str) -> bool:
    if is_admin():
        return True
    return perm in user_permissions()


def ensure_permission(*perms: str) -> None:
    if not perms:
        return
    if not current_user.is_authenticated:
        abort(403)
    if is_admin():
        return
    allowed = user_permissions()
    if any(p in allowed for p in perms if p):
        return
    abort(403)

def human_duration_from_login():
    try:
        ts = session.get("login_at_utc")
        if not ts: return "—"
        start = datetime.fromisoformat(ts)
        delta: timedelta = datetime.utcnow() - start
        total_seconds = int(delta.total_seconds())
        h = total_seconds // 3600
        m = (total_seconds % 3600) // 60
        s = total_seconds % 60
        if h > 0: return f"{h:02d}:{m:02d}:{s:02d} ساعت"
        return f"{m:02d}:{s:02d} دقیقه"
    except Exception:
        return "—"

@app.context_processor
def inject_ctx():
    return {
        "prefix": URL_PREFIX,
        "logged_username": (current_user.username if current_user.is_authenticated else None),
        "login_duration": human_duration_from_login(),
        "is_admin": is_admin(),
        "current_user_role": getattr(current_user, "role", None),
        "user_permissions": sorted(user_permissions()),
        "has_permission": has_permission,
        "now_info": date_now_info(),
        "active_theme": _ui_theme_key(),
        "theme_choices": THEME_CHOICES,
        "search_sort_pref": _search_sort_key(),
        "search_sort_choices": SEARCH_SORT_CHOICES,
        "price_display_mode": _price_display_mode(),
        "price_display_modes": PRICE_DISPLAY_MODES,
        "dashboard_widgets": _dashboard_widgets(),
        "dashboard_widget_choices": DASHBOARD_WIDGET_CHOICES,
        "allow_negative_sales": _allow_negative_sales(),
        "demo_mode": DEMO_MODE,
    }

# === فیلتر جینجا برای جداکننده هزارگان ===
@app.template_filter('sep')
def sep_filter(val):
    try:
        f = float(val)
        if abs(f - int(f)) < 1e-9:
            return f"{int(f):,}"
        return f"{f:,.2f}"
    except Exception:
        return val


@app.template_filter('fa_digits')
def fa_digits_filter(val):
    try:
        return fa_digits(val)
    except Exception:
        return val


@app.template_filter('jdate')
def jdate_filter(val):
    return to_jdate_str(val)

# === کمک‌ها ===
def generate_invoice_number():
    last = db.session.query(Invoice).order_by(Invoice.id.desc()).first()
    if last and (last.number or "").isdigit():
        nxt = int(last.number) + 1
    else:
        nxt = (last.id + 1) if last else 1
    return f"{nxt:08d}"

def _to_float(x, default=0.0):
    try:
        if x is None:
            return default
        text = str(x).strip()
        text = text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
        text = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
        text = text.replace("٬", "").replace("،", "").replace(",", "")
        text = text.replace("٫", ".").replace(" ", "")
        if text == "":
            return default
        return float(text)
    except (TypeError, ValueError):
        return default

def _now_info():
    return date_now_info()

def _pos_device_config():
    key = Setting.get("pos_device", "none") or "none"
    label = dict(POS_DEVICE_CHOICES).get(key, POS_DEVICE_CHOICES[0][1])
    return key, label

def _ui_theme_key():
    key = (Setting.get("ui_theme", "light") or "light").strip().lower()
    valid = {k for k, _ in THEME_CHOICES}
    if key not in valid:
        key = "light"
    return key

def _search_sort_key():
    key = (Setting.get("search_sort", "recent") or "recent").strip().lower()
    valid = {k for k, _ in SEARCH_SORT_CHOICES}
    if key not in valid:
        key = "recent"
    return key

def _price_display_mode():
    key = (Setting.get("price_display_mode", "last") or "last").strip().lower()
    valid = {k for k, _ in PRICE_DISPLAY_MODES}
    if key not in valid:
        key = "last"
    return key

def _dashboard_widgets():
    raw = Setting.get("dashboard_widgets", "") or ""
    valid = [k for k, _ in DASHBOARD_WIDGET_CHOICES]
    try:
        data = json.loads(raw) if raw else []
        if not isinstance(data, list):
            data = []
    except Exception:
        data = []
    filtered = [k for k in data if k in valid]
    if not filtered:
        filtered = list(valid)
    return filtered

def _allow_negative_sales() -> bool:
    val = (Setting.get("allow_negative_sales", "off") or "off").strip().lower()
    return val in ("on", "true", "1", "yes")

def _assistant_model() -> str:
    key = (Setting.get("openai_model", ASSISTANT_MODEL_CHOICES[0][0]) or ASSISTANT_MODEL_CHOICES[0][0]).strip()
    key = ASSISTANT_MODEL_ALIASES.get(key, key)
    valid = {k for k, _ in ASSISTANT_MODEL_CHOICES}
    if key not in valid:
        key = ASSISTANT_MODEL_CHOICES[0][0]
    return key

def _openai_api_key() -> str:
    stored = (Setting.get("openai_api_key", "") or "").strip()
    key = decrypt_secret(current_app, stored) if stored else ""
    if not key:
        key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    return key


def _user_openai_api_key(user_settings) -> str:
    stored = (getattr(user_settings, "openai_api_key", "") or "").strip()
    return decrypt_secret(current_app, stored) if stored else ""


def _assistant_api_ready() -> bool:
    """Return whether the current user has an effective AI credential."""
    if OpenAI is None:
        return False
    if _openai_api_key():
        return True
    if not current_user.is_authenticated:
        return False
    username = getattr(current_user, "username", "")
    if not username:
        return False
    user_settings = UserSettings.query.filter_by(username=username).first()
    return bool(user_settings and _user_openai_api_key(user_settings))

def _csv_safe_cell(value: Any) -> str:
    """Prevent spreadsheet formula injection in exported text cells."""
    text_value = "" if value is None else str(value)
    stripped = text_value.lstrip()
    if stripped.startswith(("=", "+", "-", "@")):
        return "'" + text_value
    return text_value


def _mask_secret(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}{'*' * (len(value) - 6)}{value[-3:]}"

AI_PENDING_TASKS: Dict[str, Dict[str, Any]] = {}

AI_RESPONSE_SCHEMA = {
    "name": "hesabpak_assistant_response",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reply": {
                "type": "string",
                "description": "متن پاسخ به کاربر به زبان فارسی",
            },
            "needs_confirmation": {"type": "boolean"},
            "follow_up": {"type": ["string", "null"]},
            "uncertain_fields": {
                "type": "array",
                "items": {"type": "string"},
            },
            "invoice": {
                "type": ["null", "object"],
                "additionalProperties": False,
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["sales", "purchase", "unknown"],
                    },
                    "number": {"type": ["string", "null"]},
                    "date": {"type": ["string", "null"]},
                    "partner": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "code": {"type": ["string", "null"]},
                            "phone": {"type": ["string", "null"]},
                            "role": {"type": ["string", "null"]},
                        },
                        "required": ["name", "code", "phone", "role"],
                    },
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "name": {"type": "string"},
                                "code": {"type": ["string", "null"]},
                                "qty": {"type": "number"},
                                "unit": {"type": ["string", "null"]},
                                "unit_price": {"type": ["number", "null"]},
                                "total": {"type": ["number", "null"]},
                            },
                            "required": [
                                "name",
                                "code",
                                "qty",
                                "unit",
                                "unit_price",
                                "total",
                            ],
                        },
                    },
                    "notes": {"type": ["string", "null"]},
                },
                "required": [
                    "kind",
                    "number",
                    "date",
                    "partner",
                    "items",
                    "notes",
                ],
            },
            "cash": {
                "type": ["null", "object"],
                "additionalProperties": False,
                "properties": {
                    "doc_type": {
                        "type": "string",
                        "enum": ["receive", "payment", "unknown"],
                    },
                    "number": {"type": ["string", "null"]},
                    "date": {"type": ["string", "null"]},
                    "person": {
                        "type": ["null", "object"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "code": {"type": ["string", "null"]},
                        },
                        "required": ["name", "code"],
                    },
                    "amount": {"type": "number"},
                    "method": {"type": ["string", "null"]},
                    "bank_account": {"type": ["string", "null"]},
                    "bank_name": {"type": ["string", "null"]},
                    "cheque_number": {"type": ["string", "null"]},
                    "cheque_due": {"type": ["string", "null"]},
                },
                "required": [
                    "doc_type",
                    "number",
                    "date",
                    "person",
                    "amount",
                    "method",
                    "bank_account",
                    "bank_name",
                    "cheque_number",
                    "cheque_due",
                ],
            },
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["write_file", "append_file", "delete_path"],
                        },
                        "path": {"type": "string"},
                        "content": {"type": ["string", "null"]},
                        "description": {"type": ["string", "null"]},
                    },
                    "required": ["operation", "path", "content", "description"],
                },
            },
        },
        "required": [
            "reply",
            "needs_confirmation",
            "follow_up",
            "uncertain_fields",
            "invoice",
            "cash",
            "actions",
        ],
    },
}


def _cleanup_ai_tasks():
    expired = []
    now = datetime.utcnow()
    for token, data in AI_PENDING_TASKS.items():
        if data.get("expires_at") and data["expires_at"] < now:
            expired.append(token)
    for token in expired:
        AI_PENDING_TASKS.pop(token, None)

def _register_ai_task(username: str, payload: Dict[str, Any]) -> str:
    _cleanup_ai_tasks()
    token = secrets.token_hex(16)
    AI_PENDING_TASKS[token] = {
        "username": username,
        "payload": payload,
        "created_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(minutes=15),
    }
    return token

def _pop_ai_task(username: str, token: str) -> Optional[Dict[str, Any]]:
    _cleanup_ai_tasks()
    data = AI_PENDING_TASKS.get(token)
    if not data:
        return None
    if data.get("username") != username:
        return None
    AI_PENDING_TASKS.pop(token, None)
    return data.get("payload")

def _build_openai_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    prepared: List[Dict[str, Any]] = []
    for msg in messages:
        role = (msg.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
        text = (msg.get("text") or "").strip()
        attachments = msg.get("attachments") or []
        content: List[Dict[str, Any]] = []
        # Responses API input messages use input_text for user/assistant context.
        if text:
            content.append({"type": "input_text", "text": text})
        for att in attachments:
            atype = (att.get("type") or "").strip().lower()
            if atype != "image":
                continue
            data = (att.get("data") or "").strip()
            mime = (att.get("mime_type") or "image/png").strip() or "image/png"
            if not data:
                continue
            # Validate base64 to avoid invalid payloads
            try:
                base64.b64decode(data, validate=True)
            except Exception:
                continue
            # The Responses API variant in some deployments expects an image URL
            # rather than a bespoke 'image_base64' field. Use a data: URL which
            # is widely supported as an image_url fallback.
            # 'input_image' is commonly accepted, but if the client rejects it
            # we still provide the data URL which many assistants can consume.
            content.append({"type": "input_image", "image_url": f"data:{mime};base64,{data}"})
        if not content and not text:
            continue
        # attach the prepared content for this message
        prepared.append({"role": role, "content": content or [{"type": "input_text", "text": text or ""}]})
    return prepared


PROTECTED_PROJECT_PATHS = {".env", ".env.local", ".env.production", "data"}
PROTECTED_PROJECT_DIRS = {".git", ".hg", ".svn"}


def _resolve_project_path(rel_path: str) -> Path:
    rel_path = (rel_path or "").strip().replace("\\", "/")
    if not rel_path:
        raise ValueError("مسیر فایل مشخص نشده است.")
    rel = Path(rel_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("مسیر باید نسبی و داخل پروژه باشد.")
    if rel.parts and (rel.parts[0] in PROTECTED_PROJECT_DIRS or rel.parts[0] in PROTECTED_PROJECT_PATHS):
        raise ValueError("این مسیر برای ویرایش توسط دستیار محافظت شده است.")
    target = (PROJECT_ROOT / rel).resolve()
    if PROJECT_ROOT not in target.parents and target != PROJECT_ROOT:
        raise ValueError("امکان دسترسی به مسیر خارج از پروژه وجود ندارد.")
    return target


def _apply_assistant_actions(actions: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not is_admin():
        return {
            "applied": 0,
            "failed": len(actions or []),
            "messages": [],
            "errors": ["اعمال تغییرات فایل پروژه فقط برای مدیر سیستم مجاز است."],
        }
    summary = {"applied": 0, "failed": 0, "messages": [], "errors": []}
    for action in actions:
        if not isinstance(action, dict):
            summary["failed"] += 1
            summary["errors"].append("ساختار عملیات نامعتبر است.")
            continue
        operation = (action.get("operation") or action.get("type") or "").strip().lower()
        rel_path = (action.get("path") or "").strip()
        description = (action.get("description") or "").strip()
        label = description or rel_path or "عملیات فایل"
        if not operation:
            summary["failed"] += 1
            summary["errors"].append(f"{label}: نوع عملیات مشخص نشده است.")
            continue
        if not rel_path:
            summary["failed"] += 1
            summary["errors"].append(f"{label}: مسیر فایل مشخص نیست.")
            continue
        try:
            target = _resolve_project_path(rel_path)
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(f"{label}: {exc}")
            continue

        try:
            if operation == "write_file":
                content = action.get("content")
                if not isinstance(content, str):
                    raise ValueError("محتوای فایل موجود نیست.")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                summary["applied"] += 1
                message = description or f"فایل «{rel_path}» بروزرسانی شد."
            elif operation == "append_file":
                content = action.get("content")
                if not isinstance(content, str):
                    raise ValueError("محتوای فایل موجود نیست.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("a", encoding="utf-8") as fh:
                    fh.write(content)
                summary["applied"] += 1
                message = description or f"محتوا به فایل «{rel_path}» افزوده شد."
            elif operation == "delete_path":
                if target.is_dir():
                    raise ValueError("حذف پوشه مجاز نیست.")
                if target.exists():
                    target.unlink()
                summary["applied"] += 1
                message = description or f"فایل «{rel_path}» حذف شد."
            else:
                raise ValueError(f"عملیات ناشناخته: {operation}")

            summary["messages"].append(message)
            app.logger.info("AI_ACTION %s %s", operation.upper(), rel_path)
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(f"{label}: {exc}")
            app.logger.exception("AI_ACTION_FAILED operation=%s path=%s", operation or "?", rel_path)
    return summary


def _call_openai_assistant(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    # دریافت تنظیمات شخصی کاربر
    username = getattr(current_user, "username", "admin")
    user_settings = UserSettings.get_for_user(username)
    
    # استفاده از کلید API شخصی یا سراسری
    api_key = _user_openai_api_key(user_settings) or _openai_api_key()
    if not api_key:
        raise RuntimeError("کلید API تنظیم نشده است.")
    if OpenAI is None:
        raise RuntimeError("کتابخانه openai نصب نشده است.")

    # استفاده از مدل شخصی یا پیش‌فرض؛ مقادیر deprecated به alias فعلی نگاشت می‌شوند.
    model = (user_settings.openai_model or "").strip()
    model = ASSISTANT_MODEL_ALIASES.get(model, model)
    valid_models = {key for key, _ in ASSISTANT_MODEL_CHOICES}
    if model not in valid_models:
        model = _assistant_model()
    
    # استفاده از دستورالعمل سیستمی شخصی یا پیش‌فرض
    if user_settings.system_prompt:
        system_prompt = user_settings.system_prompt
    else:
        system_prompt = (
            "شما دستیار هوشمند حساب‌پاک هستید. وظیفه شما استخراج اطلاعات فاکتورهای خرید و فروش"
            " از متن یا تصویر و هدایت کاربر برای ثبت آن‌ها در سیستم است. همواره پاسخ نهایی را"
            " به زبان فارسی بدهید و در صورت ابهام، مواردی که نیاز به تأیید دارند را مشخص کنید."
            " اگر تصویر فاکتور دریافت کردید مقادیر تاریخ، شماره فاکتور، نام خریدار/فروشنده و"
            " اقلام را با قیمت و تعداد استخراج کنید. در صورت نبود قیمت، مقدار null قرار دهید."
            " در صورتی که کاربر درخواست ویرایش رابط کاربری یا کد برنامه را داشت، ابتدا راهکار"
            " را توضیح دهید و سپس در فیلد actions فهرستی از عملیات فایل را ارائه کنید. برای"
            " عملیات write_file محتوای کامل فایل هدف را ارسال کنید و مسیرها را نسبت به ریشه"
            " پروژه بنویسید."
        )

    client = OpenAI(api_key=api_key)

    request_messages = [
        {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]}
    ] + _build_openai_messages(messages)

    # استفاده از temperature شخصی برای مدل‌های sampling-based؛ reasoning models مقدار temperature نمی‌گیرند.
    temperature = user_settings.temperature if user_settings.temperature is not None else 0.7
    if not isinstance(temperature, (int, float)) or not 0 <= float(temperature) <= 2:
        temperature = 0.7

    # استفاده از max_tokens شخصی یا پیش‌فرض
    max_tokens = user_settings.max_tokens if user_settings.max_tokens else 800
    try:
        max_tokens = max(100, min(int(max_tokens), 16000))
    except (TypeError, ValueError):
        max_tokens = 800

    # Responses API uses text.format for Structured Outputs.
    # Keep a compatibility fallback for older SDK variants exposing
    # the legacy response_format keyword.
    response_format = {
        "format": {
            "type": "json_schema",
            "name": AI_RESPONSE_SCHEMA["name"],
            "schema": AI_RESPONSE_SCHEMA["schema"],
            "strict": True,
        }
    }
    request_kwargs = {
        "model": model,
        "input": request_messages,
        "max_output_tokens": max_tokens,
        "text": response_format,
    }
    if model not in ASSISTANT_REASONING_MODELS:
        request_kwargs["temperature"] = temperature

    try:
        try:
            response = client.responses.create(**request_kwargs)
        except TypeError:
            app.logger.warning("OpenAI client.responses.create() does not support text.format; trying legacy response_format")
            legacy_kwargs = {
                "model": model,
                "input": request_messages,
                "max_output_tokens": max_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": AI_RESPONSE_SCHEMA,
                },
            }
            if model not in ASSISTANT_REASONING_MODELS:
                legacy_kwargs["temperature"] = temperature
            response = client.responses.create(**legacy_kwargs)
    except Exception as e:
        # Log full exception for diagnostics and return a safe fallback reply
        app.logger.exception("OpenAI assistant request failed: %s", e)
        return {
            "reply": "خطا در برقراری ارتباط با سرویس پردازش هوش مصنوعی. لطفاً اتصال اینترنت یا تنظیمات API را بررسی کنید.",
            "needs_confirmation": False,
            "follow_up": None,
            "uncertain_fields": [],
            "actions": [],
            "invoice": None,
        }

    content: Optional[str] = None
    try:
        output_blocks = getattr(response, "output", None) or []
        if output_blocks:
            first_block = output_blocks[0]
            if isinstance(first_block, dict):
                block_content = first_block.get("content") or []
            else:
                block_content = getattr(first_block, "content", None) or []
            for part in block_content:
                # parts can be dicts or objects depending on client
                if isinstance(part, dict):
                    maybe_text = part.get("text") or part.get("content")
                else:
                    maybe_text = getattr(part, "text", None) or getattr(part, "content", None)
                if maybe_text:
                    content = maybe_text
                    break
    except Exception:
        app.logger.exception("failed to parse response.output for assistant reply")

    if not content:
        content = getattr(response, "output_text", None)

    if not content:
        app.logger.warning("empty response from OpenAI assistant")
        # Return a minimal fallback reply instead of raising, so caller can show
        # the assistant's lack of content gracefully.
        return {"reply": "", "needs_confirmation": False}

    # Try to parse JSON first. If it fails, attempt to extract a JSON substring
    # from the response text (common when the assistant includes explanation)
    try:
        return json.loads(content)
    except Exception as exc:
        app.logger.warning("failed to json-decode assistant content, trying to extract JSON: %s", exc)
        # Try to find a JSON object or array in the text
        try:
            import re

            m = re.search(r"(\{.*\}|\[.*\])", content, re.DOTALL)
            if m:
                candidate = m.group(1)
                try:
                    return json.loads(candidate)
                except Exception as exc2:
                    app.logger.warning("extracted JSON still invalid: %s", exc2)
        except Exception:
            app.logger.exception("error while trying to extract JSON from assistant content")

        # As a last resort, return the plain text as the assistant reply so the
        # application can continue (the UI will show the text). Include a few
        # safe defaults for other expected fields.
        app.logger.warning("Returning plain-text assistant reply as fallback; raw content: %s", content)
        return {
            "reply": content,
            "needs_confirmation": False,
            "follow_up": None,
            "uncertain_fields": [],
            "actions": [],
            "invoice": None,
        }

def _parse_invoice_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    parsed = parse_gregorian_date(raw, allow_none=True)
    if parsed:
        return parsed
    parsed = parse_jalali_date(raw, allow_none=True)
    return parsed

def _entity_level_from_code(code: str) -> int:
    return _level_by_code(code)

def _generate_entity_code(kind: str, preferred: Optional[str] = None) -> str:
    kind = (kind or "item").strip()
    if preferred and preferred.isdigit() and len(preferred) in (3, 6, 9):
        exists = Entity.query.filter_by(type=kind, code=preferred).first()
        if not exists:
            return preferred

    target_level = 1 if kind == "person" else 3
    prefix = ""
    if preferred and preferred.isdigit() and len(preferred) > 3:
        prefix = preferred[: len(preferred) - 3]
        target_level = len(preferred) // 3

    target_length = {1: 3, 2: 6, 3: 9}.get(target_level, 9)
    base_query = db.session.query(Entity.code).filter(Entity.type == kind)
    if prefix:
        base_query = base_query.filter(Entity.code.like(f"{prefix}%"))
    base_query = base_query.filter(func.length(Entity.code) == target_length)

    existing = set()
    for (code,) in base_query.all():
        try:
            suffix = code[len(prefix):len(prefix) + 3]
            existing.add(int(suffix))
        except Exception:
            continue

    candidate = 100
    while candidate in existing:
        candidate += 1
    code = f"{prefix}{candidate:03d}"
    while len(code) < target_length:
        code += "000"
    return code[:target_length]

def _resolve_entity(kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    name = (payload.get("name") or "").strip()
    code = (payload.get("code") or "").strip()
    entity = None
    if code and code.isdigit():
        entity = Entity.query.filter_by(type=kind, code=code).first()
    if not entity and name:
        entity = Entity.query.filter(Entity.type == kind, Entity.name.ilike(name)).first()
    info = {
        "name": name,
        "code": code if code and code.isdigit() else "",
        "entity": entity,
    }
    return info

def _assistant_plan_permission(plan: Dict[str, Any]) -> Optional[str]:
    """Return the module permission required to apply an assistant accounting plan."""
    if not isinstance(plan, dict):
        return None
    if plan.get("items"):
        kind = str(plan.get("kind") or "").strip().lower()
        return kind if kind in ("sales", "purchase") else None
    if plan.get("amount") is not None and plan.get("person"):
        doc_type = str(plan.get("doc_type") or "").strip().lower()
        return doc_type if doc_type in ("receive", "payment") else None
    return None


def _prepare_invoice_plan(invoice_data: Dict[str, Any]) -> Dict[str, Any]:
    kind = (invoice_data.get("kind") or "").strip().lower()
    if kind not in ("sales", "purchase"):
        raise ValueError("نوع فاکتور مشخص نیست.")

    partner_payload = invoice_data.get("partner") or {}
    partner_info = _resolve_entity("person", partner_payload)
    if not partner_info["name"] and not partner_info["entity"]:
        raise ValueError("طرف حساب فاکتور مشخص نیست.")

    raw_date = (invoice_data.get("date") or "").strip()
    parsed_date = _parse_invoice_date(raw_date)
    if raw_date and parsed_date is None:
        raise ValueError("تاریخ فاکتور معتبر نیست.")
    number = (invoice_data.get("number") or "").strip()

    items_preview = []
    missing_items = []
    total = 0.0
    for row in invoice_data.get("items") or []:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        qty = _to_float(row.get("qty"), 0.0)
        unit_price = _to_float(row.get("unit_price"), 0.0)
        if qty <= 0:
            raise ValueError(f"تعداد کالای «{name}» باید بزرگ‌تر از صفر باشد.")
        if unit_price < 0:
            raise ValueError(f"قیمت واحد کالای «{name}» نمی‌تواند منفی باشد.")
        unit = (row.get("unit") or "عدد").strip() or "عدد"
        code = (row.get("code") or "").strip()

        item_info = _resolve_entity("item", {"name": name, "code": code})
        line_total = qty * unit_price if unit_price else 0.0
        total += line_total

        preview_entry = {
            "name": name,
            "qty": qty,
            "unit_price": unit_price,
            "unit": unit,
            "code": code if code and code.isdigit() else "",
            "line_total": line_total,
            "entity_id": item_info["entity"].id if item_info["entity"] else None,
            "exists": bool(item_info["entity"]),
        }
        items_preview.append(preview_entry)

        if not item_info["entity"]:
            missing_items.append({
                "name": name,
                "unit": unit,
                "code": preview_entry["code"] or None,
                "qty": qty,
                "unit_price": unit_price,
            })

    if not items_preview:
        raise ValueError("فاکتور حداقل یک قلم کالا نیاز دارد.")

    plan = {
        "kind": kind,
        "number": number,
        "date": parsed_date.isoformat() if parsed_date else None,
        "partner": {
            "name": partner_info["name"],
            "code": partner_info["code"],
            "entity_id": partner_info["entity"].id if partner_info["entity"] else None,
            "exists": bool(partner_info["entity"]),
        },
        "items": items_preview,
        "missing_items": missing_items,
        "total": total,
    }

    if not partner_info["entity"]:
        plan["missing_partner"] = {
            "name": partner_info["name"],
            "code": partner_info["code"] or None,
        }

    return plan


def _prepare_cash_plan(cash_data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize assistant-provided cash/transfer payload into an internal plan."""
    doc_type = (cash_data.get("doc_type") or "").strip().lower()
    if doc_type not in ("receive", "payment"):
        raise ValueError("نوع سند دریافت/پرداخت مشخص نیست.")

    person_payload = cash_data.get("person") or {}
    person_info = _resolve_entity("person", person_payload)
    if not person_info["name"] and not person_info["entity"]:
        raise ValueError("طرف حساب سند مشخص نیست.")

    amount = _to_float(cash_data.get("amount"), 0.0)
    number = (cash_data.get("number") or "").strip()
    raw_date = (cash_data.get("date") or "").strip()
    parsed_date = _parse_invoice_date(raw_date)
    if raw_date and parsed_date is None:
        raise ValueError("تاریخ سند معتبر نیست.")
    method = (cash_data.get("method") or "").strip().lower() or None
    bank_account = (cash_data.get("bank_account") or "").strip() or None
    bank_name = (cash_data.get("bank_name") or "").strip() or None
    cheque_number = (cash_data.get("cheque_number") or "").strip() or None
    raw_cheque_due = (cash_data.get("cheque_due") or "").strip()
    cheque_due = _parse_invoice_date(raw_cheque_due)
    if raw_cheque_due and cheque_due is None:
        raise ValueError("تاریخ سررسید چک معتبر نیست.")

    plan = {
        "doc_type": doc_type,
        "number": number or None,
        "date": parsed_date.isoformat() if parsed_date else None,
        "person": {
            "name": person_info["name"],
            "code": person_info["code"] or None,
            "entity_id": person_info["entity"].id if person_info["entity"] else None,
            "exists": bool(person_info["entity"]),
        },
        "amount": float(amount or 0.0),
        "method": method,
        "bank_account": bank_account,
        "bank_name": bank_name,
        "cheque_number": cheque_number,
        "cheque_due": cheque_due.isoformat() if cheque_due else None,
    }

    if not person_info["entity"]:
        plan["missing_person"] = {"name": person_info["name"], "code": person_info["code"] or None}

    return plan


def _apply_cash_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a prepared cash plan: create CashDoc and update balances."""
    doc_type = (plan.get("doc_type") or "").strip().lower()
    if doc_type not in ("receive", "payment"):
        raise ValueError("نوع سند دریافت/پرداخت معتبر نیست.")

    person_entity = None
    if plan.get("person") and plan["person"].get("entity_id"):
        person_entity = Entity.query.get(int(plan["person"]["entity_id"]))
    if not person_entity:
        # create or fetch by name/code
        pname = plan.get("person", {}).get("name") or ""
        pcode = plan.get("person", {}).get("code") or ""
        person_info = _resolve_entity("person", {"name": pname, "code": pcode})
        if person_info.get("entity"):
            person_entity = person_info["entity"]
        else:
            # create person
            person_entity = _ensure_entity("person", {"name": pname, "code": pcode or None})

    # find matching cashbox by account_no, iban, or bank name
    cb = None
    acct = (plan.get("bank_account") or "")
    bname = (plan.get("bank_name") or "")
    if acct:
        cb = CashBox.query.filter((CashBox.account_no == acct) | (CashBox.iban == acct)).filter(CashBox.is_active == True).first()
    if not cb and bname:
        cb = CashBox.query.filter(CashBox.bank_name == bname, CashBox.is_active == True).first()

    # if method indicates cheque, set cheque fields
    cheque_number = plan.get("cheque_number")
    cheque_due = None
    raw_cheque_due = (plan.get("cheque_due") or "").strip()
    if raw_cheque_due:
        cheque_due = parse_gregorian_date(raw_cheque_due, allow_none=True) or parse_jalali_date(raw_cheque_due, allow_none=True)
        if cheque_due is None:
            raise ValueError("تاریخ سررسید چک معتبر نیست.")

    # generate number if missing
    number = plan.get("number") or None
    if not number:
        number = jalali_reference("RCV" if doc_type=="receive" else "PAY", datetime.utcnow())

    raw_date = (plan.get("date") or "").strip()
    date_val = None
    if raw_date:
        date_val = parse_gregorian_date(raw_date, allow_none=True) or parse_jalali_date(raw_date, allow_none=True)
        if date_val is None:
            raise ValueError("تاریخ سند معتبر نیست.")
    else:
        date_val = datetime.utcnow().date()

    amount = float(plan.get("amount") or 0.0)

    # validate amount must be positive
    if amount <= 0:
        raise ValueError("مبلغ باید بزرگ‌تر از صفر باشد.")

    # create doc
    doc = CashDoc(
        doc_type=doc_type,
        number=number,
        date=date_val,
        person_id=person_entity.id if person_entity else None,
        amount=amount,
        method=plan.get("method"),
        note=None,
        cashbox_id=cb.id if cb else None,
        cheque_number=cheque_number or None,
        cheque_due_date=cheque_due,
    )
    db.session.add(doc)

    # update person's balance same as receive/payment handlers
    try:
        if doc_type == "receive":
            person_entity.balance = float(person_entity.balance or 0.0) - float(amount)
        else:
            person_entity.balance = float(person_entity.balance or 0.0) + float(amount)
    except Exception:
        # best-effort
        if doc_type == "receive":
            person_entity.balance = -float(amount)
        else:
            person_entity.balance = float(amount)

    db.session.commit()
    # record ledger entry for cash doc creation
    try:
        ledger_payload = {
            "doc_id": doc.id,
            "number": doc.number,
            "doc_type": doc.doc_type,
            "amount": float(doc.amount or 0.0),
            "person_id": person_entity.id if person_entity else None,
            "cashbox_id": cb.id if cb else None,
        }
        try:
            record_ledger("cashdoc", doc.id, "create", ledger_payload)
        except Exception:
            app.logger.exception("failed to write cashdoc ledger entry")
    except Exception:
        app.logger.exception("failed to prepare cashdoc ledger payload")

    return {"doc": doc, "person": person_entity, "cashbox": cb}

def _ensure_entity(kind: str, data: Dict[str, Any]) -> Entity:
    name = (data.get("name") or "").strip()
    code = (data.get("code") or "").strip()
    unit = (data.get("unit") or ("عدد" if kind == "item" else "شرکت")).strip()
    if not name:
        raise ValueError("نام موجودیت مشخص نشده است.")

    existing = None
    if code and code.isdigit():
        existing = Entity.query.filter_by(type=kind, code=code).first()
    if not existing:
        existing = Entity.query.filter(Entity.type == kind, Entity.name == name).first()
    if existing:
        return existing

    final_code = _generate_entity_code(kind, code if code and code.isdigit() else None)
    level = _entity_level_from_code(final_code)
    parent_id = None
    if level == 2:
        parent = Entity.query.filter_by(type=kind, code=final_code[:3]).first()
        parent_id = parent.id if parent else None
    elif level == 3:
        parent = Entity.query.filter_by(type=kind, code=final_code[:6]).first()
        parent_id = parent.id if parent else None

    ent = Entity(type=kind, code=final_code, name=name, unit=unit, level=level, parent_id=parent_id)
    db.session.add(ent)
    db.session.flush()
    return ent

def _apply_invoice_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    kind = (plan.get("kind") or "").strip().lower()
    if kind not in ("sales", "purchase"):
        raise ValueError("نوع فاکتور معتبر نیست.")
    partner_payload = plan.get("partner") or {}
    partner_entity = None
    if partner_payload.get("entity_id"):
        partner_entity = Entity.query.get(int(partner_payload["entity_id"]))
    if not partner_entity:
        partner_entity = _ensure_entity("person", partner_payload)

    number = (plan.get("number") or "").strip()
    if not number:
        number = generate_invoice_number()
    if Invoice.query.filter_by(number=number).first():
        number = generate_invoice_number()

    raw_date = (plan.get("date") or "").strip()
    if raw_date:
        inv_date = _parse_invoice_date(raw_date)
        if inv_date is None:
            raise ValueError("تاریخ فاکتور معتبر نیست.")
    else:
        inv_date = datetime.utcnow().date()

    allow_negative = _allow_negative_sales()
    items_payload = []
    created_items = []
    for row in plan.get("items") or []:
        qty = _to_float(row.get("qty"), 0.0)
        if qty <= 0:
            continue
        unit_price = _to_float(row.get("unit_price"), 0.0)
        if unit_price < 0:
            raise ValueError(f"قیمت واحد کالای «{row.get('name') or 'نامشخص'}» نمی‌تواند منفی باشد.")
        unit = (row.get("unit") or "عدد").strip() or "عدد"
        item_entity = None
        if row.get("entity_id"):
            item_entity = Entity.query.get(int(row["entity_id"]))
        if not item_entity:
            item_entity = _ensure_entity("item", row)
            created_items.append(item_entity)

        current_stock = float(item_entity.stock_qty or 0.0)
        if kind == "sales" and not allow_negative and current_stock - qty < -1e-6:
            raise ValueError(f"موجودی کالا «{item_entity.name}» کافی نیست.")

        items_payload.append({
            "entity": item_entity,
            "qty": qty,
            "unit_price": unit_price,
            "unit": unit,
        })

    if not items_payload:
        raise ValueError("هیچ ردیف کالایی معتبر نیست.")

    inv = Invoice(number=number, date=inv_date, person_id=partner_entity.id, kind=kind, discount=0.0, tax=0.0, total=0.0)
    db.session.add(inv)
    db.session.flush()

    total = 0.0
    for payload in items_payload:
        item = payload["entity"]
        qty = float(payload["qty"])
        unit_price = float(payload["unit_price"])
        line_total = qty * unit_price
        total += line_total

        db.session.add(
            InvoiceLine(
                invoice_id=inv.id,
                item_id=item.id,
                qty=qty,
                unit_price=unit_price,
                line_total=line_total,
            )
        )

        if kind == "sales":
            _adjust_item_stock_delta(item, -qty, allow_negative=allow_negative)
            ph = PriceHistory.query.filter_by(person_id=partner_entity.id, item_id=item.id).first()
            if not ph:
                db.session.add(PriceHistory(person_id=partner_entity.id, item_id=item.id, last_price=unit_price))
            else:
                ph.last_price = unit_price
        else:
            _adjust_item_stock_delta(item, qty)

    # total must be positive
    if float(total) <= 0:
        db.session.rollback()
        raise ValueError("جمع کل فاکتور باید بزرگ‌تر از صفر باشد.")

    inv.total = total

    # Update the denormalized person balance atomically in the same transaction.
    _adjust_person_balance_delta(
        partner_entity,
        float(total) if kind == "sales" else -float(total),
    )

    db.session.commit()

    # record ledger entry for invoice creation (append-only)
    try:
        ledger_lines = [
            {"item_id": int(p["entity"].id), "qty": float(p["qty"]), "unit_price": float(p["unit_price"]) }
            for p in items_payload
        ]
        ledger_payload = {
            "invoice_id": inv.id,
            "number": inv.number,
            "kind": inv.kind,
            "total": float(inv.total or 0.0),
            "partner_id": partner_entity.id if partner_entity else None,
            "lines": ledger_lines,
        }
        try:
            record_ledger("invoice", inv.id, "create", ledger_payload)
        except Exception:
            app.logger.exception("failed to write invoice ledger entry")
    except Exception:
        app.logger.exception("failed to prepare invoice ledger payload")

    return {
        "invoice": inv,
        "partner": partner_entity,
        "created_items": created_items,
    }


def _find_entity_by_code_or_id(kind: str, code_or_id: str):
    if not code_or_id: return None
    q = Entity.query.filter(Entity.type == kind)
    if code_or_id.isdigit() and len(code_or_id) > 3:
        by_id = Entity.query.filter_by(id=int(code_or_id), type=kind).first()
        if by_id:
            return by_id
    return q.filter(Entity.code == code_or_id).first()

_SENSITIVE_LOG_QUERY_KEYS = {
    "password", "pass", "token", "secret", "api_key", "apikey", "key", "authorization",
}


def _safe_request_args_for_log() -> dict:
    safe = {}
    for key, value in request.args.items():
        normalized = str(key).strip().lower()
        if normalized in _SENSITIVE_LOG_QUERY_KEYS or any(
            marker in normalized for marker in ("password", "token", "secret", "api_key", "apikey")
        ):
            safe[str(key)] = "***"
        else:
            safe[str(key)] = str(value)[:256]
    return safe


@app.before_request
def _req_log():
    safe_args = _safe_request_args_for_log()
    if current_user.is_authenticated:
        app.logger.info(f"USER={current_user.username}  IP={request.remote_addr}  {request.method} {request.path}  ARGS={safe_args}")
    else:
        app.logger.info(f"ANON  IP={request.remote_addr}  {request.method} {request.path}  ARGS={safe_args}")
    # record site view for analytics
    try:
        sv = SiteView(ip=request.headers.get('X-Forwarded-For', request.remote_addr or ''), path=request.path, method=request.method, user=(getattr(current_user, 'username', None) if current_user and getattr(current_user, 'is_authenticated', False) else None))
        db.session.add(sv)
        db.session.commit()
    except Exception:
        db.session.rollback()

def _level_by_code(code: str) -> int:
    L = len(code or "")
    if L == 3: return 1
    if L == 6: return 2
    if L == 9: return 3
    return 0


def _suggest_next_entity_code(e_type: str) -> str:
    """Suggest the next available numeric code for entities of type e_type.

    Rules implemented:
    - Prefer 3-digit codes starting at 100 and increasing (100..999).
    - If no 3-digit free code, try 6-digit codes starting at 100001 and increasing
      (this naturally produces sequences like 100001, 100002, ...).
    - If still none, try 9-digit codes starting at 100000001 and increasing.

    This mirrors the repo's 3/6/9 grouping and matches examples like
    100 -> 101 and 100001 -> 100002.
    """
    try:
        existing = {str(e.code) for e in Entity.query.filter_by(type=e_type).all()}
    except Exception:
        existing = set()

    # 3-digit
    for n in range(100, 1000):
        s = f"{n:03d}"
        if s not in existing:
            return s

    # 6-digit (avoid suffix 000; start at 100001)
    for n in range(100001, 1_000_000):
        s = f"{n:06d}"
        if s not in existing:
            return s

    # 9-digit (avoid suffix 000000000; start at 100000001)
    for n in range(100000001, 1_000_000_000):
        s = f"{n:09d}"
        if s not in existing:
            return s

    # Fallback: random numeric string (very unlikely to reach here)
    return "100"

def validate_entity_form(form, for_update_id=None):
    e_type = (form.get("type") or "").strip()         # person | item
    code   = (form.get("code") or "").strip()
    name   = (form.get("name") or "").strip()
    unit   = (form.get("unit") or "").strip() or None
    serial = (form.get("serial_no") or "").strip() or None

    errors = []
    if e_type not in ("person","item"):
        errors.append("نوع نامعتبر است.")
    if not code.isdigit():
        errors.append("کد باید فقط عدد باشد.")

    lvl = _level_by_code(code)
    if lvl == 0:
        errors.append("طول کد باید یکی از 3، 6 یا 9 رقم باشد.")

    q = Entity.query.filter_by(type=e_type, code=code)
    if for_update_id:
        q = q.filter(Entity.id != for_update_id)
    exists = q.first()
    if exists:
        et = "شخص" if exists.type == "person" else "کالا"
        errors.append(f"این کد قبلاً برای {et} «{exists.name}» استفاده شده است.")

    qn = Entity.query.filter_by(type=e_type, name=name)
    if for_update_id:
        qn = qn.filter(Entity.id != for_update_id)
    if qn.first():
        errors.append("نام در این نوع تکراری است.")

    if lvl == 1:
        child6 = Entity.query.filter(Entity.type==e_type, Entity.level==2, Entity.code.like(f"{code}%")).first()
        if child6:
            errors.append(f"کد {code} قبلاً سرشاخه شده (زیرفرع دارد). لطفاً یکی از زیرفرع‌ها را انتخاب/تعریف کنید.")
    elif lvl == 2:
        child9 = Entity.query.filter(Entity.type==e_type, Entity.level==3, Entity.code.like(f"{code}%")).first()
        if child9:
            errors.append(f"کد {code} سرشاخهٔ زیرفرع‌های ۹رقمی است و قابل ثبت به‌عنوان آیتم نیست.")

    parent = None
    if lvl == 2:
        pcode = code[:3]
        parent = Entity.query.filter_by(type=e_type, code=pcode).first()
    elif lvl == 3:
        pcode = code[:6]
        parent = Entity.query.filter_by(type=e_type, code=pcode).first()

    return errors, dict(e_type=e_type, code=code, name=name, unit=unit, serial=serial,
                        parent=parent, level=lvl)

# ----------------- Routes: main -----------------
@app.route(URL_PREFIX + "/")
@login_required
def index():
    ensure_permission("dashboard")
    now = _now_info()
    today = now["datetime"].date()
    horizon = today + timedelta(days=3)

    inv_stats = (
        db.session.query(
            func.count(Invoice.id),
            func.coalesce(func.sum(Invoice.total), 0.0),
        )
        .filter(Invoice.date == today)
        .first()
    ) or (0, 0.0)
    today_invoice_count = int(inv_stats[0] or 0)
    # split today's totals into sales vs purchases using number prefix heuristic
    today_invoice_total = float(inv_stats[1] or 0.0)
    today_sales_total = float(
        db.session.query(func.coalesce(func.sum(Invoice.total), 0.0))
        .filter(Invoice.date == today, Invoice.kind == 'sales')
        .scalar() or 0.0
    )
    today_purchase_total = float(
        db.session.query(func.coalesce(func.sum(Invoice.total), 0.0))
        .filter(Invoice.date == today, Invoice.kind == 'purchase')
        .scalar() or 0.0
    )

    def _sum_cash(doc_type, dt=today):
        return float(
            db.session.query(func.coalesce(func.sum(CashDoc.amount), 0.0))
            .filter(CashDoc.doc_type == doc_type, CashDoc.date == dt)
            .scalar()
            or 0.0
        )

    today_receives_total = _sum_cash("receive")
    today_payments_total = _sum_cash("payment")

    # Dashboard: show a single aggregated row for all active cashboxes (unified view)
    active_ids = [b.id for b in CashBox.query.filter_by(is_active=True).all()]
    q_recv = db.session.query(func.coalesce(func.sum(CashDoc.amount), 0.0)).filter(CashDoc.doc_type == "receive")
    q_pay  = db.session.query(func.coalesce(func.sum(CashDoc.amount), 0.0)).filter(CashDoc.doc_type == "payment")
    if active_ids:
        q_recv = q_recv.filter(CashDoc.cashbox_id.in_(active_ids))
        q_pay  = q_pay.filter(CashDoc.cashbox_id.in_(active_ids))
    receive_sum = float(q_recv.scalar() or 0.0)
    payment_sum = float(q_pay.scalar() or 0.0)
    method_balances = [
        {
            "method": "all",
            "label": "همه صندوق‌ها",
            "receive": receive_sum,
            "payment": payment_sum,
            "balance": receive_sum - payment_sum,
        }
    ]

    due_date_expr = func.coalesce(CashDoc.cheque_due_date, CashDoc.date)
    upcoming_receive_cheques = (
        CashDoc.query.filter(
            CashDoc.doc_type == "receive",
            func.lower(func.coalesce(CashDoc.method, "")) == "cheque",
            due_date_expr >= today,
            due_date_expr <= horizon,
        )
        .order_by(due_date_expr.asc())
        .all()
    )

    upcoming_payment_cheques = (
        CashDoc.query.filter(
            CashDoc.doc_type == "payment",
            func.lower(func.coalesce(CashDoc.method, "")) == "cheque",
            due_date_expr >= today,
            due_date_expr <= horizon,
        )
        .order_by(due_date_expr.asc())
        .all()
    )

    def cheque_to_dict(doc: CashDoc):
        due_dt = doc.cheque_due_date or doc.date
        return {
            "id": doc.id,
            "number": doc.number,
            "person": doc.person.name if doc.person else "—",
            "amount": float(doc.amount or 0.0),
            "date": to_jdate_str(due_dt) if due_dt else "—",
            "cheque_number": doc.cheque_number,
        }

    chart_days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    chart_labels = [to_jdate_str(d) for d in chart_days]

    # build per-day sales and purchase totals and invoice counts
    inv_rows = (
        db.session.query(Invoice.date, Invoice.kind, func.coalesce(Invoice.total, 0.0))
        .filter(Invoice.date >= chart_days[0], Invoice.date <= today)
        .all()
    )
    sales_total_map = {}
    purchase_total_map = {}
    sales_count_map = {}
    for dt, kind, total in inv_rows:
        try:
            total_val = float(total or 0.0)
        except Exception:
            total_val = 0.0
        if (kind or '') == 'sales':
            sales_total_map[dt] = float(sales_total_map.get(dt, 0.0) + total_val)
            sales_count_map[dt] = int(sales_count_map.get(dt, 0) + 1)
        else:
            purchase_total_map[dt] = float(purchase_total_map.get(dt, 0.0) + total_val)

    cash_rows = (
        db.session.query(
            CashDoc.date,
            CashDoc.doc_type,
            func.coalesce(func.sum(CashDoc.amount), 0.0),
        )
        .filter(CashDoc.date >= chart_days[0], CashDoc.date <= today)
        .group_by(CashDoc.date, CashDoc.doc_type)
        .order_by(CashDoc.date)
        .all()
    )
    receive_map = {}
    payment_map = {}
    for dt, doc_type, total in cash_rows:
        total_val = float(total or 0.0)
        if doc_type == "receive":
            receive_map[dt] = total_val
        elif doc_type == "payment":
            payment_map[dt] = total_val

    chart_sales_totals = [sales_total_map.get(day, 0.0) for day in chart_days]
    chart_purchase_totals = [purchase_total_map.get(day, 0.0) for day in chart_days]
    chart_invoice_counts = [sales_count_map.get(day, 0) for day in chart_days]
    chart_receives_totals = [receive_map.get(day, 0.0) for day in chart_days]
    chart_payments_totals = [payment_map.get(day, 0.0) for day in chart_days]

    return render_template(
        "dashboard.html",
        prefix=URL_PREFIX,
        now=now,
        today_stats={
            "invoice_count": today_invoice_count,
            "invoice_total": today_invoice_total,
            "sales_total": today_sales_total,
            "purchase_total": today_purchase_total,
            "receives_total": today_receives_total,
            "payments_total": today_payments_total,
            "net_cash": today_receives_total - today_payments_total,
        },
        cash_balances=method_balances,
        incoming_cheques=[cheque_to_dict(doc) for doc in upcoming_receive_cheques],
        outgoing_cheques=[cheque_to_dict(doc) for doc in upcoming_payment_cheques],
        chart_data={
            "labels": chart_labels,
            "salesTotals": chart_sales_totals,
            "purchaseTotals": chart_purchase_totals,
            "invoiceCounts": chart_invoice_counts,
            "receivesTotals": chart_receives_totals,
            "paymentsTotals": chart_payments_totals,
        },
        dashboard_widgets=_dashboard_widgets(),
        assistant_model_label=dict(ASSISTANT_MODEL_CHOICES).get(_assistant_model(), _assistant_model()),
        api_ready=_assistant_api_ready(),
    )

@app.route(URL_PREFIX + "/sw.js", methods=["GET"])
def service_worker():
    """Serve the PWA worker from app root so it can control the whole app scope."""
    return send_from_directory(PROJECT_ROOT, "sw.js", mimetype="application/javascript")


@app.route(URL_PREFIX + "/healthz", methods=["GET"])
def healthz():
    """Lightweight deployment health probe; never exposes application data."""
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok", "version": APP_VERSION}), 200
    except Exception:
        db.session.rollback()
        return jsonify({"status": "error", "version": APP_VERSION}), 503


@app.route(URL_PREFIX + "/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(URL_PREFIX + "/")
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        catalog = load_users_catalog()
        entry = catalog.get(username)
        stored_password = (entry.get("password") or "") if entry else ""
        password_ok = False
        if stored_password:
            try:
                password_ok = check_password_hash(stored_password, password) if _password_is_hashed(stored_password) else stored_password == password
            except (ValueError, TypeError):
                password_ok = False
        if password_ok:
            if not entry.get("is_active", True):
                flash("دسترسی این کاربر غیرفعال شده است.", "danger")
                return redirect(URL_PREFIX + "/login")

            # Upgrade legacy plaintext passwords after a successful login.
            if not _password_is_hashed(stored_password):
                catalog[username]["password"] = generate_password_hash(password)
                save_users_catalog(catalog)
            login_user(
                User(
                    username,
                    role=entry.get("role", "staff"),
                    permissions=entry.get("permissions", []),
                    is_active=entry.get("is_active", True),
                    email=entry.get("email", ""),
                )
            )
            session["login_at_utc"] = datetime.utcnow().isoformat()
            flash("ورود موفق", "success")
            app.logger.info(f"LOGIN  USER={username}  IP={request.remote_addr}")
            return redirect(URL_PREFIX + "/")
        flash("نام کاربری یا رمز عبور اشتباه است.", "danger")
        app.logger.warning(f"LOGIN_FAIL  USER={username}  IP={request.remote_addr}")
    return render_template("login.html", prefix=URL_PREFIX)

def _reset_activity_log_file() -> None:
    """Truncate the demo log without leaving the old file descriptor active."""
    global _handler
    log_target = str(Path(LOG_FILE).resolve())
    for handler in list(app.logger.handlers):
        if isinstance(handler, logging.FileHandler):
            try:
                if str(Path(handler.baseFilename).resolve()) == log_target:
                    app.logger.removeHandler(handler)
                    handler.close()
            except (OSError, ValueError):
                pass
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(LOG_FILE).write_text("", encoding="utf-8")
    _handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _handler.setFormatter(LocalTimeFormatter("%(asctime)s  %(levelname)s  %(message)s"))
    app.logger.addHandler(_handler)


def _seed_demo_data():
    if not DEMO_MODE:
        return
    if Entity.query.filter_by(type="person", code="D001").first():
        return
    db.session.add_all([
        Entity(type="person", code="D001", name="فروشگاه نمونه", unit="شرکت", level=1),
        Entity(type="person", code="D002", name="مشتری نمونه", unit="فروشگاه", level=1),
        Entity(type="item", code="I001", name="لپ‌تاپ نمونه", unit="عدد", level=1, stock_qty=8),
        Entity(type="item", code="I002", name="ماوس بی‌سیم نمونه", unit="عدد", level=1, stock_qty=25),
        Entity(type="item", code="I003", name="کیبورد نمونه", unit="عدد", level=1, stock_qty=14),
        CashBox(name="صندوق اصلی", kind="cash", description="صندوق نمونه حساب پاک", is_active=True),
    ])
    Setting.set("ui_theme", "dark")
    Setting.set("dashboard_widgets", json.dumps([k for k, _ in DASHBOARD_WIDGET_CHOICES], ensure_ascii=False))
    Setting.set("price_display_mode", "last")
    Setting.set("search_sort", "recent")
    db.session.commit()


def _reset_demo_workspace():
    if not DEMO_MODE:
        return
    try:
        db.session.remove()
        db.drop_all()
        db.create_all()
        for path in (DB_DIR / "backups", DB_DIR / "fiscal_cases", DB_DIR / "uploads"):
            shutil.rmtree(path, ignore_errors=True)
        (DB_DIR / "backups").mkdir(parents=True, exist_ok=True)
        (DB_DIR / "fiscal_cases").mkdir(parents=True, exist_ok=True)
        ASSISTANT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        _reset_activity_log_file()
        save_users_catalog({
            DEMO_USERNAME: {
                "password": DEMO_PASSWORD,
                "role": "staff",
                "permissions": DEFAULT_PERMISSIONS,
                "is_active": True,
                "email": "",
            }
        })
        _seed_demo_data()
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("demo workspace reset failed")
        raise


@app.route(URL_PREFIX + "/demo/start", methods=["GET"])
def demo_start():
    if not DEMO_MODE:
        abort(404)
    if current_user.is_authenticated:
        logout_user()
        session.clear()
    _reset_demo_workspace()
    catalog = load_users_catalog()
    entry = catalog.get(DEMO_USERNAME) or {}
    login_user(User(DEMO_USERNAME, role=entry.get("role", "staff"), permissions=entry.get("permissions", DEFAULT_PERMISSIONS), is_active=True))
    session["login_at_utc"] = datetime.utcnow().isoformat()
    session["demo_session"] = True
    flash("وارد محیط آزمایشی شدید؛ این داده‌ها موقتی هستند و با خروج از دمو پاک می‌شوند.", "success")
    return redirect(URL_PREFIX + "/")


@app.route(URL_PREFIX + "/logout", methods=["POST"])
def logout():
    is_demo = bool(DEMO_MODE and session.get("demo_session"))
    if current_user.is_authenticated:
        app.logger.info(f"LOGOUT USER={current_user.username} IP={request.remote_addr}")
    logout_user()
    session.clear()
    if is_demo:
        _reset_demo_workspace()
    return redirect(URL_PREFIX + "/login")

# ----------------- Developer console -----------------
def _run_script_lines(lines:list[str]):
    msgs = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        cmd = parts[0].upper()
        if cmd not in ALLOWED_CMDS:
            msgs.append(f"رد شد: دستور نامجاز {cmd}")
            continue
        try:
            if cmd == "ADD_ITEM":
                code = parts[1]
                name = " ".join(parts[2:]).strip().strip('"').strip("'")
                form = {"type":"item","code":code,"name":name,"unit":"عدد","serial_no":code}
                errs, data = validate_entity_form(form)
                if errs:
                    msgs.append("خطا: " + " | ".join(errs))
                else:
                    ent = Entity(
                        type=data["e_type"], code=data["code"], name=data["name"],
                        unit=data["unit"], serial_no=data["serial"] or data["code"],
                        parent_id=(data["parent"].id if data["parent"] else None),
                        level=data["level"]
                    )
                    db.session.add(ent); db.session.commit()
                    msgs.append(f"ثبت شد: کالا {code} - {name}")

            elif cmd == "ADD_PERSON":
                code = parts[1]
                name = " ".join(parts[2:]).strip().strip('"').strip("'")
                form = {"type":"person","code":code,"name":name,"unit":"شرکت","serial_no":code}
                errs, data = validate_entity_form(form)
                if errs:
                    msgs.append("خطا: " + " | ".join(errs))
                else:
                    ent = Entity(
                        type=data["e_type"], code=data["code"], name=data["name"],
                        unit=data["unit"], serial_no=data["serial"] or data["code"],
                        parent_id=(data["parent"].id if data["parent"] else None),
                        level=data["level"]
                    )
                    db.session.add(ent); db.session.commit()
                    msgs.append(f"ثبت شد: شخص {code} - {name}")

            elif cmd == "RENAME":
                etype = parts[1].lower()
                code  = parts[2]
                new_name = " ".join(parts[3:]).strip().strip('"').strip("'")
                ent = Entity.query.filter_by(type=etype, code=code).first()
                if not ent:
                    msgs.append("یافت نشد.")
                else:
                    ent.name = new_name
                    db.session.commit()
                    msgs.append("نام تغییر کرد.")

            elif cmd == "DELETE":
                etype = parts[1].lower()
                code  = parts[2]
                ent = Entity.query.filter_by(type=etype, code=code).first()
                if not ent:
                    msgs.append("یافت نشد.")
                else:
                    references = _entity_delete_references(ent.id)
                    if references:
                        msgs.append(
                            "رد شد: حذف فیزیکی مجاز نیست؛ سابقه در "
                            + "، ".join(references)
                        )
                    else:
                        db.session.delete(ent)
                        db.session.commit()
                        try:
                            record_ledger(
                                "entity",
                                ent.id,
                                "delete",
                                {
                                    "type": ent.type,
                                    "code": ent.code,
                                    "name": ent.name,
                                },
                            )
                        except Exception:
                            app.logger.exception("failed to write developer entity delete ledger entry")
                        msgs.append("حذف شد.")

            elif cmd == "SEED_ITEMS":
                if not Entity.query.filter_by(type="item", code="101").first():
                    db.session.add(Entity(type="item", code="101", name="لپتاپ", level=1)); db.session.commit()
                p1 = Entity.query.filter_by(type="item", code="101").first()
                if not Entity.query.filter_by(type="item", code="101001").first():
                    db.session.add(Entity(type="item", code="101001", name="لپتاپ اچ‌پی", level=2, parent_id=p1.id)); db.session.commit()
                p2 = Entity.query.filter_by(type="item", code="101001").first()
                if not Entity.query.filter_by(type="item", code="101001001").first():
                    db.session.add(Entity(type="item", code="101001001", name="HP 650", level=3, parent_id=p2.id)); db.session.commit()
                msgs.append("نمونه کدینگ کالا ثبت شد.")

            elif cmd == "SEED_ACCOUNTS":
                root = parts[1]
                title = " ".join(parts[2:]).strip().strip('"').strip("'") or "حساب ریشه"
                if not root.isdigit() or len(root) not in (3,6,9):
                    msgs.append("کد ریشه نامعتبر است.")
                else:
                    if not Account.query.filter_by(code=root).first():
                        db.session.add(Account(code=root, name=title, level={3:1,6:2,9:3}[len(root)], locked=True))
                    if root == "990":
                        s1 = root + "001"
                        if not Account.query.filter_by(code=s1).first():
                            par = Account.query.filter_by(code=root).first()
                            db.session.add(Account(code=s1, name="حقوق", level=2, parent=par, locked=True))
                    db.session.commit()
                    msgs.append("کدینگ حسابداری ثبت/به‌روزرسانی شد.")
        except Exception as ex:
            msgs.append(f"خطا در اجرای دستور «{line}»: {ex}")
    return msgs

@app.route(URL_PREFIX + "/developer", methods=["GET","POST"])
@login_required
def developer_console():
    admin_required()
    if request.method == "POST":
        script = request.form.get("script","")
        msgs = _run_script_lines(script.splitlines())
        for m in msgs:
            flash(m, "danger" if m.startswith("خطا") or m.startswith("رد شد") else "success")
        return redirect(URL_PREFIX + "/developer")
    return render_template("developer.html", prefix=URL_PREFIX)

# ----------------- Unified Invoice (Sales & Purchase) -----------------
def _invoice_kind_from_request() -> str:
    """Resolve sales/purchase from an explicit kind or a legacy URL."""
    requested_kind = (request.args.get("kind") or "").strip().lower()
    if requested_kind in ("sales", "purchase"):
        return requested_kind
    if "/purchase" in request.path:
        return "purchase"
    if "/sales" in request.path:
        return "sales"
    return "sales"


@app.route(URL_PREFIX + "/invoice", methods=["GET", "POST"])
@app.route(URL_PREFIX + "/sales", methods=["GET", "POST"])  # backward compatibility
@app.route(URL_PREFIX + "/purchase", methods=["GET", "POST"])  # backward compatibility
@login_required
def unified_invoice():
    kind = _invoice_kind_from_request()
    
    # Check permission
    if kind == "sales":
        ensure_permission("sales")
    else:
        ensure_permission("purchase")
    
    now_info = _now_info()
    
    # Generate invoice number
    if kind == "sales":
        inv_number_generated = jalali_reference("INV", now_info["datetime"])
    else:
        # Purchase number logic
        nums = []
        for (num,) in db.session.query(Invoice.number).filter(Invoice.kind == "purchase").all():
            s = (num or "").strip()
            if s.isdigit():
                try:
                    nums.append(int(s))
                except:
                    pass
        inv_number_generated = str(max(nums) + 1) if nums else "1"
    
    allow_negative = _allow_negative_sales()

    if request.method == "POST":
        # Get kind from form
        form_kind = (request.form.get("invoice_kind") or kind).strip().lower()
        if form_kind not in ("sales", "purchase"):
            form_kind = kind
        # Re-authorize using the submitted document type; hidden fields are untrusted.
        ensure_permission(form_kind)
        
        number = (request.form.get("inv_number") or "").strip() or inv_number_generated
        inv_date = parse_gregorian_date(
            request.form.get("inv_date_greg"),
            allow_none=True,
        )
        if not inv_date:
            flash("تاریخ فاکتور معتبر نیست.", "danger")
            return redirect(URL_PREFIX + f"/invoice?kind={form_kind}")

        person = None
        pid = (request.form.get("person_token") or "").strip()
        if pid.isdigit():
            person = Entity.query.get(int(pid))
        if not person:
            pcode = (request.form.get("person_code") or "").strip()
            if pcode:
                person = Entity.query.filter_by(type="person", code=pcode).first()
        if not person or person.type != "person":
            person_label = "مشتری" if form_kind == "sales" else "تأمین‌کننده"
            flash(f"لطفاً {person_label} معتبر انتخاب کنید.", "danger")
            return redirect(URL_PREFIX + f"/invoice?kind={form_kind}")

        item_ids    = request.form.getlist("item_id[]")
        item_codes  = request.form.getlist("item_code[]")
        unit_prices = request.form.getlist("unit_price[]")
        qtys        = request.form.getlist("qty[]")

        rows = []
        MAX_ROWS = 15
        pending_stock = {}
        for i in range(min(len(item_ids), len(unit_prices), len(qtys))):
            iid = (item_ids[i] or "").strip()
            icode= (item_codes[i] or "").strip()
            up   = _to_float(unit_prices[i], 0.0)
            q    = _to_float(qtys[i], 0.0)

            item = None
            if iid.isdigit():
                item = Entity.query.get(int(iid))
            if (not item) and icode:
                item = Entity.query.filter_by(type="item", code=icode).first()

            if (item is not None) and item.type == "item" and q > 0 and up >= 0:
                # Stock check only for sales
                if form_kind == "sales" and not allow_negative:
                    base_stock = float(pending_stock.get(item.id, item.stock_qty or 0.0))
                    if base_stock - q < -1e-6:
                        flash(f"موجودی کالا «{item.name}» برای فروش کافی نیست.", "danger")
                        return redirect(URL_PREFIX + f"/invoice?kind={form_kind}")
                    pending_stock[item.id] = base_stock - q
                elif form_kind == "sales":
                    current = float(pending_stock.get(item.id, item.stock_qty or 0.0))
                    pending_stock[item.id] = current - q
                rows.append({"item": item, "unit_price": up, "qty": q})
            if len(rows) >= MAX_ROWS:
                break

        if not rows:
            flash("لطفاً حداقل یک ردیف کالای معتبر با تعداد وارد کنید.", "danger")
            return redirect(URL_PREFIX + f"/invoice?kind={form_kind}")

        subtotal = sum(r["unit_price"] * r["qty"] for r in rows)
        discount = 0.0
        tax      = 0.0
        total    = subtotal - discount + tax

        if float(total) <= 0:
            flash("جمع کل فاکتور باید بزرگ‌تر از صفر باشد.", "danger")
            return redirect(URL_PREFIX + f"/invoice?kind={form_kind}")

        if Invoice.query.filter_by(number=number).first():
            number = generate_invoice_number()

        inv = Invoice(
            number=number,
            date=inv_date,
            person_id=person.id,
            kind=form_kind,
            discount=discount,
            tax=tax,
            total=total,
        )
        db.session.add(inv)
        db.session.flush()

        for r in rows:
            item = r["item"]
            qty  = float(r["qty"])
            up   = float(r["unit_price"])
            line_total = qty * up

            db.session.add(InvoiceLine(
                invoice_id=inv.id,
                item_id=item.id,
                qty=qty,
                unit_price=up,
                line_total=line_total
            ))

            # Update stock atomically: sales decreases, purchase increases.
            _adjust_item_stock_delta(
                item,
                -qty if form_kind == "sales" else qty,
                allow_negative=(allow_negative or form_kind == "purchase"),
            )

            ph = PriceHistory.query.filter_by(person_id=person.id, item_id=item.id).first()
            if not ph:
                ph = PriceHistory(person_id=person.id, item_id=item.id, last_price=up)
                db.session.add(ph)
            else:
                ph.last_price = up

        # Update person balance atomically inside the same transaction.
        _adjust_person_balance_delta(
            person,
            float(total) if form_kind == "sales" else -float(total),
        )

        db.session.commit()

        action_label = "فروش" if form_kind == "sales" else "خرید"
        flash(
            f"✅ فاکتور {action_label} «{inv.number}» ثبت شد برای «{person.name}» — {amount_to_toman_words(total)}",
            "success",
        )
        
        # Redirect to appropriate cash doc
        if form_kind == "sales":
            return redirect(URL_PREFIX + f"/receive?invoice_id={inv.id}")
        else:
            return redirect(URL_PREFIX + f"/payment?invoice_id={inv.id}")

    return render_template(
        "invoice.html",
        prefix=URL_PREFIX,
        inv_number=inv_number_generated,
        current_jdate=now_info["jalali_date"],
        current_gdate=now_info["greg_date"],
        invoice_kind=kind,
    )

def _adjust_item_stock_delta(item, delta: float, allow_negative: bool = False) -> None:
    """Atomically change item stock inside the current transaction."""
    delta = float(delta or 0.0)
    if abs(delta) < 1e-12:
        return
    item_id = getattr(item, "id", None)
    if item_id is None:
        item.stock_qty = float(item.stock_qty or 0.0) + delta
        return

    if delta < 0 and not allow_negative:
        result = db.session.execute(
            text(
                "UPDATE entities "
                "SET stock_qty = COALESCE(stock_qty, 0) + :delta "
                "WHERE id = :id AND type = 'item' "
                "AND COALESCE(stock_qty, 0) + :delta >= 0"
            ),
            {"delta": delta, "id": int(item_id)},
        )
    else:
        result = db.session.execute(
            text(
                "UPDATE entities "
                "SET stock_qty = COALESCE(stock_qty, 0) + :delta "
                "WHERE id = :id AND type = 'item'"
            ),
            {"delta": delta, "id": int(item_id)},
        )

    if result.rowcount != 1:
        raise ValueError("موجودی کالای «{}» کافی نیست.".format(getattr(item, "name", "نامشخص")))
    db.session.refresh(item, attribute_names=["stock_qty"])


def _adjust_person_balance_delta(person, delta: float) -> None:
    """Atomically update a person's denormalized balance inside the current transaction."""
    delta = float(delta or 0.0)
    if abs(delta) < 1e-12:
        return
    person_id = getattr(person, "id", None)
    if person_id is None:
        person.balance = float(person.balance or 0.0) + delta
        return

    result = db.session.execute(
        text(
            "UPDATE entities "
            "SET balance = COALESCE(balance, 0) + :delta "
            "WHERE id = :id AND type = 'person'"
        ),
        {"delta": delta, "id": int(person_id)},
    )
    if result.rowcount != 1:
        raise ValueError("طرف حساب برای به‌روزرسانی مانده پیدا نشد.")
    db.session.refresh(person, attribute_names=["balance"])


def _adjust_cash_person_balance(doc_type: str, person, old_amount: float, new_amount: float) -> None:
    """Adjust a denormalized person balance by the change in a cash document."""
    delta = float(new_amount or 0.0) - float(old_amount or 0.0)
    if (doc_type or "").strip().lower() == "receive":
        _adjust_person_balance_delta(person, -delta)
    else:
        _adjust_person_balance_delta(person, delta)


# ----------------- Entities CRUD -----------------
@app.route(URL_PREFIX + "/entities")
@login_required
def entities_list():
    ensure_permission("entities")
    kind = (request.args.get("kind") or "item").strip()
    if kind not in ("person","item"):
        kind = "item"
    q = (request.args.get("q") or "").strip()
    try:
        page = int(request.args.get("page") or 1)
    except Exception:
        page = 1
    page = max(1, page)
    try:
        per_page = int(request.args.get("per_page") or 50)
    except Exception:
        per_page = 50
    per_page = max(10, min(per_page, 500))

    base = Entity.query.filter_by(type=kind)

    if q:
        starts = base.filter(or_(Entity.code.ilike(f"{q}%"), Entity.name.ilike(f"{q}%")))
        contains = base.filter(or_(Entity.code.ilike(f"%{q}%"), Entity.name.ilike(f"%{q}%")))
        rows = list(starts.order_by(Entity.level.asc(), Entity.code.asc()).all())
        seen = {r.id for r in rows}
        for e in contains.order_by(Entity.level.asc(), Entity.code.asc()).all():
            if e.id not in seen:
                rows.append(e); seen.add(e.id)
    else:
        rows = base.order_by(Entity.level.asc(), Entity.code.asc()).all()

    # Enrich with last prices and stock/balance
    enriched = []
    for e in rows:
        item = {"entity": e}
        if kind == "item":
            item["stock"] = float(e.stock_qty or 0.0)
            # Last purchase price
            last_purchase = (
                db.session.query(InvoiceLine.unit_price)
                .join(Invoice, InvoiceLine.invoice_id == Invoice.id)
                .filter(InvoiceLine.item_id == e.id, Invoice.kind == "purchase")
                .order_by(Invoice.date.desc(), Invoice.id.desc())
                .first()
            )
            item["last_purchase_price"] = float(last_purchase[0]) if last_purchase else None
            # Last sale price
            last_sale = (
                db.session.query(InvoiceLine.unit_price)
                .join(Invoice, InvoiceLine.invoice_id == Invoice.id)
                .filter(InvoiceLine.item_id == e.id, Invoice.kind == "sales")
                .order_by(Invoice.date.desc(), Invoice.id.desc())
                .first()
            )
            item["last_sale_price"] = float(last_sale[0]) if last_sale else None
        elif kind == "person":
            item["balance"] = float(e.balance or 0.0)
            item["status"] = "بستانکار" if item["balance"] >= 0 else "بدهکار"
        enriched.append(item)

    # Pagination
    total_count = len(enriched)
    start = (page - 1) * per_page
    end = start + per_page
    page_rows = enriched[start:end]
    has_prev = page > 1
    has_next = end < total_count

    return render_template(
        "entities/list.html",
        rows=page_rows,
        q=q,
        kind=kind,
        prefix=URL_PREFIX,
        page=page,
        per_page=per_page,
        has_prev=has_prev,
        has_next=has_next,
        total=total_count,
    )

@app.route(URL_PREFIX + "/entities/new", methods=["GET", "POST"])
@login_required
def entities_new():
    ensure_permission("entities")
    if request.method == "POST":
        errors, data = validate_entity_form(request.form)
        if errors:
            for e in errors: flash(e, "danger")
            return redirect(URL_PREFIX + "/entities/new")
        ent = Entity(
            type=data["e_type"], code=data["code"], name=data["name"],
            unit=data["unit"], serial_no=data["serial"] or data["code"],
            parent_id=(data["parent"].id if data["parent"] else None),
            level=data["level"]
        )
        db.session.add(ent); db.session.commit()
        try:
            record_ledger("entity", ent.id, "create", {"type": ent.type, "code": ent.code, "name": ent.name, "unit": ent.unit, "level": ent.level})
        except Exception:
            pass
        flash("ثبت شد.", "success")
        return redirect(URL_PREFIX + f"/entities?kind={ent.type}")

    parents_lvl1 = Entity.query.filter_by(level=1).order_by(Entity.code.asc()).all()
    parents_lvl2 = Entity.query.filter_by(level=2).order_by(Entity.code.asc()).all()
    # suggest next codes for both types so the template can prefill accordingly
    suggested_person_code = _suggest_next_entity_code("person")
    suggested_item_code = _suggest_next_entity_code("item")
    return render_template(
        "entities/new.html",
        parents_lvl1=parents_lvl1,
        parents_lvl2=parents_lvl2,
        prefix=URL_PREFIX,
        suggested_person_code=suggested_person_code,
        suggested_item_code=suggested_item_code,
    )

@app.route(URL_PREFIX + "/entities/<int:eid>/edit", methods=["GET","POST"])
@login_required
def entities_edit(eid):
    admin_required()
    ent = Entity.query.get_or_404(eid)
    if request.method == "POST":
        errors, data = validate_entity_form(request.form, for_update_id=ent.id)
        if errors:
            for e in errors: flash(e, "danger")
            return redirect(URL_PREFIX + f"/entities/{eid}/edit")
        ent.type = data["e_type"]; ent.code = data["code"]; ent.name = data["name"]
        ent.unit = data["unit"];   ent.serial_no = data["serial"] or data["code"]
        ent.parent_id = data["parent"].id if data["parent"] else None
        ent.level = data["level"]
        db.session.commit()
        try:
            record_ledger("entity", ent.id, "update", {"type": ent.type, "code": ent.code, "name": ent.name, "unit": ent.unit, "level": ent.level})
        except Exception:
            pass
        flash("ویرایش شد.", "success")
        return redirect(URL_PREFIX + f"/entities?kind={ent.type}")

    parents_lvl1 = Entity.query.filter_by(level=1).order_by(Entity.code.asc()).all()
    parents_lvl2 = Entity.query.filter_by(level=2).order_by(Entity.code.asc()).all()
    return render_template("entities/edit.html", ent=ent, parents_lvl1=parents_lvl1, parents_lvl2=parents_lvl2, prefix=URL_PREFIX)

def _entity_delete_references(eid: int) -> list[str]:
    """Return accounting relations that prevent physical entity deletion."""
    references = []
    if Entity.query.filter_by(parent_id=eid).first():
        references.append("زیرمجموعه‌ها")
    if Invoice.query.filter_by(person_id=eid).first():
        references.append("فاکتورها")
    if InvoiceLine.query.filter_by(item_id=eid).first():
        references.append("ردیف فاکتورها")
    if CashDoc.query.filter_by(person_id=eid).first():
        references.append("اسناد دریافت/پرداخت")
    if PriceHistory.query.filter(
        (PriceHistory.person_id == eid) | (PriceHistory.item_id == eid)
    ).first():
        references.append("سوابق قیمت")
    return references


@app.route(URL_PREFIX + "/entities/<int:eid>/delete", methods=["POST"])
@login_required
def entities_delete(eid):
    admin_required()
    ent = Entity.query.get_or_404(eid)
    t = ent.type

    # Preserve accounting history in the primary UI and every admin path.
    references = _entity_delete_references(eid)

    if references:
        flash(
            "حذف فیزیکی این مورد مجاز نیست چون در " + "، ".join(references) + " سابقه دارد.",
            "warning",
        )
        return redirect(URL_PREFIX + f"/entities?kind={t}")

    payload = {"id": ent.id, "type": ent.type, "code": ent.code, "name": ent.name}
    db.session.delete(ent)
    db.session.commit()
    try:
        record_ledger("entity", eid, "delete", payload)
    except Exception:
        pass
    flash("حذف شد.", "success")
    return redirect(URL_PREFIX + f"/entities?kind={t}")

# ----------------- Reports -----------------
@app.route(URL_PREFIX + "/reports")
@login_required
def reports():
    ensure_permission("reports")
    q     = (request.args.get("q") or "").strip()
    typ   = (request.args.get("type") or "all").strip().lower()
    dfrom = request.args.get("from")
    dto   = request.args.get("to")
    method = (request.args.get("method") or "").strip().lower()  # cash methods
    cashbox_id = (request.args.get("cashbox_id") or "").strip()
    amount_min_raw = (request.args.get("amount_min") or "").strip()
    amount_max_raw = (request.args.get("amount_max") or "").strip()
    export = (request.args.get("export") or "").strip().lower()
    try:
        page = int(request.args.get("page") or 1)
    except Exception:
        page = 1
    page = max(1, page)

    def parse_d(s):
        return parse_gregorian_date(s, allow_none=True)
    df = parse_d(dfrom); dt = parse_d(dto)

    # optional filters from links/search
    person_filter = (request.args.get("person_id") or "").strip()
    item_filter = (request.args.get("item_id") or "").strip()
    try:
        per_page = int(request.args.get("per_page") or 100)
    except Exception:
        per_page = 100
    per_page = max(10, min(per_page, 500))
    try:
        amount_min = _to_float(amount_min_raw, None) if amount_min_raw else None
    except Exception:
        amount_min = None
    try:
        amount_max = _to_float(amount_max_raw, None) if amount_max_raw else None
    except Exception:
        amount_max = None

    rows = []
    totals = {"sales": 0.0, "purchase": 0.0, "receive": 0.0, "payment": 0.0}

    # Invoices (sales/purchase)
    if typ in ("all", "invoice", "sales", "purchase"):
        inv_q = db.session.query(Invoice).join(Entity, Invoice.person_id == Entity.id)
        if q:
            inv_q = inv_q.filter(or_(
                Invoice.number.ilike(f"%{q}%"),
                Entity.name.ilike(f"%{q}%"),
                Entity.code.ilike(f"%{q}%"),
            ))
        if person_filter and person_filter.isdigit():
            inv_q = inv_q.filter(Invoice.person_id == int(person_filter))
        if item_filter and item_filter.isdigit():
            inv_q = inv_q.join(InvoiceLine, Invoice.id == InvoiceLine.invoice_id).filter(InvoiceLine.item_id == int(item_filter))
        if df: inv_q = inv_q.filter(Invoice.date >= df)
        if dt: inv_q = inv_q.filter(Invoice.date <= dt)
        # Use explicit kind when available
        if typ == "sales":
            inv_q = inv_q.filter(Invoice.kind == 'sales')
        elif typ == "purchase":
            inv_q = inv_q.filter(Invoice.kind == 'purchase')

        for inv in inv_q.order_by(Invoice.id.desc()).all():
            kind = inv.kind or ("sales" if (inv.number or "").upper().startswith("INV-") else "purchase")
            amt = float(inv.total or 0.0)
            if amount_min is not None and amt < amount_min: 
                continue
            if amount_max is not None and amt > amount_max:
                continue
            totals[kind] += amt
            rows.append({
                "kind": "invoice",
                "id": inv.id,
                "number": inv.number,
                "date": to_jdate_str(inv.date),
                "date_key": inv.date,
                "person": inv.person.name,
                "amount": amt,
                "invoice_kind": kind,
                "person_balance": float(inv.person.balance or 0.0) if inv.person else None,
            })

    # Cash documents
    if typ in ("all", "receive", "payment", "cheque"):
        cd_q = db.session.query(CashDoc).join(Entity, CashDoc.person_id == Entity.id)
        if typ in ("receive", "payment"):
            cd_q = cd_q.filter(CashDoc.doc_type == typ)
        if typ == "cheque":
            cd_q = cd_q.filter(func.lower(func.coalesce(CashDoc.method, "")) == "cheque")
        if q:
            cd_q = cd_q.filter(or_(
                CashDoc.number.ilike(f"%{q}%"),
                Entity.name.ilike(f"%{q}%"),
                Entity.code.ilike(f"%{q}%"),
                CashDoc.cheque_number.ilike(f"%{q}%"),
            ))
        if df: cd_q = cd_q.filter(CashDoc.date >= df)
        if dt: cd_q = cd_q.filter(CashDoc.date <= dt)
        if method:
            cd_q = cd_q.filter(func.lower(func.coalesce(CashDoc.method, "")) == method)
        if cashbox_id and cashbox_id.isdigit():
            cd_q = cd_q.filter(CashDoc.cashbox_id == int(cashbox_id))

        for d in cd_q.order_by(CashDoc.id.desc()).all():
            amt = float(d.amount or 0.0)
            if amount_min is not None and amt < amount_min: 
                continue
            if amount_max is not None and amt > amount_max:
                continue
            totals[d.doc_type] += amt
            rows.append({
                "kind": d.doc_type,
                "id": d.id,
                "number": d.number,
                "date": to_jdate_str(d.date),
                "date_key": d.date,
                "person": d.person.name,
                "amount": amt,
                "cheque_number": d.cheque_number,
                "method": d.method,
                "cashbox": d.cashbox.name if d.cashbox else None,
                "cheque_due": to_jdate_str(d.cheque_due_date) if d.cheque_due_date else None,
            })

    # Sort rows by date (gregorian) then id/number desc
    rows.sort(key=lambda r: (r.get("date_key"), r.get("id", 0)), reverse=True)

    # Pagination
    total_count = len(rows)
    start = (page - 1) * per_page
    end = start + per_page
    page_rows = rows[start:end]
    has_prev = page > 1
    has_next = end < total_count

    # Cashboxes for filter select
    cashboxes = (
        CashBox.query.filter_by(is_active=True)
        .order_by(CashBox.kind.desc(), CashBox.name.asc())
        .all()
    )

    try:
        return render_template(
            "reports.html",
            rows=page_rows, q=q, typ=typ, dfrom=dfrom, dto=dto, prefix=URL_PREFIX,
            per_page=per_page, page=page, has_prev=has_prev, has_next=has_next, total=total_count,
            person_filter=person_filter,
            item_filter=item_filter,
            method=method,
            cashbox_id=cashbox_id,
            amount_min=amount_min_raw,
            amount_max=amount_max_raw,
            totals=totals,
            cashboxes=cashboxes,
        )
    except Exception:
        html = [
            "<h2>گزارشات</h2>",
            "<form method='get' action='' style='display:flex;gap:8px;flex-wrap:wrap;margin:10px 0'>",
            f"<input class='inp' name='q' value='{escape(q)}' placeholder='شماره/نام/کد' style='max-width:240px'>",
            "<select class='inp' name='type' style='max-width:160px'>",
            f"<option value='all' {'selected' if typ=='all' else ''}>همه</option>",
            f"<option value='invoice' {'selected' if typ=='invoice' else ''}>فاکتور فروش</option>",
            f"<option value='receive' {'selected' if typ=='receive' else ''}>دریافت</option>",
            f"<option value='payment' {'selected' if typ=='payment' else ''}>پرداخت</option>",
            "</select>",
            f"<input class='inp' type='date' name='from' value='{escape(dfrom or '')}' style='max-width:160px'>",
            f"<input class='inp' type='date' name='to'   value='{escape(dto or '')}'   style='max-width:160px'>",
            "<button class='btn-primary' style='width:auto;padding:0 16px'>جستجو</button>",
            "</form>",
            "<div class='links'><table style='width:100%;background:#fff;border:1px solid #eee;border-radius:10px;border-collapse:collapse'>",
            "<thead><tr style='background:#f7faf9'>"
            "<th style='text-align:right;padding:10px;border-bottom:1px solid #eee'>نوع</th>"
            "<th style='text-align:right;padding:10px;border-bottom:1px solid #eee'>شماره</th>"
            "<th style='text-align:right;padding:10px;border-bottom:1px solid #eee'>تاریخ (شمسی)</th>"
            "<th style='text-align:right;padding:10px;border-bottom:1px solid #eee'>طرف حساب</th>"
            "<th style='text-align:right;padding:10px;border-bottom:1px solid #eee'>مبلغ/جمع</th>"
            "<th style='text-align:center;padding:10px;border-bottom:1px solid #eee'>عملیات</th>"
            "</tr></thead><tbody>"
        ]

        for r in rows:
            label = {"invoice": "فاکتور فروش","receive": "دریافت","payment": "پرداخت"}.get(r["kind"], str(r["kind"]))
            view = f"{URL_PREFIX}/invoice/{int(r['id'])}" if r["kind"] == "invoice" else f"{URL_PREFIX}/cash/{int(r['id'])}"
            ops_str = f'<a href="{escape(view)}">مشاهده</a>'
            jdate = to_jdate_str(r["date"])
            row_html = (
                "<tr>"
                f"<td style='padding:8px;border-bottom:1px solid #f3f3f3'>{escape(label)}</td>"
                f"<td style='padding:8px;border-bottom:1px solid #f3f3f3'><code>{escape(r['number'])}</code></td>"
                f"<td style='padding:8px;border-bottom:1px solid #f3f3f3'>{escape(jdate)}</td>"
                f"<td style='padding:8px;border-bottom:1px solid #f3f3f3'>{escape(r['person'])}</td>"
                f"<td style='padding:8px;border-bottom:1px solid #f3f3f3'>{int(r['amount']):,}</td>"
                f"<td style='padding:8px;border-bottom:1px solid #f3f3f3;text-align:center'>{ops_str}</td>"
                "</tr>"
            )
            html.append(row_html)

        html.append("</tbody></table></div>")
        return render_template("page.html", title="گزارشات", content=Markup("".join(html)), prefix=URL_PREFIX)

# ====== Minimal viewers ======
@app.route(URL_PREFIX + "/invoice/<int:inv_id>")
@login_required
def invoice_view(inv_id):
    ensure_permission("reports", "sales", "purchase")
    inv = Invoice.query.get_or_404(inv_id)
    lines = InvoiceLine.query.filter_by(invoice_id=inv.id).all()
    title = "فاکتور فروش" if inv.kind == "sales" else "فاکتور خرید"
    html = [
        f"<b>شماره:</b> {escape(inv.number)}",
        f"<br><b>تاریخ (شمسی):</b> {escape(to_jdate_str(inv.date))}",
        f"<br><b>مشتری/تأمین‌کننده:</b> {escape(inv.person.name)}",
        f"<br><b>جمع:</b> {int(inv.total):,}",
        "<hr><b>آیتم‌ها:</b>",
        "<ul>" + "".join(
            f"<li>{escape(ln.item.name)} | {ln.qty} × {ln.unit_price} = {ln.line_total}</li>"
            for ln in lines
        ) + "</ul>",
    ]
    return render_template("page.html", title=title, content=Markup("".join(html)), prefix=URL_PREFIX)

@app.route(URL_PREFIX + "/cash/<int:doc_id>")
@login_required
def cash_view(doc_id):
    ensure_permission("reports", "receive", "payment")
    doc = CashDoc.query.get_or_404(doc_id)
    kind = "دریافت" if doc.doc_type == "receive" else "پرداخت"
    cheque_meta = ""
    if (doc.method or "").lower() == "cheque":
        cheque_parts = []
        if doc.cheque_number:
            cheque_parts.append(f"شماره صیادی: <code>{escape(doc.cheque_number)}</code>")
        if doc.cheque_bank:
            cheque_parts.append(f"بانک: {escape(doc.cheque_bank)}")
        if doc.cheque_branch:
            cheque_parts.append(f"شعبه: {escape(doc.cheque_branch)}")
        if doc.cheque_due_date:
            cheque_parts.append(f"سررسید: {escape(to_jdate_str(doc.cheque_due_date))}")
        if doc.cheque_account:
            cheque_parts.append(f"شماره حساب: {escape(doc.cheque_account)}")
        if doc.cheque_owner:
            cheque_parts.append(f"صاحب حساب: {escape(doc.cheque_owner)}")
        if cheque_parts:
            cheque_meta = "<br><b>جزئیات چک:</b> " + "<br>".join(cheque_parts)
    cashbox_line = ""
    if doc.cashbox:
        box_label = escape(doc.cashbox.name)
        if doc.cashbox.kind == "bank" and doc.cashbox.bank_name:
            box_label = f"{box_label} ({escape(doc.cashbox.bank_name)})"
        cashbox_line = f"<br><b>صندوق/حساب:</b> {box_label}"
    method_label = CASH_METHOD_LABELS.get(doc.method or "", doc.method or "—")
    html = (
        f"<b>نوع:</b> {escape(kind)}<br><b>شماره:</b> {escape(doc.number)}"
        f"<br><b>تاریخ (شمسی):</b> {escape(to_jdate_str(doc.date))}"
        f"<br><b>طرف حساب:</b> {escape(doc.person.name)}"
        f"<br><b>مبلغ:</b> {int(doc.amount):,}"
        f"<br><b>روش:</b> {escape(method_label)}"
        + cashbox_line
        + cheque_meta
    )
    return render_template("page.html", title="سند نقدی", content=Markup(html), prefix=URL_PREFIX)

# ===================== دریافت وجه =====================
# ----------------- Unified Cash Doc (Receive & Payment) -----------------
@app.route(URL_PREFIX + "/cash_doc", methods=["GET", "POST"])
@app.route(URL_PREFIX + "/receive", methods=["GET", "POST"])
@app.route(URL_PREFIX + "/payment", methods=["GET", "POST"])
@login_required
def unified_cash():
    # تشخیص نوع سند از مسیر یا پارامتر
    kind = request.args.get("kind", "").strip().lower()
    if not kind:
        if "/receive" in request.path:
            kind = "receive"
        elif "/payment" in request.path:
            kind = "payment"
        else:
            kind = "receive"  # پیش‌فرض
    
    # بررسی دسترسی
    ensure_permission(kind)
    
    now_info = _now_info()
    if kind == "receive":
        doc_number = jalali_reference("RCV", now_info["datetime"])
    else:
        doc_number = jalali_reference("PAY", now_info["datetime"])
    
    pos_device_key, pos_device_label = _pos_device_config()
    prefill_amount = None
    prefill_note = None
    prefill_person = None
    
    # فقط صندوق‌های فعال را بیاور
    cashboxes = (
        CashBox.query.filter_by(is_active=True)
        .order_by(CashBox.kind.desc(), CashBox.name.asc())
        .all()
    )
    
    if request.method == "GET":
        invoice_id = (request.args.get("invoice_id") or "").strip()
        if invoice_id.isdigit():
            inv = Invoice.query.get(int(invoice_id))
            if inv:
                prefill_amount = float(inv.total or 0.0)
                if inv.person:
                    prefill_person = inv.person
                if kind == "receive":
                    prefill_note = f"دریافت بابت فاکتور فروش {inv.number}"
                else:
                    prefill_note = f"پرداخت بابت فاکتور خرید {inv.number}"
        if prefill_amount is None:
            try:
                prefill_amount = _to_float(request.args.get("amount"), None)
            except Exception:
                prefill_amount = None
        pid = (request.args.get("person_id") or "").strip()
        if not prefill_person and pid.isdigit():
            prefill_person = Entity.query.get(int(pid))

    if request.method == "POST":
        # kind از form data
        form_kind = request.form.get("cash_kind", kind).strip().lower()
        if form_kind not in ("receive", "payment"):
            form_kind = kind
        # Re-authorize using the submitted document type; hidden fields are untrusted.
        ensure_permission(form_kind)

        number = (request.form.get("doc_number") or "").strip() or doc_number
        doc_date = parse_gregorian_date(request.form.get("doc_date_greg"))
        if not doc_date:
            flash("تاریخ سند معتبر نیست.", "danger")
            return redirect(URL_PREFIX + f"/cash_doc?kind={form_kind}")

        person = None
        pid = (request.form.get("person_token") or "").strip()
        if pid.isdigit():
            person = Entity.query.get(int(pid))
        if not person:
            pcode = (request.form.get("person_code") or "").strip()
            if pcode:
                person = Entity.query.filter_by(type="person", code=pcode).first()
        if not person or person.type != "person":
            flash("لطفاً طرف حساب معتبر انتخاب کنید.", "danger")
            return redirect(URL_PREFIX + f"/cash_doc?kind={form_kind}")

        amount = _to_float(request.form.get("amount"), 0.0)
        if amount <= 0:
            flash("مبلغ باید بزرگ‌تر از صفر باشد.", "danger")
            return redirect(URL_PREFIX + f"/cash_doc?kind={form_kind}")
        
        method = (request.form.get("method") or "").strip().lower()
        if method not in ("pos", "cash", "bank", "cheque"):
            method = "cash"
        note = (request.form.get("note") or "").strip() or None

        cashbox = None
        cashbox_raw = (request.form.get("cashbox_id") or "").strip()
        if cashbox_raw.isdigit():
            cashbox = CashBox.query.get(int(cashbox_raw))
            if cashbox and not cashbox.is_active:
                cashbox = None

        cheque_number = "".join(ch for ch in (request.form.get("cheque_number") or "") if ch.isdigit())
        cheque_bank = (request.form.get("cheque_bank") or "").strip() or None
        cheque_branch = (request.form.get("cheque_branch") or "").strip() or None
        cheque_account = (request.form.get("cheque_account") or "").strip() or None
        cheque_owner = (request.form.get("cheque_owner") or "").strip() or None
        cheque_due_date = parse_gregorian_date(
            request.form.get("cheque_due_date"), allow_none=True
        )
        if cheque_due_date is None:
            cheque_due_date = parse_jalali_date(
                request.form.get("cheque_due_date_fa"), allow_none=True
            )

        # فقط برای چک نیاز به صندوق داریم
        if method == "cheque":
            if not cashbox or cashbox.kind != "bank":
                flash("برای ثبت چک، یک حساب بانکی فعال انتخاب کنید.", "danger")
                return redirect(URL_PREFIX + f"/cash_doc?kind={form_kind}")
            if len(cheque_number) != 16:
                flash("شماره صیادی چک باید ۱۶ رقم باشد.", "danger")
                return redirect(URL_PREFIX + f"/cash_doc?kind={form_kind}")
        elif method in ("cash", "bank"):
            # اگر صندوق انتخاب شده، نوعش را بررسی کن
            if cashbox:
                required_kind = "cash" if method == "cash" else "bank"
                if cashbox.kind != required_kind:
                    flash("نوع صندوق با روش پرداخت مطابقت ندارد.", "warning")
        else:
            # برای POS و سایر روش‌ها چک را پاک کن
            cheque_number = None
            cheque_bank = None
            cheque_branch = None
            cheque_account = None
            cheque_owner = None
            cheque_due_date = None

        # اگر چک نیست، مقادیر چک را null کن
        if method != "cheque":
            cheque_number = None
            cheque_bank = None
            cheque_branch = None
            cheque_account = None
            cheque_owner = None
            cheque_due_date = None

        doc = CashDoc(
            doc_type=form_kind,
            number=number,
            date=doc_date,
            person_id=person.id,
            amount=amount,
            method=method,
            note=note,
            cashbox_id=cashbox.id if cashbox else None,
            cheque_number=cheque_number or None,
            cheque_bank=cheque_bank,
            cheque_branch=cheque_branch,
            cheque_account=cheque_account,
            cheque_owner=cheque_owner,
            cheque_due_date=cheque_due_date,
        )
        db.session.add(doc)

        # ثبت سند و تغییر مانده باید اتمیک باشند.
        try:
            _adjust_cash_person_balance(form_kind, person, 0.0, amount)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            current_app.logger.exception("cash document posting failed")
            flash(f"خطا در ثبت سند: {exc}", "danger")
            return redirect(URL_PREFIX + f"/cash_doc?kind={form_kind}")

        try:
            record_ledger(
                "cashdoc",
                doc.id,
                "create",
                {
                    "doc_type": form_kind,
                    "number": number,
                    "amount": float(amount),
                    "person_id": person.id,
                    "cashbox_id": cashbox.id if cashbox else None,
                    "method": method,
                },
            )
        except Exception:
            current_app.logger.exception("failed to write unified cashdoc ledger entry")

        action_label = "دریافت" if form_kind == "receive" else "پرداخت"
        flash(
            f"✅ {action_label} «{number}» برای «{person.name}» — {amount_to_toman_words(amount)}",
            "success",
        )
        return redirect(URL_PREFIX + f"/cash/{doc.id}")

    return render_template(
        "cash_doc.html",
        doc_number=doc_number,
        prefix=URL_PREFIX,
        current_jdate=now_info["jalali_date"],
        current_gdate=now_info["greg_date"],
        pos_device_key=pos_device_key,
        pos_device_label=pos_device_label,
        prefill_amount=prefill_amount,
        prefill_person=prefill_person,
        prefill_note=prefill_note,
        cashboxes=cashboxes,
        cash_kind=kind,
    )

# ----------------- Legacy Receive compatibility -----------------
@app.route(URL_PREFIX + "/receive_old", methods=["GET"])
@login_required
def receive_old():
    ensure_permission("receive")
    return redirect(URL_PREFIX + "/receive")
# ===================== ویرایش سند نقدی =====================
@app.route(URL_PREFIX + "/cash/<int:doc_id>/edit", methods=["GET","POST"])
@login_required
def cash_edit(doc_id):
    admin_required()
    doc = CashDoc.query.get_or_404(doc_id)
    if request.method == "POST":
        try:
            old_amount = float(doc.amount or 0.0)
            new_amount = _to_float(request.form.get("amount"), old_amount)
            if new_amount <= 0:
                flash("مبلغ سند باید بزرگ‌تر از صفر باشد.", "danger")
                return redirect(URL_PREFIX + f"/cash/{doc.id}/edit")
            _adjust_cash_person_balance(doc.doc_type, doc.person, old_amount, new_amount)
            doc.amount = new_amount
            doc.note = (request.form.get("note") or "").strip() or None
            m = (request.form.get("method") or "").strip().lower()
            current_method = (doc.method or "").strip().lower()
            if m and m != current_method:
                db.session.rollback()
                flash("روش سند هنگام ویرایش قابل تغییر نیست؛ برای روش دیگر سند جدید ثبت کنید.", "warning")
                return redirect(URL_PREFIX + f"/cash/{doc.id}/edit")
            db.session.commit()
            try:
                record_ledger(
                    "cashdoc",
                    doc.id,
                    "update",
                    {
                        "before_amount": old_amount,
                        "after_amount": new_amount,
                        "person_id": doc.person_id,
                        "doc_type": doc.doc_type,
                        "method": doc.method,
                    },
                )
            except Exception:
                app.logger.exception("failed to write cashdoc update ledger entry")
            flash("ویرایش شد.", "success")
        except Exception as ex:
            db.session.rollback()
            flash(f"خطا: {ex}", "danger")
        return redirect(URL_PREFIX + f"/cash/{doc.id}")
    current_method = (doc.method or "").lower()
    edit_html = f"""
    <form method="post">
      <div class="card" style="padding:10px">
        <label class="lbl">مبلغ</label>
        <input class="inp" name="amount" value="{int(doc.amount)}">
        <label class="lbl" style="margin-top:8px">روش</label>
        <input class="inp" value="{escape(CASH_METHOD_LABELS.get(current_method, 'نامشخص'))}" disabled>
        <input type="hidden" name="method" value="{escape(current_method)}">
        <label class="lbl" style="margin-top:8px">یادداشت</label>
        <textarea class="inp" name="note">{escape(doc.note or "")}</textarea>
        <div style="margin-top:10px"><button class="btn">ذخیره</button></div>
      </div>
    </form>
    """
    return render_template("page.html", title="ویرایش سند دریافت/پرداخت", content=Markup(edit_html), prefix=URL_PREFIX)

# ----------------- Settings/Admin stubs -----------------
@app.route(URL_PREFIX + "/settings", methods=["GET", "POST"])
@login_required
def settings_stub():
    current_key, current_label = _pos_device_config()
    
    # دریافت تنظیمات شخصی کاربر
    username = getattr(current_user, "username", "admin")
    user_settings = UserSettings.get_for_user(username)
    
    if request.method == "POST":
        form_id = (request.form.get("form_id") or "pos").strip().lower()
        if form_id == "pos":
            admin_required()
            key = (request.form.get("pos_device") or "none").strip()
            if key not in dict(POS_DEVICE_CHOICES):
                flash("دستگاه انتخاب‌شده نامعتبر است.", "danger")
                return redirect(URL_PREFIX + "/settings")
            Setting.set("pos_device", key)
            db.session.commit()
            flash("تنظیمات ذخیره شد.", "success")
        elif form_id == "ui":
            admin_required()
            theme = (request.form.get("ui_theme") or "light").strip().lower()
            sort_key = (request.form.get("search_sort") or _search_sort_key()).strip().lower()
            price_mode = (request.form.get("price_display_mode") or _price_display_mode()).strip().lower()
            allow_negative = request.form.get("allow_negative_sales") == "on"
            widget_keys = request.form.getlist("dashboard_widgets")

            valid_themes = {k for k, _ in THEME_CHOICES}
            if theme not in valid_themes:
                theme = _ui_theme_key()

            valid_sorts = {k for k, _ in SEARCH_SORT_CHOICES}
            if sort_key not in valid_sorts:
                sort_key = _search_sort_key()

            valid_price = {k for k, _ in PRICE_DISPLAY_MODES}
            if price_mode not in valid_price:
                price_mode = _price_display_mode()

            valid_widgets = {k for k, _ in DASHBOARD_WIDGET_CHOICES}
            widgets_payload = [w for w in widget_keys if w in valid_widgets]
            if not widgets_payload:
                widgets_payload = list(valid_widgets)

            Setting.set("ui_theme", theme)
            Setting.set("search_sort", sort_key)
            Setting.set("price_display_mode", price_mode)
            Setting.set("allow_negative_sales", "on" if allow_negative else "off")
            Setting.set("dashboard_widgets", json.dumps(widgets_payload, ensure_ascii=False))
            db.session.commit()
            flash("تنظیمات ظاهری و جستجو ذخیره شد.", "success")
        elif form_id == "ai":
            admin_required()
            api_key = (request.form.get("openai_api_key") or "").strip()
            clear_api_key = request.form.get("clear_openai_api_key") == "1"
            current_api_key = _openai_api_key()
            model = (request.form.get("openai_model") or _assistant_model()).strip()
            valid_models = {k for k, _ in ASSISTANT_MODEL_CHOICES}
            if model not in valid_models:
                model = _assistant_model()
            if api_key:
                Setting.set("openai_api_key", encrypt_secret(current_app, api_key))
            elif clear_api_key:
                Setting.set("openai_api_key", "")
            else:
                # Empty credential input means "leave unchanged", not "delete".
                Setting.set("openai_api_key", encrypt_secret(current_app, current_api_key) if current_api_key else "")
            Setting.set("openai_model", model)
            db.session.commit()
            if clear_api_key:
                flash("کلید سراسری دستیار پاک شد.", "info")
            elif api_key:
                flash("کلید و تنظیمات دستیار هوشمند ذخیره شد.", "success")
            else:
                flash("تنظیمات دستیار هوشمند ذخیره شد؛ کلید فعلی حفظ شد.", "success")
        elif form_id == "user_ai":
            # ذخیره تنظیمات شخصی هر کاربر
            user_api_key = (request.form.get("user_openai_api_key") or "").strip()
            clear_user_api_key = request.form.get("clear_user_openai_api_key") == "1"
            if user_api_key:
                user_settings.openai_api_key = encrypt_secret(current_app, user_api_key)
            elif clear_user_api_key:
                user_settings.openai_api_key = None
            # Empty credential input otherwise leaves the existing key unchanged.
            user_settings.openai_model = (request.form.get("user_openai_model") or "").strip() or None
            user_settings.system_prompt = (request.form.get("user_system_prompt") or "").strip() or None
            
            temp = request.form.get("user_temperature")
            if temp:
                try:
                    user_settings.temperature = float(temp)
                except:
                    user_settings.temperature = 0.7
            else:
                user_settings.temperature = 0.7
            
            max_tok = request.form.get("user_max_tokens")
            if max_tok:
                try:
                    user_settings.max_tokens = int(max_tok)
                except:
                    user_settings.max_tokens = None
            else:
                user_settings.max_tokens = None
            
            db.session.commit()
            flash("✅ تنظیمات شخصی شما ذخیره شد.", "success")
        return redirect(URL_PREFIX + "/settings")
    
    return render_template(
        "settings.html",
        prefix=URL_PREFIX,
        pos_choices=POS_DEVICE_CHOICES,
        selected_pos=current_key,
        selected_theme=_ui_theme_key(),
        search_sort_selected=_search_sort_key(),
        price_mode_selected=_price_display_mode(),
        dashboard_widgets_selected=_dashboard_widgets(),
        allow_negative_selected=_allow_negative_sales(),
        assistant_model_selected=_assistant_model(),
        assistant_model_choices=ASSISTANT_MODEL_CHOICES,
        assistant_api_mask=_mask_secret(_openai_api_key()),
        assistant_api_has=bool(_openai_api_key()),
        user_api_mask=_mask_secret(_user_openai_api_key(user_settings)),
        user_api_has=bool(_user_openai_api_key(user_settings)),
        user_settings=user_settings,
    )

@app.route(URL_PREFIX + "/assistant")
@login_required
def assistant_home():
    ensure_permission("assistant")
    api_ready = _assistant_api_ready()
    return render_template(
        "assistant.html",
        prefix=URL_PREFIX,
        api_ready=api_ready,
        assistant_model_label=dict(ASSISTANT_MODEL_CHOICES).get(_assistant_model(), _assistant_model()),
    )

@app.route(URL_PREFIX + "/assistant/api/chat", methods=["POST"])
@login_required
def assistant_chat():
    ensure_permission("assistant")
    if OpenAI is None:
        return jsonify({"status": "error", "message": "کتابخانه openai در محیط نصب نشده است."}), 500
    if not _assistant_api_ready():
        return jsonify({"status": "error", "message": "ابتدا کلید API سراسری یا شخصی را در تنظیمات ثبت کنید."}), 400

    payload = request.get_json(silent=True) or {}
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return jsonify({"status": "error", "message": "ساختار پیام نامعتبر است."}), 400

    try:
        result = _call_openai_assistant(messages)
    except Exception as exc:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(exc)}), 500

    reply_text = result.get("reply", "")
    needs_confirmation = bool(result.get("needs_confirmation"))
    invoice_preview = None
    ticket = None
    applied = False
    applied_invoice_number = None
    apply_error = None

    invoice_payload = result.get("invoice") if isinstance(result.get("invoice"), dict) else None
    if invoice_payload:
        try:
            plan = _prepare_invoice_plan(invoice_payload)
        except ValueError as exc:
            db.session.rollback()
            return jsonify({"status": "error", "message": str(exc)}), 400
        invoice_preview = plan
        missing_partner = plan.get("missing_partner")
        missing_items = plan.get("missing_items")
        required_permission = _assistant_plan_permission(plan)
        if missing_partner or missing_items or (
            required_permission and not has_permission(required_permission)
        ):
            needs_confirmation = True

        if not needs_confirmation:
            try:
                outcome = _apply_invoice_plan(plan)
                applied = True
                applied_invoice_number = outcome["invoice"].number
                reply_text = (reply_text or "") + f"\nفاکتور «{applied_invoice_number}» با موفقیت ثبت شد."
            except Exception as exc:
                db.session.rollback()
                apply_error = str(exc)
                needs_confirmation = True

        if needs_confirmation:
            ticket = _register_ai_task(
                current_user.username,
                {
                    "plan": plan,
                    "reply": reply_text,
                    "apply_error": apply_error,
                },
            )

    # handle assistant-suggested cash/transfer documents (receive/payment)
    cash_payload = result.get("cash") if isinstance(result.get("cash"), dict) else None
    applied_cash_number = None
    if cash_payload:
        try:
            cplan = _prepare_cash_plan(cash_payload)
        except ValueError as exc:
            db.session.rollback()
            return jsonify({"status": "error", "message": str(exc)}), 400
        # if assistant couldn't determine direction or missing critical info, require confirmation
        required_permission = _assistant_plan_permission(cplan)
        if cplan.get("missing_person") or (
            not cplan.get("bank_account") and (cplan.get("method") or '').lower() == 'bank'
        ) or (
            required_permission and not has_permission(required_permission)
        ):
            needs_confirmation = True

        if not needs_confirmation:
            try:
                outcome_cash = _apply_cash_plan(cplan)
                applied = True
                applied_cash_number = outcome_cash["doc"].number
                reply_text = (reply_text or "") + f"\nسند نقدی «{applied_cash_number}» با موفقیت ثبت شد."
            except Exception as exc:
                db.session.rollback()
                apply_error = str(exc)
                needs_confirmation = True

        if needs_confirmation and not ticket:
            ticket = _register_ai_task(
                current_user.username,
                {
                    "plan": cplan,
                    "reply": reply_text,
                    "apply_error": apply_error,
                },
            )

    actions_summary = None
    actions_preview = None
    actions_payload = result.get("actions") if isinstance(result.get("actions"), list) else []
    if actions_payload:
        if ticket:
            actions_summary = {
                "applied": 0,
                "failed": len(actions_payload),
                "messages": [],
                "errors": ["برای جلوگیری از اجرای همزمان، تغییرات فایل پروژه را جداگانه تأیید کنید."],
            }
        elif not is_admin():
            actions_summary = {
                "applied": 0,
                "failed": len(actions_payload),
                "messages": [],
                "errors": ["اعمال تغییرات فایل پروژه فقط برای مدیر سیستم مجاز است."],
            }
        else:
            safe_actions = []
            preview_errors = []
            for action in actions_payload:
                if not isinstance(action, dict):
                    preview_errors.append("ساختار یکی از عملیات‌ها نامعتبر است.")
                    continue
                try:
                    _resolve_project_path((action.get("path") or "").strip())
                    safe_actions.append(action)
                except Exception as exc:
                    preview_errors.append(str(exc))
            if preview_errors:
                actions_summary = {
                    "applied": 0,
                    "failed": len(preview_errors),
                    "messages": [],
                    "errors": preview_errors,
                }
            elif safe_actions:
                actions_preview = safe_actions
                needs_confirmation = True
                ticket = _register_ai_task(
                    current_user.username,
                    {
                        "actions": safe_actions,
                        "reply": reply_text,
                    },
                )

    response = {
        "status": "ok",
        "reply": reply_text,
        "needs_confirmation": needs_confirmation,
        "invoice_preview": invoice_preview,
        "ticket": ticket,
        "applied": applied,
        "invoice_number": applied_invoice_number,
        "uncertain_fields": result.get("uncertain_fields", []),
        "follow_up": result.get("follow_up"),
        "apply_error": apply_error,
        "actions_summary": actions_summary,
        "actions_preview": actions_preview,
        "actions_applied": bool(actions_summary and actions_summary.get("applied")),
    }
    return jsonify(response)

@app.route(URL_PREFIX + "/assistant/api/apply", methods=["POST"])
@login_required
def assistant_apply():
    ensure_permission("assistant")
    payload = request.get_json(silent=True) or {}
    token = (payload.get("ticket") or "").strip()
    if not token:
        return jsonify({"status": "error", "message": "توکن یافت نشد."}), 400
    task = _pop_ai_task(current_user.username, token)
    if not task:
        return jsonify({"status": "error", "message": "توکن منقضی یا نامعتبر است."}), 400
    plan = task.get("plan")
    actions = task.get("actions")
    if actions:
        if not is_admin():
            return jsonify({"status": "error", "message": "اعمال تغییرات فایل پروژه فقط برای مدیر سیستم مجاز است."}), 403
        summary = _apply_assistant_actions(actions)
        if summary.get("errors"):
            return jsonify({"status": "error", "message": "برخی تغییرات اعمال نشد.", "actions_summary": summary}), 400
        return jsonify({"status": "ok", "actions_summary": summary})

    if not plan:
        return jsonify({"status": "error", "message": "اطلاعات عملیات موجود نیست."}), 400

    # Decide whether this is an invoice plan or cash plan.
    try:
        required_permission = _assistant_plan_permission(plan)
        if not required_permission:
            return jsonify({"status": "error", "message": "نوع عملیات یا مجوز ماژول مشخص نیست."}), 400
        if not has_permission(required_permission):
            return jsonify({"status": "error", "message": "برای اعمال این عملیات مجوز ماژول مربوطه را ندارید."}), 403

        if isinstance(plan, dict) and plan.get("items"):
            outcome = _apply_invoice_plan(plan)
            invoice = outcome["invoice"]
            return jsonify({"status": "ok", "invoice_number": invoice.number, "invoice_id": invoice.id})
        elif isinstance(plan, dict) and (plan.get("amount") is not None and plan.get("person")):
            outcome = _apply_cash_plan(plan)
            doc = outcome.get("doc")
            return jsonify({"status": "ok", "cash_number": doc.number, "cash_id": doc.id})
        else:
            return jsonify({"status": "error", "message": "نوع عملیات قابل اعمال نیست."}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(exc)}), 400

@app.route(URL_PREFIX + "/admin", methods=["GET"])
@login_required
def admin_stub():
    admin_required()
    return render_template("admin/dashboard.html", prefix=URL_PREFIX)


@app.route(URL_PREFIX + "/admin/update_from_git", methods=["POST"])
@login_required
def admin_update_from_git():
    admin_required()
    EXPECTED_REMOTE = "https://github.com/mahdiyarp/hesabpak.git"
    repo_dir = str(PROJECT_ROOT)
    result = {"ok": False, "steps": []}
    def step(msg, ok=True):
        result["steps"].append({"msg": msg, "ok": bool(ok)})

    try:
        # verify git repo
        git_dir = PROJECT_ROOT / ".git"
        if not git_dir.exists():
            step("Not a git repository", ok=False)
            return jsonify(result), 400

        # check origin url
        try:
            out = subprocess.check_output(shlex.split(f"git -C {shlex.quote(repo_dir)} remote get-url origin"), stderr=subprocess.STDOUT, text=True).strip()
        except subprocess.CalledProcessError as e:
            step(f"failed to read git remote: {e.output}", ok=False)
            return jsonify(result), 500

        normalized_remote = out.strip().rstrip("/")
        expected_remote = EXPECTED_REMOTE.rstrip("/")
        if normalized_remote != expected_remote:
            step(f"remote origin URL mismatch: {out}", ok=False)
            return jsonify(result), 403
        step(f"remote origin OK: {out}")

        # Create a consistent full backup before changing code.
        try:
            backup_path = create_full_backup(
                app,
                user=getattr(current_user, "username", "admin"),
                reason="pre-git-update",
            )
            step(f"Full backup created: {backup_path}")
        except Exception as e:
            step(f"Full backup failed: {e}", ok=False)
            return jsonify(result), 500

        # ensure clean working tree
        status = subprocess.check_output(shlex.split(f"git -C {shlex.quote(repo_dir)} status --porcelain"), text=True)
        if status.strip():
            step("Local uncommitted changes present; aborting to avoid data loss", ok=False)
            return jsonify(result), 409
        step("Working tree clean")

        # fetch & fast-forward pull
        try:
            subprocess.check_call(shlex.split(f"git -C {shlex.quote(repo_dir)} fetch origin --prune"))
            branch = os.environ.get('GIT_BRANCH', 'main')
            subprocess.check_call(shlex.split(f"git -C {shlex.quote(repo_dir)} pull --ff-only origin {shlex.quote(branch)}"))
            step("git pull --ff-only origin succeeded")
        except subprocess.CalledProcessError as e:
            step(f"git pull failed: {e}", ok=False)
            return jsonify(result), 500

        # install requirements inside venv if present
        venv_dir = PROJECT_ROOT / "venv"
        if venv_dir.exists() and (venv_dir / "bin" / "python").exists():
            py = str(venv_dir / "bin" / "python")
            try:
                subprocess.check_call([py, "-m", "pip", "install", "-r", str(PROJECT_ROOT / "requirements.txt")])
                step("requirements installed inside venv")
            except subprocess.CalledProcessError as e:
                step(f"pip install in venv failed: {e}", ok=False)
                return jsonify(result), 500
        else:
            step("No venv found; skipping pip install")

        # restart (Passenger)
        try:
            tmpdir = PROJECT_ROOT / "tmp"
            tmpdir.mkdir(parents=True, exist_ok=True)
            (tmpdir / "restart.txt").write_text(datetime.utcnow().isoformat())
            step("Passenger restart triggered (tmp/restart.txt)")
        except Exception as e:
            step(f"failed to trigger restart: {e}", ok=False)
            return jsonify(result), 500

        result["ok"] = all(bool(s.get("ok")) for s in result["steps"])
        return jsonify(result)

    except Exception:
        app.logger.exception("update_from_git failed")
        step("unexpected error; see server logs", ok=False)
        return jsonify(result), 500


@app.route(URL_PREFIX + "/admin/site-views", methods=["GET"])
@login_required
def admin_site_views():
    admin_required()
    try:
        page = int(request.args.get("page") or 1)
    except Exception:
        page = 1
    per_page = 100
    q = db.session.query(SiteView).order_by(SiteView.created_at.desc())
    total = q.count()
    rows = q.offset((page-1)*per_page).limit(per_page).all()
    return render_template("admin/site_views.html", prefix=URL_PREFIX, rows=rows, page=page, per_page=per_page, total=total)


@app.route(URL_PREFIX + "/admin/ledger", methods=["GET"])
@login_required
def admin_ledger():
    admin_required()
    try:
        page = int(request.args.get("page") or 1)
    except Exception:
        page = 1
    per_page = 100
    q = db.session.query(LedgerEntry).order_by(LedgerEntry.id.desc())
    total = q.count()
    rows = q.offset((page - 1) * per_page).limit(per_page).all()
    return render_template("admin/ledger.html", prefix=URL_PREFIX, rows=rows, page=page, per_page=per_page, total=total)


@app.route(URL_PREFIX + "/admin/users", methods=["GET", "POST"])
@login_required
def admin_users():
    admin_required()
    catalog = load_users_catalog()

    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()
        username = (request.form.get("username") or "").strip()

        if action == "delete":
            if not username:
                flash("نام کاربری ارسال نشده است.", "danger")
            elif username == ADMIN_USERNAME:
                flash("کاربر مدیر اصلی قابل حذف نیست.", "warning")
            elif username not in catalog:
                flash("کاربر یافت نشد.", "danger")
            elif _would_remove_last_active_admin(catalog, username, deleting=True):
                flash("آخرین مدیر فعال قابل حذف نیست؛ ابتدا یک مدیر فعال دیگر ایجاد کنید.", "warning")
            else:
                catalog.pop(username, None)
                save_users_catalog(catalog)
                flash(f"کاربر «{username}» حذف شد.", "success")
            return redirect(URL_PREFIX + "/admin/users")

        role = (request.form.get("role") or "staff").strip().lower()
        if role not in USER_ROLE_LABELS:
            role = "staff"
        requested_perms = request.form.getlist("permissions")
        is_active = request.form.get("is_active") == "on"
        email = (request.form.get("email") or "").strip()

        if action == "update":
            if not username or username not in catalog:
                flash("کاربر مورد نظر یافت نشد.", "danger")
                return redirect(URL_PREFIX + "/admin/users")

            entry = catalog[username]
            if username == ADMIN_USERNAME:
                role = "admin"
                is_active = True
            elif _would_remove_last_active_admin(
                catalog,
                username,
                new_role=role,
                new_is_active=is_active,
            ):
                flash("آخرین مدیر فعال را نمی‌توان غیرفعال یا تنزل نقش داد.", "warning")
                return redirect(URL_PREFIX + "/admin/users")
            entry["role"] = role
            entry["permissions"] = _permissions_for_role(role, requested_perms)
            entry["is_active"] = is_active
            entry["email"] = email

            new_password = (request.form.get("password") or "").strip()
            if new_password:
                entry["password"] = new_password

            save_users_catalog(catalog)
            flash("تغییرات ذخیره شد.", "success")
            return redirect(URL_PREFIX + "/admin/users")

        # default: create
        password = (request.form.get("password") or "").strip()
        confirm = (request.form.get("password_confirm") or "").strip()

        if not username:
            flash("نام کاربری را وارد کنید.", "danger")
            return redirect(URL_PREFIX + "/admin/users")
        if username in catalog:
            flash("کاربری با این نام از قبل وجود دارد.", "warning")
            return redirect(URL_PREFIX + "/admin/users")
        if not password:
            flash("رمز عبور را وارد کنید.", "danger")
            return redirect(URL_PREFIX + "/admin/users")
        if password != confirm:
            flash("تکرار رمز عبور با مقدار اولیه یکسان نیست.", "danger")
            return redirect(URL_PREFIX + "/admin/users")

        entry = _normalize_user_entry(
            username,
            {
                "password": password,
                "role": role,
                "permissions": requested_perms,
                "is_active": is_active,
                "email": email,
            },
        )
        catalog[username] = entry
        save_users_catalog(catalog)
        flash(f"کاربر «{username}» ایجاد شد.", "success")
        return redirect(URL_PREFIX + "/admin/users")

    users = sorted(
        catalog.values(),
        key=lambda u: (0 if u["username"] == ADMIN_USERNAME else 1, u["username"].lower()),
    )
    return render_template(
        "admin/users.html",
        prefix=URL_PREFIX,
        users=users,
        role_labels=USER_ROLE_LABELS,
        permission_labels=PERMISSION_LABELS,
        assignable_permissions=ASSIGNABLE_PERMISSIONS,
        admin_username=ADMIN_USERNAME,
    )


@app.route(URL_PREFIX + "/admin/modules", methods=["GET", "POST"])
@login_required
def admin_modules():
    admin_required()
    # load current config from settings
    cfg = {
        "enable_text_commands": Setting.get("assistant.enable_text_commands", "1") == "1",
        "enable_image_commands": Setting.get("assistant.enable_image_commands", "1") == "1",
        "enable_auto_create": Setting.get("assistant.enable_auto_create", "0") == "1",
    }
    if request.method == "POST":
        enable_text = "1" if request.form.get("enable_text_commands") == "on" else "0"
        enable_image = "1" if request.form.get("enable_image_commands") == "on" else "0"
        enable_auto = "1" if request.form.get("enable_auto_create") == "on" else "0"
        Setting.set("assistant.enable_text_commands", enable_text)
        Setting.set("assistant.enable_image_commands", enable_image)
        Setting.set("assistant.enable_auto_create", enable_auto)
        db.session.commit()
        flash("تنظیمات ماژول‌ها ذخیره شد.", "success")
        return redirect(URL_PREFIX + "/admin/modules")

    return render_template("admin/module_designer.html", prefix=URL_PREFIX, config=cfg)


@app.route(URL_PREFIX + "/admin/assistant-tokens", methods=["GET", "POST"])
@login_required
def admin_assistant_tokens():
    admin_required()

    # load users from catalog
    catalog = load_users_catalog()

    # handle form submissions
    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()
        if action == "update_global":
            key = (request.form.get("global_api_key") or "").strip()
            if key:
                Setting.set("openai_api_key", encrypt_secret(current_app, key))
                db.session.commit()
                flash("کلید سراسری API ذخیره شد.", "success")
            else:
                Setting.set("openai_api_key", "")
                db.session.commit()
                flash("کلید سراسری API حذف شد.", "success")
            return redirect(URL_PREFIX + "/admin/assistant-tokens")

        if action == "update_user":
            username = (request.form.get("username") or "").strip()
            user_key = (request.form.get("user_api_key") or "").strip()
            if not username or username not in catalog:
                flash("کاربر نامعتبر است.", "danger")
                return redirect(URL_PREFIX + "/admin/assistant-tokens")
            us = UserSettings.get_for_user(username)
            us.openai_api_key = encrypt_secret(current_app, user_key) if user_key else None
            db.session.commit()
            flash(f"کلید کاربر «{username}» به‌روزرسانی شد.", "success")
            return redirect(URL_PREFIX + "/admin/assistant-tokens")

        if action == "clear_user":
            username = (request.form.get("username") or "").strip()
            if not username or username not in catalog:
                flash("کاربر نامعتبر است.", "danger")
                return redirect(URL_PREFIX + "/admin/assistant-tokens")
            us = UserSettings.get_for_user(username)
            us.openai_api_key = None
            db.session.commit()
            flash(f"کلید کاربر «{username}» پاک شد.", "success")
            return redirect(URL_PREFIX + "/admin/assistant-tokens")

    # GET: show current keys (masked)
    global_key = _openai_api_key()
    users = []
    for username, meta in sorted(catalog.items(), key=lambda kv: kv[0].lower()):
        us = UserSettings.get_for_user(username)
        user_key = _user_openai_api_key(us)
        users.append({
            "username": username,
            "role": meta.get("role"),
            "api_key_masked": _mask_secret(user_key),
            "has_key": bool(user_key),
        })

    return render_template("admin/assistant_tokens.html", prefix=URL_PREFIX, global_key_masked=_mask_secret(global_key), users=users)


@app.route(URL_PREFIX + "/admin/assistant-drafts", methods=["GET"])
@login_required
def admin_assistant_drafts():
    admin_required()
    drafts_raw = Setting.get("assistant.drafts", "[]") or "[]"
    try:
        drafts = json.loads(drafts_raw)
    except Exception:
        drafts = []
    # add index for actions
    for i, d in enumerate(drafts):
        d["_idx"] = i
    return render_template("admin/assistant_drafts.html", prefix=URL_PREFIX, drafts=drafts)


@app.route(URL_PREFIX + "/admin/assistant-drafts/<int:idx>", methods=["GET", "POST"]) 
@login_required
def admin_assistant_draft_view(idx: int):
    admin_required()
    drafts_raw = Setting.get("assistant.drafts", "[]") or "[]"
    try:
        drafts = json.loads(drafts_raw)
    except Exception:
        drafts = []
    if idx < 0 or idx >= len(drafts):
        flash("پیش‌نویس پیدا نشد.", "warning")
        return redirect(URL_PREFIX + "/admin/assistant-drafts")

    draft = drafts[idx]

    # handle actions
    if request.method == "POST":
        action = request.form.get("action")
        if action == "delete":
            drafts.pop(idx)
            Setting.set("assistant.drafts", json.dumps(drafts, ensure_ascii=False))
            db.session.commit()
            flash("پیش‌نویس حذف شد.", "success")
            return redirect(URL_PREFIX + "/admin/assistant-drafts")

        if action == "export_csv":
            # export this draft as CSV
            import io, csv
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["field", "value"])
            for k, v in draft.items():
                if k.startswith("_"):
                    continue
                w.writerow([_csv_safe_cell(k), _csv_safe_cell(v)])
            csv_data = buf.getvalue()
            return (csv_data, 200, {
                'Content-Type': 'text/csv; charset=utf-8',
                'Content-Disposition': f'attachment; filename="draft_{idx}.csv"'
            })

        if action == "export_xls":
            # quick Excel-compatible HTML table
            rows = []
            for k, v in draft.items():
                if k.startswith("_"):
                    continue
                safe_key = _csv_safe_cell(k)
                safe_value = _csv_safe_cell(v)
                rows.append(f"<tr><td>{escape(safe_key)}</td><td>{escape(safe_value)}</td></tr>")
            html = """
            <html><head><meta charset='utf-8'></head><body>
            <table>%s</table>
            </body></html>
            """ % ("\n".join(rows))
            return (html, 200, {
                'Content-Type': 'application/vnd.ms-excel; charset=utf-8',
                'Content-Disposition': f'attachment; filename="draft_{idx}.xls"'
            })

        if action == "print":
            # render printable HTML
            return render_template("admin/assistant_draft_print.html", prefix=URL_PREFIX, draft=draft)

        if action == "apply":
            # attempt to apply draft only if it's structured JSON plan
            txt = draft.get("text") or draft.get("extracted_text") or ""
            try:
                payload = json.loads(txt)
            except Exception:
                payload = None

            if not payload or not isinstance(payload, dict):
                flash("پیش‌نویس قابل اعمال نیست — متن ساختاریافته (JSON) لازم است.", "warning")
                return redirect(URL_PREFIX + f"/admin/assistant-drafts/{idx}")

            kind = payload.get("kind") or payload.get("type")
            try:
                if kind == "invoice":
                    outcome = _apply_invoice_plan(payload)
                    flash(f"فاکتور ایجاد شد (#{outcome['invoice'].id}).", "success")
                elif kind in ("receive", "payment"):
                    outcome = _apply_cash_plan(payload)
                    flash(f"اسناد نقدی ایجاد شد (id={outcome['doc'].id}).", "success")
                else:
                    flash("نوع سند قابل اعمال مشخص نیست.", "warning")
            except Exception as e:
                db.session.rollback()
                flash(f"خطا هنگام اعمال پیش‌نویس: {str(e)}", "danger")
            # remove draft after attempt
            try:
                drafts.pop(idx)
                Setting.set("assistant.drafts", json.dumps(drafts, ensure_ascii=False))
                db.session.commit()
            except Exception:
                db.session.rollback()

            return redirect(URL_PREFIX + "/admin/assistant-drafts")

    # GET: show view
    return render_template("admin/assistant_draft_view.html", prefix=URL_PREFIX, draft=draft, idx=idx)


@app.route(URL_PREFIX + "/assistant/api/parse", methods=["POST"])
@login_required
def assistant_parse():
    # permissions: allow admin or assistant permission
    ensure_permission("assistant")

    text = (request.form.get("text") or "").strip()
    file = request.files.get("image")
    saved_path = None
    ocr_text = ""
    if file and (file.filename or "").strip():
        from werkzeug.utils import secure_filename

        max_image_mb = float(os.environ.get("ASSISTANT_MAX_IMAGE_MB", "2"))
        max_image_bytes = max(1, int(max_image_mb * 1024 * 1024))
        allowed_mimes = {"image/png", "image/jpeg", "image/webp", "image/gif"}
        content_type = (file.mimetype or "").strip().lower()
        if content_type not in allowed_mimes:
            return jsonify({
                "ok": False,
                "message": "فرمت تصویر پشتیبانی نمی‌شود. فقط PNG، JPEG، WebP و GIF مجاز است.",
            }), 400

        stream = file.stream
        try:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(0)
        except Exception:
            size = 0
        if size <= 0 or size > max_image_bytes:
            return jsonify({
                "ok": False,
                "message": f"حجم تصویر باید بین ۱ بایت و {max_image_mb:g} مگابایت باشد.",
            }), 413

        fname = secure_filename(file.filename or "")
        if not fname:
            fname = f"upload_{uuid.uuid4().hex}.img"
        suffix = Path(fname).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            ext_by_mime = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/webp": ".webp",
                "image/gif": ".gif",
            }
            fname = f"{Path(fname).stem or 'upload'}{ext_by_mime[content_type]}"

        unique_prefix = f"{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:12]}"
        dest = Path(app.config.get("ASSISTANT_UPLOAD_DIR")) / f"{unique_prefix}_{fname}"
        file.save(str(dest))
        saved_path = str(dest)
        # try OCR if pytesseract available
        try:
            import pytesseract
            from PIL import Image
            ocr_text = pytesseract.image_to_string(Image.open(str(dest))) or ""
        except Exception:
            # fallback: no OCR available
            ocr_text = ""

    # combine text + ocr_text for detection
    combined = " ".join([t for t in [text, ocr_text] if t])

    # Improved heuristics for detection: look for strong indicators of invoices,
    # receipts (دریافت/رسید), payments (پرداخت), and cheques. Use counts and
    # priority so short receipt texts are not misclassified as invoices.
    lower = combined.lower()
    kind = "unknown"

    # Strong indicators
    has_invoice = any(k in lower for k in ["فاکتور", "invoice", "صورت حساب"])
    has_receipt = any(k in lower for k in ["رسید", "رسید پرداخت", "رسید دریافت", "receipt"]) or any(k in lower for k in ["دریافت", "وصول", "واریز شد"]) 
    has_payment = any(k in lower for k in ["پرداخت", "payment", "پرداخت شد"]) 
    has_cheque = any(k in lower for k in ["چک", "چک صیاد", "شماره چک", "سررسید"]) 

    # If explicit invoice markers present and no receipt/payment clues -> invoice
    if has_invoice and not (has_receipt or has_payment):
        kind = "invoice"
    # If receipt/payment markers present and no invoice marker -> treat as cash doc
    elif has_receipt or has_payment:
        # prefer 'receive' if words like 'دریافت' or 'وصول' or 'رسید' exist
        if has_receipt and not has_payment:
            kind = "receive"
        elif has_payment and not has_receipt:
            kind = "payment"
        else:
            # ambiguous: if both appear, try to infer by context: look for 'پرداخت' near numbers
            if "پرداخت" in lower and not "فاکتور" in lower:
                kind = "payment"
            else:
                kind = "receive"
    # If cheque indicators present without invoice markers, mark as receive/payment
    elif has_cheque:
        # default to receive; downstream logic can flip based on context
        kind = "receive"
    elif any(k in lower for k in ["کالا", "محصول", "item"]):
        kind = "item"
    elif any(k in lower for k in ["شخص", "طرف حساب", "person"]):
        kind = "person"

    # prepare a draft preview payload
    preview = {
        "detected_kind": kind,
        "extracted_text": combined,
        "saved_file": saved_path,
    }

    # If auto-create enabled, create a draft (not final commit)
    auto_create = Setting.get("assistant.enable_auto_create", "0") == "1"
    draft_id = None
    if auto_create and kind in ("invoice", "receive", "payment", "item", "person"):
        # store a minimal draft in settings as JSON list (simple approach)
        drafts_raw = Setting.get("assistant.drafts", "[]") or "[]"
        try:
            drafts = json.loads(drafts_raw)
        except Exception:
            drafts = []
        d = {"kind": kind, "text": combined, "file": saved_path, "created_by": getattr(current_user, "username", None), "ts": datetime.utcnow().isoformat()}
        drafts.insert(0, d)
        Setting.set("assistant.drafts", json.dumps(drafts, ensure_ascii=False))
        db.session.commit()
        draft_id = 0

    return jsonify({"ok": True, "preview": preview, "draft_id": draft_id})


@app.route(URL_PREFIX + "/admin/cashboxes", methods=["GET", "POST"])
@login_required
def admin_cashboxes():
    admin_required()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        kind = (request.form.get("kind") or "cash").strip().lower()
        bank_name = (request.form.get("bank_name") or "").strip() or None
        account_no = (request.form.get("account_no") or "").strip() or None
        iban = (request.form.get("iban") or "").strip() or None
        description = (request.form.get("description") or "").strip() or None

        if kind not in ("cash", "bank"):
            kind = "cash"

        if not name:
            flash("نام صندوق/حساب را وارد کنید.", "danger")
        else:
            exists = CashBox.query.filter(func.lower(CashBox.name) == name.lower()).first()
            if exists:
                flash("صندوقی با این نام از قبل ثبت شده است.", "warning")
            else:
                box = CashBox(
                    name=name,
                    kind=kind,
                    bank_name=bank_name if kind == "bank" else None,
                    account_no=account_no if kind == "bank" else None,
                    iban=iban if kind == "bank" else None,
                    description=description,
                    is_active=True,
                )
                db.session.add(box)
                db.session.commit()
                flash("صندوق جدید ثبت شد.", "success")
        return redirect(URL_PREFIX + "/admin/cashboxes")

    boxes = (
        CashBox.query.order_by(CashBox.kind.desc(), CashBox.is_active.desc(), CashBox.name.asc())
        .all()
    )
    totals_rows = (
        db.session.query(
            CashDoc.cashbox_id,
            CashDoc.doc_type,
            func.coalesce(func.sum(CashDoc.amount), 0.0),
        )
        .filter(CashDoc.cashbox_id.isnot(None))
        .group_by(CashDoc.cashbox_id, CashDoc.doc_type)
        .all()
    )
    totals_map = {}
    for box_id, doc_type, total in totals_rows:
        if box_id not in totals_map:
            totals_map[box_id] = {"receive": 0.0, "payment": 0.0}
        totals_map[box_id][doc_type] = float(total or 0.0)
    grand_net = 0.0
    for box in boxes:
        meta = totals_map.get(box.id, {"receive": 0.0, "payment": 0.0})
        net = meta.get("receive", 0.0) - meta.get("payment", 0.0)
        if box.is_active:
            grand_net += net
    grand_net = float(grand_net)
    return render_template(
        "admin/cashboxes.html",
        prefix=URL_PREFIX,
        boxes=boxes,
        cash_totals=totals_map,
        cash_grand_total=grand_net,
    )


@app.route(URL_PREFIX + "/admin/cashboxes/<int:box_id>/delete", methods=["POST"])
@login_required
def admin_cashboxes_delete(box_id):
    admin_required()
    box = CashBox.query.get_or_404(box_id)
    usage_exists = (
        db.session.query(CashDoc.id)
        .filter(CashDoc.cashbox_id == box.id)
        .limit(1)
        .first()
    )
    if usage_exists:
        box.is_active = False
        flash("به دلیل استفاده در اسناد، صندوق غیرفعال شد.", "warning")
    else:
        db.session.delete(box)
        flash("صندوق حذف شد.", "success")
    db.session.commit()
    return redirect(URL_PREFIX + "/admin/cashboxes")


@app.route(URL_PREFIX + "/admin/cashboxes/remove-defaults", methods=["POST"]) 
@login_required
def admin_cashboxes_remove_defaults():
    """Remove or deactivate common pre-seeded default cashbox names.

    This endpoint looks for boxes with common default names (english and persian)
    and deletes them if unused or deactivates them if they have usage.
    """
    admin_required()
    DEFAULT_NAMES = {"cash", "pos", "bank", "نقد", "بانک", "پوز"}
    boxes = CashBox.query.all()
    removed = 0
    deactivated = 0
    for box in boxes:
        if (box.name or "").strip().lower() in DEFAULT_NAMES:
            usage_exists = (
                db.session.query(CashDoc.id)
                .filter(CashDoc.cashbox_id == box.id)
                .limit(1)
                .first()
            )
            if usage_exists:
                if box.is_active:
                    box.is_active = False
                    deactivated += 1
            else:
                db.session.delete(box)
                removed += 1
    db.session.commit()
    msg_parts = []
    if removed:
        msg_parts.append(f"{removed} صندوق پیش‌فرض حذف شد.")
    if deactivated:
        msg_parts.append(f"{deactivated} صندوق به‌علت استفاده غیرفعال شد.")
    if not msg_parts:
        flash("صندوق پیش‌فرضی پیدا نشد.", "info")
    else:
        flash("; ".join(msg_parts), "success")
    return redirect(URL_PREFIX + "/admin/cashboxes")

# ----------------- Utility APIs -----------------
@app.route(URL_PREFIX + "/api/num2words", methods=["GET"])
@login_required
def api_num2words():
    amount = request.args.get("amount", "0")
    try:
        text = amount_to_toman_words(_to_float(amount, 0.0))
        return jsonify({"ok": True, "text": text})
    except Exception:
        return jsonify({"ok": False, "text": ""}), 400


@app.route(URL_PREFIX + "/api/audit/log", methods=["POST"])
@login_required
def api_audit_log():
    data = request.get_json(silent=True) or {}
    context = (data.get("context") or "general").strip()[:64]
    action = (data.get("action") or "unknown").strip()[:64]
    payload = data.get("payload")
    ip = request.remote_addr or ""
    username = getattr(current_user, "username", None)

    serialized_payload = None
    if payload is not None:
        serialized_payload = json.dumps(payload, ensure_ascii=False, default=str)
        if len(serialized_payload.encode("utf-8")) > 32768:
            return jsonify({"ok": False, "message": "payload ثبت گزارش بیش از حد بزرگ است."}), 413

    event = AuditEvent(
        user=username,
        ip_address=ip,
        context=context or "general",
        action=action or "unknown",
        payload=serialized_payload,
    )

    db.session.add(event)
    db.session.commit()

    event_payload = {
        "id": event.id,
        "context": context,
        "action": action,
        "payload": payload,
        "user": username,
        "ip": ip,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }

    try:
        autosave_record(current_app, "AuditEvent", event.id, event_payload)
        current_app.logger.info(f"[audit] {context}/{action} by {username} ({ip})")
    except Exception as exc:
        current_app.logger.exception(f"audit autosave failed: {exc}")

    return jsonify({"ok": True})


@app.route(URL_PREFIX + "/api/now", methods=["GET"])
@login_required
def api_now():
    info = _now_info()
    return jsonify(
        {
            "ok": True,
            "greg_date": info["greg_date"],
            "jalali_date": info["jalali_date"],
            "jalali_reference": info["jalali_reference"],
            "timestamp": int(info["datetime"].timestamp()),
        }
    )


@app.route(URL_PREFIX + "/api/rates", methods=["GET"])
@login_required
def api_rates_get():
    """بازگرداندن اسنپ‌شات نرخ‌ها (مقداری که قبلاً ذخیره شده یا پیش‌فرض)."""
    try:
        snap = rates_utils.get_rate_snapshot()
        return jsonify({"ok": True, "rates": snap})
    except Exception:
        return jsonify({"ok": False, "rates": {}}), 500


@app.route(URL_PREFIX + "/api/rates", methods=["POST"])
@login_required
def api_rates_set():
    """تنها برای ادمین: تنظیم دستی نرخ‌ها (MVP).
    بدنه JSON می‌تواند دقیقاً همان ساختار rates.json را داشته باشد.
    """
    try:
        admin_required()
    except Exception:
        return jsonify({"ok": False, "message": "forbidden"}), 403
    payload = request.get_json(silent=True) or {}
    try:
        rates_utils.save_rates(payload)
        return jsonify({"ok": True})
    except Exception as exc:
        app.logger.exception("failed to save rates: %s", exc)
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.route(URL_PREFIX + "/api/bank_detect", methods=["POST"])
@login_required
def api_bank_detect():
    data = request.get_json(silent=True) or {}
    val = (data.get('value') or data.get('q') or '').strip()
    if not val:
        return jsonify({"ok": False, "message": "no value provided"}), 400
    try:
        info = bank_utils.detect_bank(val)
        return jsonify({"ok": True, "result": info})
    except Exception as exc:
        app.logger.exception("bank_detect failed: %s", exc)
        return jsonify({"ok": False, "message": str(exc)}), 500


# ----------------- Search API -----------------
def _search_targets_for_permissions(targets, permissions, admin=False):
    """Limit search record types to the modules visible to a user."""
    if admin:
        return set(targets)
    permissions = set(permissions or ())
    allowed = set()
    if "entities" in permissions:
        allowed.update({"item", "person"})
    if "sales" in permissions or "purchase" in permissions:
        allowed.add("invoice")
    if "reports" in permissions:
        allowed.update({"invoice", "receive", "payment"})
    if "receive" in permissions:
        allowed.add("receive")
    if "payment" in permissions:
        allowed.add("payment")
    return set(targets).intersection(allowed)

@app.route(URL_PREFIX + "/api/search", methods=["GET"])
@login_required
def api_search():
    q_raw = (request.args.get("q") or "").strip()
    kind = (request.args.get("kind") or "").strip().lower()
    sort_key = (request.args.get("sort") or _search_sort_key()).strip().lower()
    valid_sort_keys = {k for k, _ in SEARCH_SORT_CHOICES}
    if sort_key not in valid_sort_keys:
        sort_key = _search_sort_key()
    price_mode = _price_display_mode()

    try:
        limit = int(request.args.get("limit", 10))
    except Exception:
        limit = 10
    limit = max(1, min(limit, 50))

    def try_float(val):
        try:
            return _to_float(val, None)
        except Exception:
            return None

    def fmt_number(val):
        try:
            f = float(val)
            if abs(f - int(f)) < 1e-6:
                return fa_digits(f"{int(f):,}")
            return fa_digits(f"{f:,.2f}".rstrip("0").rstrip("."))
        except Exception:
            return str(val)

    def fmt_jalali(val):
        try:
            return fa_digits(to_jdate_str(val)) if val else "—"
        except Exception:
            return "—"

    q_number = try_float(q_raw)
    term = f"%{q_raw}%" if q_raw else None

    alias_map = {
        "item": {"item"},
        "items": {"item"},
        "product": {"item"},
        "person": {"person"},
        "people": {"person"},
        "customer": {"person"},
        "vendor": {"person"},
        "invoice": {"invoice"},
        "invoices": {"invoice"},
        "sale": {"invoice"},
        "sales": {"invoice"},
        "factor": {"invoice"},
        "receive": {"receive"},
        "receipt": {"receive"},
        "payment": {"payment"},
        "payments": {"payment"},
        "cash": {"receive", "payment"},
        "cashdoc": {"receive", "payment"},
        "all": {"item", "person", "invoice", "receive", "payment"},
        "any": {"item", "person", "invoice", "receive", "payment"},
    }

    default_targets = {"item", "person", "invoice", "receive", "payment"}
    targets = alias_map.get(kind, default_targets if not kind else {kind})
    cheque_only = kind in {"cheque", "check", "chak", "cek"}
    if cheque_only:
        targets = {"receive", "payment"}
    # اگر مقدار ناشناس بود، به صورت عمومی جستجو کن
    if not targets.intersection(default_targets):
        targets = default_targets

    targets = _search_targets_for_permissions(targets, user_permissions(), admin=is_admin())
    if not targets:
        return jsonify([])

    ordered_targets = [t for t in ["item", "person", "invoice", "receive", "payment"] if t in targets]

    results = []

    def limit_left() -> int:
        return max(0, limit - len(results))

    item_rows = []
    if "item" in ordered_targets:
        remaining = limit_left()
        if remaining:
            query = db.session.query(Entity).filter(Entity.type == "item")
            if term:
                query = query.filter(
                    or_(
                        Entity.code.ilike(f"{q_raw}%"),
                        Entity.name.ilike(term),
                        Entity.serial_no.ilike(term),
                    )
                )
            if sort_key == "code":
                query = query.order_by(Entity.code.asc())
            elif sort_key == "name":
                query = query.order_by(Entity.name.asc())
            elif sort_key == "balance":
                query = query.order_by(Entity.stock_qty.desc(), Entity.name.asc())
            else:  # recent
                query = query.order_by(Entity.updated_at.desc(), Entity.id.desc())

            item_rows = query.limit(remaining).all()

            price_map = {}
            if item_rows:
                item_ids = [it.id for it in item_rows]
                if price_mode == "average":
                    avg_rows = (
                        db.session.query(InvoiceLine.item_id, func.avg(InvoiceLine.unit_price))
                        .filter(InvoiceLine.item_id.in_(item_ids))
                        .group_by(InvoiceLine.item_id)
                        .all()
                    )
                    price_map = {iid: float(avg or 0.0) for iid, avg in avg_rows}
                else:
                    ph_rows = (
                        db.session.query(PriceHistory)
                        .filter(PriceHistory.item_id.in_(item_ids))
                        .order_by(PriceHistory.item_id.asc(), PriceHistory.updated_at.desc())
                        .all()
                    )
                    for ph in ph_rows:
                        if ph.item_id not in price_map:
                            price_map[ph.item_id] = float(ph.last_price or 0.0)

            for e in item_rows:
                meta_parts = []
                if e.unit:
                    meta_parts.append(e.unit)
                if e.stock_qty is not None:
                    meta_parts.append(f"موجودی: {fmt_number(e.stock_qty)}")
                if e.serial_no:
                    meta_parts.append(e.serial_no)
                if price_map.get(e.id):
                    price_label = "میانگین" if price_mode == "average" else "آخرین"
                    meta_parts.append(f"{price_label} قیمت: {fmt_number(price_map[e.id])}")
                results.append({
                    "id": e.id,
                    "type": "item",
                    "code": e.code or "",
                    "name": e.name or "",
                    "stock": fmt_number(e.stock_qty) if e.stock_qty is not None else None,
                    "price": fmt_number(price_map.get(e.id)) if price_map.get(e.id) is not None else None,
                    "extra": e.unit or "",
                    "meta": " • ".join(meta_parts) if meta_parts else "",
                })
                if limit_left() == 0:
                    break

    if "person" in ordered_targets and limit_left():
        remaining = limit_left()
        query = db.session.query(Entity).filter(Entity.type == "person")
        if term:
            query = query.filter(
                or_(
                    Entity.code.ilike(f"{q_raw}%"),
                    Entity.name.ilike(term),
                    Entity.unit.ilike(term),
                )
            )
        if sort_key == "name":
            query = query.order_by(Entity.name.asc())
        elif sort_key == "code":
            query = query.order_by(Entity.code.asc())
        elif sort_key == "balance":
            query = query.order_by(Entity.balance.desc(), Entity.name.asc())
        else:
            query = query.order_by(Entity.updated_at.desc(), Entity.id.desc())

        rows = query.limit(remaining).all()
        for e in rows:
            meta_parts = []
            if e.unit:
                meta_parts.append(e.unit)
            meta_parts.append(f"مانده: {fmt_number(e.balance or 0)}")
            results.append({
                "id": e.id,
                "type": "person",
                "code": e.code or "",
                "name": e.name or "",
                "balance": fmt_number(e.balance or 0.0),
                "extra": e.unit or "",
                "meta": " • ".join(meta_parts),
            })
            if limit_left() == 0:
                break

    if "invoice" in ordered_targets and limit_left():
        remaining = limit_left()
        query = db.session.query(Invoice)
        conds = []
        if term:
            conds.append(Invoice.number.ilike(term))
            conds.append(Invoice.person.has(Entity.name.ilike(term)))
        if q_number is not None:
            conds.append(Invoice.total == q_number)
        if conds:
            query = query.filter(or_(*conds))
        if sort_key == "code":
            query = query.order_by(Invoice.number.asc())
        elif sort_key == "name":
            query = query.join(Entity, Invoice.person_id == Entity.id).order_by(Entity.name.asc())
        else:
            query = query.order_by(Invoice.date.desc(), Invoice.number.desc())

        rows = query.limit(remaining).all()
        for inv in rows:
            meta_parts = []
            if inv.date:
                meta_parts.append(fmt_jalali(inv.date))
            if inv.total is not None:
                meta_parts.append(f"مبلغ: {fmt_number(inv.total)}")
            results.append({
                "id": inv.id,
                "type": "invoice",
                "code": inv.number or "",
                "name": (inv.person.name if inv.person else ""),
                "amount": float(inv.total or 0.0),
                "meta": " • ".join(meta_parts),
            })
            if limit_left() == 0:
                break

    if limit_left() and {"receive", "payment"}.intersection(ordered_targets):
        remaining = limit_left()
        query = db.session.query(CashDoc)
        conds = []
        if term:
            conds.append(CashDoc.number.ilike(term))
            conds.append(CashDoc.person.has(Entity.name.ilike(term)))
            conds.append(CashDoc.cheque_number.ilike(term))
        if q_number is not None:
            conds.append(CashDoc.amount == q_number)
        if conds:
            query = query.filter(or_(*conds))

        # محدود کردن بر اساس doc_type
        if targets == {"receive"}:
            query = query.filter(CashDoc.doc_type == "receive")
        elif targets == {"payment"}:
            query = query.filter(CashDoc.doc_type == "payment")
        elif "receive" in targets and "payment" not in targets:
            query = query.filter(CashDoc.doc_type == "receive")
        elif "payment" in targets and "receive" not in targets:
            query = query.filter(CashDoc.doc_type == "payment")
        if cheque_only:
            query = query.filter(func.lower(func.coalesce(CashDoc.method, "")) == "cheque")

        if sort_key == "code":
            query = query.order_by(CashDoc.number.asc())
        elif sort_key == "name":
            query = query.join(Entity, CashDoc.person_id == Entity.id).order_by(Entity.name.asc())
        elif sort_key == "balance":
            query = query.order_by(CashDoc.amount.desc(), CashDoc.date.desc())
        else:
            query = query.order_by(CashDoc.date.desc(), CashDoc.number.desc())

        rows = query.limit(remaining).all()
        for doc in rows:
            if doc.doc_type not in targets and len(targets) != len(default_targets):
                continue
            meta_parts = []
            if doc.date:
                meta_parts.append(fmt_jalali(doc.date))
            meta_parts.append(f"مبلغ: {fmt_number(doc.amount)}")
            if doc.cheque_number:
                meta_parts.append(f"چک: {fa_digits(doc.cheque_number)}")
            if doc.cheque_due_date:
                meta_parts.append(f"سررسید: {fmt_jalali(doc.cheque_due_date)}")
            if doc.cashbox:
                meta_parts.append(f"صندوق: {doc.cashbox.name}")
            results.append({
                "id": doc.id,
                "type": doc.doc_type,
                "code": doc.number or "",
                "name": (doc.person.name if doc.person else ""),
                "amount": float(doc.amount or 0.0),
                "meta": " • ".join(meta_parts),
            })
            if limit_left() == 0:
                break

    return jsonify(results[:limit])

# ----------------- DB init & run -----------------
def _ensure_column_sqlite(table:str, col:str, coltype:str, default_val:str="0"):
    try:
        from sqlalchemy import text
        info = db.session.execute(text(f"PRAGMA table_info({table});")).fetchall()
        cols = {row[1] for row in info}
        if col not in cols:
            db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype} DEFAULT {default_val};"))
            db.session.commit()
    except Exception as ex:
        app.logger.error(f"ALTER TABLE failed for {table}.{col}: {ex}")

def _migrate_ai_credentials_to_encrypted():
    """Encrypt legacy plaintext AI keys without breaking existing installs."""
    try:
        changed = False
        global_key = (Setting.get("openai_api_key", "") or "").strip()
        if global_key and not is_encrypted(global_key):
            Setting.set("openai_api_key", encrypt_secret(current_app, global_key))
            changed = True
        for item in UserSettings.query.all():
            raw = (item.openai_api_key or "").strip()
            if raw and not is_encrypted(raw):
                item.openai_api_key = encrypt_secret(current_app, raw)
                changed = True
        if changed:
            db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("AI credential encryption migration failed")


with app.app_context():
    db.create_all()
    _ensure_column_sqlite("entities", "stock_qty", "REAL", "0")
    _ensure_column_sqlite("entities", "balance",   "REAL", "0")
    _ensure_column_sqlite("cash_docs", "cashbox_id", "INTEGER", "NULL")
    _ensure_column_sqlite("cash_docs", "cheque_number", "TEXT", "NULL")
    _ensure_column_sqlite("cash_docs", "cheque_bank", "TEXT", "NULL")
    _ensure_column_sqlite("cash_docs", "cheque_branch", "TEXT", "NULL")
    _ensure_column_sqlite("cash_docs", "cheque_account", "TEXT", "NULL")
    _ensure_column_sqlite("cash_docs", "cheque_owner", "TEXT", "NULL")
    _ensure_column_sqlite("cash_docs", "cheque_due_date", "TEXT", "NULL")
    _ensure_column_sqlite("invoices", "kind", "TEXT", "'sales'")
    _migrate_ai_credentials_to_encrypted()
    # ensure invoices.kind column exists; do NOT force a 'sales' default that would
    # incorrectly mark existing purchase invoices as sales. Use NULL as default so
    # we can run a reliable backfill below.
    _ensure_column_sqlite("invoices", "kind", "TEXT", "NULL")

    # Backfill invoice.kind for all existing invoices using the number prefix
    # heuristic. Run unconditionally to correct any rows that may have been
    # populated with an incorrect default previously.
    try:
        rows = Invoice.query.all()
        changed = 0
        for inv in rows:
            desired = "sales" if (inv.number or "").upper().startswith("INV-") else "purchase"
            if (inv.kind or "") != desired:
                inv.kind = desired
                changed += 1
        if changed:
            db.session.commit()
            app.logger.info(f"Backfilled invoice.kind for {changed} invoices")
    except Exception as ex:
        app.logger.error(f"backfill invoice.kind failed: {ex}")

if __name__ == "__main__":
    if URL_PREFIX:
        print("Running with URL prefix:", URL_PREFIX)
    print(f"Activity log: {LOG_FILE}")
    debug_enabled = os.environ.get("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    app.run(host="0.0.0.0", port=PORT, debug=debug_enabled)

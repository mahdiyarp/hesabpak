# -*- coding: utf-8 -*-
import os, json, logging, secrets, base64
from pathlib import Path
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional

from flask import Flask, render_template, redirect, request, flash, session, jsonify, abort, current_app
from flask_login import LoginManager, login_user, logout_user, login_required, UserMixin, current_user
from dotenv import load_dotenv
from markupsafe import Markup
from sqlalchemy import func, or_, UniqueConstraint   # <- مهم

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

from extensions import db
from utils.backup_utils import ensure_dirs, autosave_record
from blueprints.backup import backup_bp
from autobackup import init_autobackup, register_autobackup_for
from models.backup_models import Setting, BackupLog
from utils.num_words_fa import amount_to_toman_words
from utils.date_utils import (
    to_jdate_str,
    now_info as date_now_info,
    parse_gregorian_date,
    parse_jalali_date,
    jalali_reference,
    fa_digits,
)

# ----------------- Config -----------------
load_dotenv()
PROJECT_ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8000"))
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-please")
URL_PREFIX = os.environ.get("URL_PREFIX", "") or ""   # مثلا: /hesabpak
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
DATA_DIR = os.environ.get("DATA_DIR", "data")

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
    ("gpt-4o-mini", "GPT-4o mini"),
    ("gpt-4.1-mini", "GPT-4.1 mini"),
    ("o4-mini", "o4 mini (چندحالته)"),
]

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
app.config["SECRET_KEY"] = SECRET_KEY

DB_DIR = Path(DATA_DIR).resolve(); DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "hesabpak.sqlite3"
DB_URI = "sqlite:///" + str(DB_PATH).replace("\\", "/")
app.config["DATA_DIR"] = str(DB_DIR)
app.config["DB_FILE"] = DB_PATH.name
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)
print(f"[DB] Using: {DB_PATH}")

USERS_FILE = str((DB_DIR / "users.json").resolve())
LOG_FILE   = str((DB_DIR / "activity.log").resolve())

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
                        "password": ADMIN_PASSWORD,
                        "role": "admin",
                        "permissions": ADMIN_PERMISSIONS,
                        "is_active": True,
                    }
                ]
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


def _normalize_user_entry(username: str, data: dict) -> dict:
    password = (data.get("password") or "").strip()
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
                "password": ADMIN_PASSWORD,
                "role": "admin",
                "permissions": ADMIN_PERMISSIONS,
                "is_active": True,
            },
        )
    return catalog


def save_users_catalog(catalog: dict) -> None:
    payload = {
        "users": [
            {
                "username": username,
                "password": data.get("password", ""),
                "role": data.get("role", "staff"),
                "permissions": _permissions_for_role(data.get("role", "staff"), data.get("permissions", [])),
                "is_active": bool(data.get("is_active", True)),
            }
            for username, data in sorted(catalog.items(), key=lambda kv: kv[0].lower())
        ]
    }
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

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
    ):
        self.id = username
        self.username = username
        self.role = role or "staff"
        self.permissions = set(permissions or [])
        self._active = bool(is_active)

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
    )


def is_admin() -> bool:
    return current_user.is_authenticated and getattr(current_user, "role", "") == "admin"


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
        if x is None: return default
        x = str(x).strip().replace(',', '')
        if x == '': return default
        return float(x)
    except:
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
    valid = {k for k, _ in ASSISTANT_MODEL_CHOICES}
    if key not in valid:
        key = ASSISTANT_MODEL_CHOICES[0][0]
    return key

def _openai_api_key() -> str:
    key = (Setting.get("openai_api_key", "") or "").strip()
    if not key:
        key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    return key

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
            "reply": {"type": "string", "description": "متن پاسخ به کاربر به زبان فارسی"},
            "needs_confirmation": {"type": "boolean"},
            "follow_up": {"type": ["string", "null"]},
            "uncertain_fields": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "invoice": {
                "type": ["null", "object"],
                "properties": {
                    "kind": {"type": "string", "enum": ["sales", "purchase", "unknown"], "default": "sales"},
                    "number": {"type": ["string", "null"]},
                    "date": {"type": ["string", "null"]},
                    "partner": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "code": {"type": ["string", "null"]},
                            "phone": {"type": ["string", "null"]},
                            "role": {"type": ["string", "null"], "description": "buyer | seller"},
                        },
                        "required": ["name"],
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
                            "required": ["name", "qty"],
                        },
                    },
                    "notes": {"type": ["string", "null"]},
                },
                "required": ["kind", "partner", "items"],
            },
            "actions": {
                "type": "array",
                "default": [],
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
                    "required": ["operation", "path"],
                },
            },
        },
        "required": ["reply", "needs_confirmation"],
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
        if text:
            content.append({"type": "text", "text": text})
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
            content.append({"type": "input_image", "image_base64": data, "mime_type": mime})
        if not content and not text:
            continue
        prepared.append({"role": role, "content": content or [{"type": "text", "text": text or ""}]})
    return prepared


def _resolve_project_path(rel_path: str) -> Path:
    rel_path = (rel_path or "").strip()
    if not rel_path:
        raise ValueError("مسیر فایل مشخص نشده است.")
    rel = Path(rel_path)
    if rel.is_absolute():
        raise ValueError("مسیر باید نسبی باشد.")
    target = (PROJECT_ROOT / rel).resolve()
    if PROJECT_ROOT not in target.parents and target != PROJECT_ROOT:
        raise ValueError("امکان دسترسی به مسیر خارج از پروژه وجود ندارد.")
    return target


def _apply_assistant_actions(actions: List[Dict[str, Any]]) -> Dict[str, Any]:
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
    api_key = _openai_api_key()
    if not api_key:
        raise RuntimeError("کلید API تنظیم نشده است.")
    if OpenAI is None:
        raise RuntimeError("کتابخانه openai نصب نشده است.")

    client = OpenAI(api_key=api_key)
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

    request_messages = [
        {"role": "system", "content": [{"type": "text", "text": system_prompt}]}
    ] + _build_openai_messages(messages)

    response = client.responses.create(
        model=_assistant_model(),
        input=request_messages,
        max_output_tokens=800,
        response_format={"type": "json_schema", "json_schema": AI_RESPONSE_SCHEMA},
    )

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
                if isinstance(part, dict):
                    maybe_text = part.get("text")
                else:
                    maybe_text = getattr(part, "text", None)
                if maybe_text:
                    content = maybe_text
                    break
    except Exception:
        logging.exception("failed to parse response.output for assistant reply")

    if not content:
        content = getattr(response, "output_text", None)

    if not content:
        raise RuntimeError("ساختار پاسخ نامعتبر است.")

    try:
        return json.loads(content)
    except Exception as exc:
        raise RuntimeError(f"امکان خواندن پاسخ وجود ندارد: {exc}")

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

def _prepare_invoice_plan(invoice_data: Dict[str, Any]) -> Dict[str, Any]:
    kind = (invoice_data.get("kind") or "sales").strip().lower()
    if kind not in ("sales", "purchase"):
        kind = "sales"

    partner_payload = invoice_data.get("partner") or {}
    partner_info = _resolve_entity("person", partner_payload)

    parsed_date = _parse_invoice_date(invoice_data.get("date"))
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
    kind = plan.get("kind", "sales")
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

    inv_date = _parse_invoice_date(plan.get("date")) or datetime.utcnow().date()

    allow_negative = _allow_negative_sales()
    items_payload = []
    created_items = []
    for row in plan.get("items") or []:
        qty = _to_float(row.get("qty"), 0.0)
        if qty <= 0:
            continue
        unit_price = _to_float(row.get("unit_price"), 0.0)
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

    inv = Invoice(number=number, date=inv_date, person_id=partner_entity.id, discount=0.0, tax=0.0, total=0.0)
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
            try:
                item.stock_qty = float(item.stock_qty or 0.0) - qty
            except Exception:
                item.stock_qty = 0.0 - qty
            ph = PriceHistory.query.filter_by(person_id=partner_entity.id, item_id=item.id).first()
            if not ph:
                db.session.add(PriceHistory(person_id=partner_entity.id, item_id=item.id, last_price=unit_price))
            else:
                ph.last_price = unit_price
        else:
            try:
                item.stock_qty = float(item.stock_qty or 0.0) + qty
            except Exception:
                item.stock_qty = 0.0 + qty

    inv.total = total

    try:
        if kind == "sales":
            partner_entity.balance = float(partner_entity.balance or 0.0) + float(total)
        else:
            partner_entity.balance = float(partner_entity.balance or 0.0) - float(total)
    except Exception:
        partner_entity.balance = float(total) if kind == "sales" else -float(total)

    db.session.commit()

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

@app.before_request
def _req_log():
    if current_user.is_authenticated:
        app.logger.info(f"USER={current_user.username}  IP={request.remote_addr}  {request.method} {request.path}  ARGS={dict(request.args)}")

def _level_by_code(code: str) -> int:
    L = len(code or "")
    if L == 3: return 1
    if L == 6: return 2
    if L == 9: return 3
    return 0

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
    today_invoice_total = float(inv_stats[1] or 0.0)

    def _sum_cash(doc_type, dt=today):
        return float(
            db.session.query(func.coalesce(func.sum(CashDoc.amount), 0.0))
            .filter(CashDoc.doc_type == doc_type, CashDoc.date == dt)
            .scalar()
            or 0.0
        )

    today_receives_total = _sum_cash("receive")
    today_payments_total = _sum_cash("payment")

    method_balances = []
    for method, label in CASH_METHOD_LABELS.items():
        receive_sum = float(
            db.session.query(func.coalesce(func.sum(CashDoc.amount), 0.0))
            .filter(
                CashDoc.doc_type == "receive",
                func.lower(func.coalesce(CashDoc.method, "")) == method,
            )
            .scalar()
            or 0.0
        )
        payment_sum = float(
            db.session.query(func.coalesce(func.sum(CashDoc.amount), 0.0))
            .filter(
                CashDoc.doc_type == "payment",
                func.lower(func.coalesce(CashDoc.method, "")) == method,
            )
            .scalar()
            or 0.0
        )
        method_balances.append(
            {
                "method": method,
                "label": label,
                "receive": receive_sum,
                "payment": payment_sum,
                "balance": receive_sum - payment_sum,
            }
        )

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

    sales_rows = (
        db.session.query(
            Invoice.date,
            func.count(Invoice.id),
            func.coalesce(func.sum(Invoice.total), 0.0),
        )
        .filter(Invoice.date >= chart_days[0], Invoice.date <= today)
        .group_by(Invoice.date)
        .order_by(Invoice.date)
        .all()
    )
    sales_total_map = {row[0]: float(row[2] or 0.0) for row in sales_rows}
    sales_count_map = {row[0]: int(row[1] or 0) for row in sales_rows}

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
            "invoiceCounts": chart_invoice_counts,
            "receivesTotals": chart_receives_totals,
            "paymentsTotals": chart_payments_totals,
        },
        dashboard_widgets=_dashboard_widgets(),
        assistant_model_label=dict(ASSISTANT_MODEL_CHOICES).get(_assistant_model(), _assistant_model()),
    )

@app.route(URL_PREFIX + "/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(URL_PREFIX + "/")
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        catalog = load_users_catalog()
        entry = catalog.get(username)
        if entry and entry.get("password") == password:
            if not entry.get("is_active", True):
                flash("دسترسی این کاربر غیرفعال شده است.", "danger")
                return redirect(URL_PREFIX + "/login")
            login_user(
                User(
                    username,
                    role=entry.get("role", "staff"),
                    permissions=entry.get("permissions", []),
                    is_active=entry.get("is_active", True),
                )
            )
            session["login_at_utc"] = datetime.utcnow().isoformat()
            flash("ورود موفق", "success")
            app.logger.info(f"LOGIN  USER={username}  IP={request.remote_addr}")
            return redirect(URL_PREFIX + "/")
        flash("نام کاربری یا رمز عبور اشتباه است.", "danger")
        app.logger.warning(f"LOGIN_FAIL  USER={username}  IP={request.remote_addr}")
    return render_template("login.html", prefix=URL_PREFIX)

@app.route(URL_PREFIX + "/logout")
def logout():
    if current_user.is_authenticated:
        app.logger.info(f"LOGOUT USER={current_user.username} IP={request.remote_addr}")
    logout_user()
    session.pop("login_at_utc", None)
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
                    db.session.delete(ent); db.session.commit()
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

# ----------------- Sales -----------------
@app.route(URL_PREFIX + "/sales", methods=["GET", "POST"])
@login_required
def sales():
    ensure_permission("sales")
    now_info = _now_info()
    inv_number_generated = jalali_reference("INV", now_info["datetime"])
    allow_negative = _allow_negative_sales()

    if request.method == "POST":
        number = (request.form.get("inv_number") or "").strip() or inv_number_generated
        inv_date = parse_gregorian_date(request.form.get("inv_date_greg"))

        person = None
        pid = (request.form.get("person_token") or "").strip()
        if pid.isdigit():
            person = Entity.query.get(int(pid))
        if not person:
            pcode = (request.form.get("person_code") or "").strip()
            if pcode:
                person = Entity.query.filter_by(type="person", code=pcode).first()
        if not person or person.type != "person":
            flash("لطفاً مشتری معتبر انتخاب کنید.", "danger")
            return redirect(URL_PREFIX + "/sales")

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
                if not allow_negative:
                    base_stock = float(pending_stock.get(item.id, item.stock_qty or 0.0))
                    if base_stock - q < -1e-6:
                        flash(f"موجودی کالا «{item.name}» برای فروش کافی نیست.", "danger")
                        return redirect(URL_PREFIX + "/sales")
                    pending_stock[item.id] = base_stock - q
                else:
                    current = float(pending_stock.get(item.id, item.stock_qty or 0.0))
                    pending_stock[item.id] = current - q
                rows.append({"item": item, "unit_price": up, "qty": q})
            if len(rows) >= MAX_ROWS:
                break

        if not rows:
            flash("لطفاً حداقل یک ردیف کالای معتبر با تعداد وارد کنید.", "danger")
            return redirect(URL_PREFIX + "/sales")

        subtotal = sum(r["unit_price"] * r["qty"] for r in rows)
        discount = 0.0
        tax      = 0.0
        total    = subtotal - discount + tax

        if Invoice.query.filter_by(number=number).first():
            number = generate_invoice_number()

        inv = Invoice(number=number, date=inv_date, person_id=person.id,
                      discount=discount, tax=tax, total=total)
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

            try:
                item.stock_qty = float(item.stock_qty or 0.0) - qty
            except Exception:
                item.stock_qty = 0.0 - qty

            ph = PriceHistory.query.filter_by(person_id=person.id, item_id=item.id).first()
            if not ph:
                ph = PriceHistory(person_id=person.id, item_id=item.id, last_price=up)
                db.session.add(ph)
            else:
                ph.last_price = up

        try:
            person.balance = float(person.balance or 0.0) + float(total)
        except Exception:
            person.balance = float(total)

        db.session.commit()

        flash(
            f"✅ فاکتور «{inv.number}» ثبت شد برای «{person.name}» — {amount_to_toman_words(total)}",
            "success",
        )
        return redirect(URL_PREFIX + f"/receive?invoice_id={inv.id}")

    return render_template(
        "sales.html",
        prefix=URL_PREFIX,
        inv_number=inv_number_generated,
        current_jdate=now_info["jalali_date"],
        current_gdate=now_info["greg_date"],
    )

# ----------------- Purchase -----------------
@app.route(URL_PREFIX + "/purchase", methods=["GET","POST"])
@login_required
def purchase_stub():
    ensure_permission("purchase")
    now_info = _now_info()
    def _next_purchase_number():
        nums = []
        for (num,) in db.session.query(Invoice.number).all():
            s = (num or "").strip()
            if s.isdigit():
                try:
                    nums.append(int(s))
                except:
                    pass
        return str(max(nums) + 1) if nums else "1"

    inv_number_generated = _next_purchase_number()

    if request.method == "POST":
        number = (request.form.get("inv_number") or "").strip() or inv_number_generated
        inv_date = parse_gregorian_date(request.form.get("inv_date_greg"))

        pid = (request.form.get("person_token") or "").strip()
        person = None
        if pid.isdigit():
            person = Entity.query.get(int(pid))
        if not person:
            pcode = (request.form.get("person_code") or "").strip()
            if pcode:
                person = Entity.query.filter_by(type="person", code=pcode).first()
        if not person or person.type != "person":
            flash("لطفاً تأمین‌کننده معتبر انتخاب کنید.", "danger")
            return redirect(URL_PREFIX + "/purchase")

        item_ids    = request.form.getlist("item_id[]")
        item_codes  = request.form.getlist("item_code[]")
        unit_prices = request.form.getlist("unit_price[]")
        qtys        = request.form.getlist("qty[]")

        def _to_float(s, dv=0.0):
            try:
                return float(str(s).replace(",", ""))
            except:
                return dv

        rows = []
        MAX_ROWS = 15
        for i in range(min(len(item_ids), len(item_codes), len(unit_prices), len(qtys))):
            iid  = (item_ids[i] or "").strip()
            icode= (item_codes[i] or "").strip()
            up   = _to_float(unit_prices[i], 0.0)
            q    = _to_float(qtys[i], 0.0)

            itm = None
            if iid.isdigit():
                itm = Entity.query.get(int(iid))
            if (not itm) and icode:
                itm = Entity.query.filter_by(type="item", code=icode).first()

            if itm is not None and itm.type == "item" and q > 0:
                rows.append({"item": itm, "unit_price": max(0.0, up), "qty": q})
            if len(rows) >= MAX_ROWS:
                break

        if not rows:
            flash("حداقل یک ردیف کالا لازم است.", "warning")
            return redirect(URL_PREFIX + "/purchase")

        inv = Invoice(number=number, date=inv_date, person_id=person.id, discount=0.0, tax=0.0, total=0.0)
        db.session.add(inv); db.session.flush()

        total = 0.0
        for r in rows:
            item = r["item"]
            up   = float(r["unit_price"])
            qty  = float(r["qty"])
            line_total = up * qty
            total += line_total

            db.session.add(InvoiceLine(invoice_id=inv.id, item_id=item.id, unit_price=up, qty=qty))

            try:
                item.stock_qty = float(item.stock_qty or 0.0) + qty
            except Exception:
                item.stock_qty = 0.0 + qty

        inv.total = total

        try:
            person.balance = float(person.balance or 0.0) - float(total)
        except Exception:
            person.balance = -float(total)

        db.session.commit()
        flash(
            f"✅ فاکتور خرید «{inv.number}» ثبت شد — {amount_to_toman_words(total)}",
            "success",
        )
        return redirect(URL_PREFIX + f"/payment?invoice_id={inv.id}")

    return render_template(
        "purchase.html",
        prefix=URL_PREFIX,
        inv_number=inv_number_generated,
        current_jdate=now_info["jalali_date"],
        current_gdate=now_info["greg_date"],
    )

# ----------------- Entities CRUD -----------------
@app.route(URL_PREFIX + "/entities")
@login_required
def entities_list():
    ensure_permission("entities")
    kind = (request.args.get("kind") or "item").strip()
    if kind not in ("person","item"):
        kind = "item"
    q = (request.args.get("q") or "").strip()
    base = Entity.query.filter_by(type=kind)

    if q:
        starts = base.filter(or_(Entity.code.ilike(f"{q}%"), Entity.name.ilike(f"{q}%")))
        contains = base.filter(or_(Entity.code.ilike(f"%{q}%"), Entity.name.ilike(f"%{q}%")))
        rows = list(starts.order_by(Entity.level.asc(), Entity.code.asc()).limit(500).all())
        seen = {r.id for r in rows}
        for e in contains.order_by(Entity.level.asc(), Entity.code.asc()).limit(500).all():
            if e.id not in seen:
                rows.append(e); seen.add(e.id)
    else:
        rows = base.order_by(Entity.level.asc(), Entity.code.asc()).limit(1000).all()
    return render_template("entities/list.html", rows=rows, q=q, kind=kind, prefix=URL_PREFIX)

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
        flash("ثبت شد.", "success")
        return redirect(URL_PREFIX + f"/entities?kind={ent.type}")

    parents_lvl1 = Entity.query.filter_by(level=1).order_by(Entity.code.asc()).all()
    parents_lvl2 = Entity.query.filter_by(level=2).order_by(Entity.code.asc()).all()
    return render_template("entities/new.html", parents_lvl1=parents_lvl1, parents_lvl2=parents_lvl2, prefix=URL_PREFIX)

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
        flash("ویرایش شد.", "success")
        return redirect(URL_PREFIX + f"/entities?kind={ent.type}")

    parents_lvl1 = Entity.query.filter_by(level=1).order_by(Entity.code.asc()).all()
    parents_lvl2 = Entity.query.filter_by(level=2).order_by(Entity.code.asc()).all()
    return render_template("entities/edit.html", ent=ent, parents_lvl1=parents_lvl1, parents_lvl2=parents_lvl2, prefix=URL_PREFIX)

@app.route(URL_PREFIX + "/entities/<int:eid>/delete", methods=["POST"])
@login_required
def entities_delete(eid):
    admin_required()
    ent = Entity.query.get_or_404(eid)
    t = ent.type
    db.session.delete(ent); db.session.commit()
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

    def parse_d(s):
        return parse_gregorian_date(s, allow_none=True)
    df = parse_d(dfrom); dt = parse_d(dto)

    rows = []

    if typ in ("all", "invoice"):
        inv_q = db.session.query(Invoice).join(Entity, Invoice.person_id == Entity.id)
        if q:
            inv_q = inv_q.filter(or_(
                Invoice.number.ilike(f"%{q}%"),
                Entity.name.ilike(f"%{q}%"),
                Entity.code.ilike(f"%{q}%"),
            ))
        if df: inv_q = inv_q.filter(Invoice.date >= df)
        if dt: inv_q = inv_q.filter(Invoice.date <= dt)

        for inv in inv_q.order_by(Invoice.id.desc()).limit(500).all():
            rows.append({
                "kind": "invoice",
                "id": inv.id,
                "number": inv.number,
                "date": to_jdate_str(inv.date),
                "person": inv.person.name,
                "amount": inv.total,
            })

    if typ in ("all", "receive", "payment"):
        cd_q = db.session.query(CashDoc).join(Entity, CashDoc.person_id == Entity.id)
        if typ in ("receive", "payment"):
            cd_q = cd_q.filter(CashDoc.doc_type == typ)
        if q:
            cd_q = cd_q.filter(or_(
                CashDoc.number.ilike(f"%{q}%"),
                Entity.name.ilike(f"%{q}%"),
                Entity.code.ilike(f"%{q}%"),
                CashDoc.cheque_number.ilike(f"%{q}%"),
            ))
        if df: cd_q = cd_q.filter(CashDoc.date >= df)
        if dt: cd_q = cd_q.filter(CashDoc.date <= dt)

        for d in cd_q.order_by(CashDoc.id.desc()).limit(500).all():
            rows.append({
                "kind": d.doc_type,
                "id": d.id,
                "number": d.number,
                "date": to_jdate_str(d.date),
                "person": d.person.name,
                "amount": d.amount,
                "cheque_number": d.cheque_number,
                "method": d.method,
                "cashbox": d.cashbox.name if d.cashbox else None,
                "cheque_due": to_jdate_str(d.cheque_due_date) if d.cheque_due_date else None,
            })

    rows.sort(key=lambda r: (r["date"], str(r["number"])), reverse=True)

    try:
        return render_template(
            "reports.html",
            rows=rows, q=q, typ=typ, dfrom=dfrom, dto=dto, prefix=URL_PREFIX
        )
    except Exception:
        html = [
            "<h2>گزارشات</h2>",
            "<form method='get' action='' style='display:flex;gap:8px;flex-wrap:wrap;margin:10px 0'>",
            f"<input class='inp' name='q' value='{q}' placeholder='شماره/نام/کد' style='max-width:240px'>",
            "<select class='inp' name='type' style='max-width:160px'>",
            f"<option value='all' {'selected' if typ=='all' else ''}>همه</option>",
            f"<option value='invoice' {'selected' if typ=='invoice' else ''}>فاکتور فروش</option>",
            f"<option value='receive' {'selected' if typ=='receive' else ''}>دریافت</option>",
            f"<option value='payment' {'selected' if typ=='payment' else ''}>پرداخت</option>",
            "</select>",
            f"<input class='inp' type='date' name='from' value='{dfrom or ''}' style='max-width:160px'>",
            f"<input class='inp' type='date' name='to'   value='{dto or ''}'   style='max-width:160px'>",
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
            label = {"invoice": "فاکتور فروش","receive": "دریافت","payment": "پرداخت"}.get(r["kind"], r["kind"])
            view = f"{URL_PREFIX}/invoice/{r['id']}" if r["kind"] == "invoice" else f"{URL_PREFIX}/cash/{r['id']}"
            edit = f"{view}/edit"
            ops  = [f'<a href="{view}">مشاهده</a>']
            if is_admin():
                ops.append(f'<a href="{edit}" style="margin-right:8px">ویرایش</a>')
            ops_str = " | ".join(ops)
            jdate = to_jdate_str(r["date"])
            row_html = (
                "<tr>"
                f"<td style='padding:8px;border-bottom:1px solid #f3f3f3'>{label}</td>"
                f"<td style='padding:8px;border-bottom:1px solid #f3f3f3'><code>{r['number']}</code></td>"
                f"<td style='padding:8px;border-bottom:1px solid #f3f3f3'>{jdate}</td>"
                f"<td style='padding:8px;border-bottom:1px solid #f3f3f3'>{r['person']}</td>"
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
    html = [
        f"<b>شماره:</b> {inv.number}",
        f"<br><b>تاریخ (شمسی):</b> { to_jdate_str(inv.date) }",
        f"<br><b>مشتری:</b> {inv.person.name}",
        f"<br><b>جمع:</b> {int(inv.total):,}",
        "<hr><b>آیتم‌ها:</b>",
        "<ul>" + "".join([f"<li>{ln.item.name} | {ln.qty} × {ln.unit_price} = {ln.line_total}</li>" for ln in lines]) + "</ul>"
    ]
    return render_template("page.html", title="فاکتور فروش", content=Markup("".join(html)), prefix=URL_PREFIX)

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
            cheque_parts.append(f"شماره صیادی: <code>{doc.cheque_number}</code>")
        if doc.cheque_bank:
            cheque_parts.append(f"بانک: {doc.cheque_bank}")
        if doc.cheque_branch:
            cheque_parts.append(f"شعبه: {doc.cheque_branch}")
        if doc.cheque_due_date:
            cheque_parts.append(f"سررسید: {to_jdate_str(doc.cheque_due_date)}")
        if doc.cheque_account:
            cheque_parts.append(f"شماره حساب: {doc.cheque_account}")
        if doc.cheque_owner:
            cheque_parts.append(f"صاحب حساب: {doc.cheque_owner}")
        if cheque_parts:
            cheque_meta = "<br><b>جزئیات چک:</b> " + "<br>".join(cheque_parts)
    cashbox_line = ""
    if doc.cashbox:
        box_label = doc.cashbox.name
        if doc.cashbox.kind == "bank" and doc.cashbox.bank_name:
            box_label += f" ({doc.cashbox.bank_name})"
        cashbox_line = f"<br><b>صندوق/حساب:</b> {box_label}"
    html = (
        f"<b>نوع:</b> {kind}<br><b>شماره:</b> {doc.number}"
        f"<br><b>تاریخ (شمسی):</b> {to_jdate_str(doc.date)}"
        f"<br><b>طرف حساب:</b> {doc.person.name}"
        f"<br><b>مبلغ:</b> {int(doc.amount):,}"
        f"<br><b>روش:</b> {CASH_METHOD_LABELS.get(doc.method or '', doc.method or '—')}"
        + cashbox_line
        + cheque_meta
    )
    return render_template("page.html", title="سند نقدی", content=Markup(html), prefix=URL_PREFIX)

# ===================== دریافت وجه =====================
@app.route(URL_PREFIX + "/receive", methods=["GET", "POST"])
@login_required
def receive():
    ensure_permission("receive")
    now_info = _now_info()
    rec_number = jalali_reference("RCV", now_info["datetime"])
    pos_device_key, pos_device_label = _pos_device_config()
    prefill_amount = None
    prefill_note = None
    prefill_person = None
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
                prefill_note = f"دریافت بابت فاکتور فروش {inv.number}"
        if prefill_amount is None:
            try:
                prefill_amount = float((request.args.get("amount") or "").replace(",", ""))
            except Exception:
                prefill_amount = None
        pid = (request.args.get("person_id") or "").strip()
        if not prefill_person and pid.isdigit():
            prefill_person = Entity.query.get(int(pid))

    if request.method == "POST":
        number = (request.form.get("rec_number") or "").strip() or rec_number
        rec_date = parse_gregorian_date(request.form.get("rec_date_greg"))

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
            return redirect(URL_PREFIX + "/receive")

        amount = _to_float(request.form.get("amount"), 0.0)
        if amount <= 0:
            flash("مبلغ نامعتبر است.", "danger")
            return redirect(URL_PREFIX + "/receive")
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
        if cheque_due_date is None:
            cheque_due_date = parse_jalali_date(
                request.form.get("cheque_due_date_fa"), allow_none=True
            )

        if method in ("cash", "bank"):
            required_kind = "cash" if method == "cash" else "bank"
            if not cashbox or cashbox.kind != required_kind:
                flash("لطفاً صندوق/حساب متناسب با روش دریافت را انتخاب کنید.", "danger")
                return redirect(URL_PREFIX + "/receive")
        if method == "cheque":
            if not cashbox or cashbox.kind != "bank":
                flash("برای ثبت چک، یک حساب بانکی فعال انتخاب کنید.", "danger")
                return redirect(URL_PREFIX + "/receive")
            if len(cheque_number) != 16:
                flash("شماره صیادی چک باید ۱۶ رقم باشد.", "danger")
                return redirect(URL_PREFIX + "/receive")
        else:
            cheque_number = None
            cheque_bank = None
            cheque_branch = None
            cheque_account = None
            cheque_owner = None
            cheque_due_date = None

        doc = CashDoc(
            doc_type="receive",
            number=number,
            date=rec_date,
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

        try:
            person.balance = float(person.balance or 0.0) - float(amount)
        except Exception:
            pass

        db.session.commit()
        flash(
            f"✅ دریافت «{number}» برای «{person.name}» — {amount_to_toman_words(amount)}",
            "success",
        )
        return redirect(URL_PREFIX + f"/cash/{doc.id}")

    return render_template(
        "receive.html",
        rec_number=rec_number,
        prefix=URL_PREFIX,
        current_jdate=now_info["jalali_date"],
        current_gdate=now_info["greg_date"],
        pos_device_key=pos_device_key,
        pos_device_label=pos_device_label,
        prefill_amount=prefill_amount,
        prefill_person=prefill_person,
        prefill_note=prefill_note,
        cashboxes=cashboxes,
    )

# ===================== ویرایش سند نقدی =====================
@app.route(URL_PREFIX + "/cash/<int:doc_id>/edit", methods=["GET","POST"])
@login_required
def cash_edit(doc_id):
    admin_required()
    doc = CashDoc.query.get_or_404(doc_id)
    if request.method == "POST":
        try:
            doc.amount = _to_float(request.form.get("amount"), doc.amount)
            doc.note = (request.form.get("note") or "").strip() or None
            m = (request.form.get("method") or "").strip().lower()
            if m in ("pos","cash","bank","cheque"): doc.method = m
            db.session.commit()
            flash("ویرایش شد.", "success")
        except Exception as ex:
            flash(f"خطا: {ex}", "danger")
        return redirect(URL_PREFIX + f"/cash/{doc.id}")
    edit_html = f"""
    <form method="post">
      <div class="card" style="padding:10px">
        <label class="lbl">مبلغ</label>
        <input class="inp" name="amount" value="{int(doc.amount)}">
        <label class="lbl" style="margin-top:8px">روش</label>
        <select class="inp" name="method">
          <option value="pos" {'selected' if (doc.method or '').lower()=='pos' else ''}>دستگاه پوز</option>
          <option value="cash" {'selected' if (doc.method or '').lower()=='cash' else ''}>نقدی</option>
          <option value="bank" {'selected' if (doc.method or '').lower()=='bank' else ''}>بانک</option>
          <option value="cheque" {'selected' if (doc.method or '').lower()=='cheque' else ''}>چک</option>
        </select>
        <label class="lbl" style="margin-top:8px">یادداشت</label>
        <textarea class="inp" name="note">{doc.note or ''}</textarea>
        <div style="margin-top:10px"><button class="btn">ذخیره</button></div>
      </div>
    </form>
    """
    return render_template("page.html", title="ویرایش سند دریافت/پرداخت", content=Markup(edit_html), prefix=URL_PREFIX)

# ===================== پرداخت =====================
@app.route(URL_PREFIX + "/payment", methods=["GET", "POST"])
@login_required
def payment():
    ensure_permission("payment")
    now_info = _now_info()
    pay_number = jalali_reference("PAY", now_info["datetime"])
    pos_device_key, pos_device_label = _pos_device_config()
    prefill_amount = None
    prefill_note = None
    prefill_person = None
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
                prefill_note = f"پرداخت بابت فاکتور خرید {inv.number}"
        if prefill_amount is None:
            try:
                prefill_amount = float((request.args.get("amount") or "").replace(",", ""))
            except Exception:
                prefill_amount = None
        pid = (request.args.get("person_id") or "").strip()
        if not prefill_person and pid.isdigit():
            prefill_person = Entity.query.get(int(pid))

    if request.method == "POST":
        number = (request.form.get("pay_number") or "").strip() or pay_number
        pay_date = parse_gregorian_date(request.form.get("pay_date_greg"))

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
            return redirect(URL_PREFIX + "/payment")

        def _to_float(x, dv=0.0):
            try:
                return float(str(x).replace(",", "").strip() or dv)
            except Exception:
                return dv
        amount = _to_float(request.form.get("amount"), 0.0)
        if amount <= 0:
            flash("مبلغ پرداخت باید بزرگ‌تر از صفر باشد.", "danger")
            return redirect(URL_PREFIX + "/payment")

        method = (request.form.get("method") or "").strip().lower() or None
        note   = (request.form.get("note") or "").strip() or None

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

        if method in ("cash", "bank"):
            required_kind = "cash" if method == "cash" else "bank"
            if not cashbox or cashbox.kind != required_kind:
                flash("لطفاً حساب متناسب با روش پرداخت را انتخاب کنید.", "danger")
                return redirect(URL_PREFIX + "/payment")
        if method == "cheque":
            if not cashbox or cashbox.kind != "bank":
                flash("برای صدور چک، حساب بانکی معتبر انتخاب کنید.", "danger")
                return redirect(URL_PREFIX + "/payment")
            if len(cheque_number) != 16:
                flash("شماره صیادی چک باید ۱۶ رقم باشد.", "danger")
                return redirect(URL_PREFIX + "/payment")
        else:
            cheque_number = None
            cheque_bank = None
            cheque_branch = None
            cheque_account = None
            cheque_owner = None
            cheque_due_date = None

        doc = CashDoc(
            doc_type="payment",
            number=number,
            date=pay_date,
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

        try:
            person.balance = float(person.balance or 0.0) + float(amount)
        except Exception:
            pass

        db.session.commit()
        flash(
            f"✅ پرداخت «{number}» برای «{person.name}» — {amount_to_toman_words(amount)}",
            "success",
        )
        return redirect(URL_PREFIX + f"/cash/{doc.id}")

    return render_template(
        "payment.html",
        pay_number=pay_number,
        prefix=URL_PREFIX,
        current_jdate=now_info["jalali_date"],
        current_gdate=now_info["greg_date"],
        pos_device_key=pos_device_key,
        pos_device_label=pos_device_label,
        prefill_amount=prefill_amount,
        prefill_person=prefill_person,
        prefill_note=prefill_note,
        cashboxes=cashboxes,
    )

# ----------------- Settings/Admin stubs -----------------
@app.route(URL_PREFIX + "/settings", methods=["GET", "POST"])
@login_required
def settings_stub():
    admin_required()
    current_key, current_label = _pos_device_config()
    if request.method == "POST":
        form_id = (request.form.get("form_id") or "pos").strip().lower()
        if form_id == "pos":
            key = (request.form.get("pos_device") or "none").strip()
            if key not in dict(POS_DEVICE_CHOICES):
                flash("دستگاه انتخاب‌شده نامعتبر است.", "danger")
                return redirect(URL_PREFIX + "/settings")
            Setting.set("pos_device", key)
            db.session.commit()
            flash("تنظیمات ذخیره شد.", "success")
        elif form_id == "ui":
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
            api_key = (request.form.get("openai_api_key") or "").strip()
            model = (request.form.get("openai_model") or _assistant_model()).strip()
            valid_models = {k for k, _ in ASSISTANT_MODEL_CHOICES}
            if model not in valid_models:
                model = _assistant_model()
            Setting.set("openai_api_key", api_key)
            Setting.set("openai_model", model)
            db.session.commit()
            if api_key:
                flash("کلید و تنظیمات دستیار هوشمند ذخیره شد.", "success")
            else:
                flash("کلید دستیار پاک شد.", "info")
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
    )

@app.route(URL_PREFIX + "/assistant")
@login_required
def assistant_home():
    ensure_permission("assistant")
    api_ready = bool(_openai_api_key()) and OpenAI is not None
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
    if not _openai_api_key():
        return jsonify({"status": "error", "message": "ابتدا کلید API را در تنظیمات ثبت کنید."}), 400
    if OpenAI is None:
        return jsonify({"status": "error", "message": "کتابخانه openai در محیط نصب نشده است."}), 500

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
        plan = _prepare_invoice_plan(invoice_payload)
        invoice_preview = plan
        missing_partner = plan.get("missing_partner")
        missing_items = plan.get("missing_items")
        if missing_partner or missing_items:
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

    actions_summary = None
    actions_payload = result.get("actions") if isinstance(result.get("actions"), list) else []
    if actions_payload:
        actions_summary = _apply_assistant_actions(actions_payload)
        if actions_summary.get("errors"):
            app.logger.warning(
                "AI_ACTION_ERRORS count=%s", len(actions_summary.get("errors") or [])
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
    if not plan:
        return jsonify({"status": "error", "message": "اطلاعات فاکتور موجود نیست."}), 400

    try:
        outcome = _apply_invoice_plan(plan)
    except Exception as exc:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(exc)}), 400

    invoice = outcome["invoice"]
    return jsonify({
        "status": "ok",
        "invoice_number": invoice.number,
        "invoice_id": invoice.id,
    })

@app.route(URL_PREFIX + "/admin", methods=["GET"])
@login_required
def admin_stub():
    admin_required()
    return render_template("admin/dashboard.html", prefix=URL_PREFIX)


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

        if action == "update":
            if not username or username not in catalog:
                flash("کاربر مورد نظر یافت نشد.", "danger")
                return redirect(URL_PREFIX + "/admin/users")

            entry = catalog[username]
            if username == ADMIN_USERNAME:
                role = "admin"
                is_active = True
            entry["role"] = role
            entry["permissions"] = _permissions_for_role(role, requested_perms)
            entry["is_active"] = is_active

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
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    username = getattr(current_user, "username", None)

    event = AuditEvent(
        user=username,
        ip_address=ip,
        context=context or "general",
        action=action or "unknown",
        payload=json.dumps(payload, ensure_ascii=False, default=str) if payload is not None else None,
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


# ----------------- Search API -----------------
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
            txt = str(val).replace(",", "").strip()
            if not txt:
                return None
            return float(txt)
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

if __name__ == "__main__":
    if URL_PREFIX:
        print("Running with URL prefix:", URL_PREFIX)
    print(f"Activity log: {LOG_FILE}")
    app.run(host="0.0.0.0", port=PORT, debug=True)

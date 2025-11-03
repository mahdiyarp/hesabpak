# -*- coding: utf-8 -*-
import os, json, logging
from pathlib import Path
from datetime import datetime, timedelta, date

from flask import Flask, render_template, redirect, request, flash, session, jsonify, abort, current_app
from flask_login import LoginManager, login_user, logout_user, login_required, UserMixin, current_user
from dotenv import load_dotenv
from markupsafe import Markup
from sqlalchemy import func, or_, UniqueConstraint   # <- مهم

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
    jalali_reference,
)

# ----------------- Config -----------------
load_dotenv()
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

CASH_METHOD_LABELS = {
    "cash": "نقدی",
    "pos": "دستگاه پوز",
    "bank": "بانکی",
    "cheque": "چک",
}

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
    created_at= db.Column(db.DateTime, nullable=False, default=datetime.now)

    person    = db.relationship("Entity", lazy="joined")

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
        json.dump({"users":[{"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}]}, f, ensure_ascii=False, indent=2)

def load_users_dict():
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {u["username"]: {"password": u["password"]} for u in data.get("users", [])}
    except Exception:
        return {ADMIN_USERNAME: {"password": ADMIN_PASSWORD}}

# ----------------- Auth -----------------
login_manager = LoginManager(app)
login_manager.login_view = "login"

class User(UserMixin):
    def __init__(self, username: str):
        self.id = username
        self.username = username

@login_manager.user_loader
def load_user(user_id):
    return User(user_id)

def is_admin() -> bool:
    return current_user.is_authenticated and current_user.username == ADMIN_USERNAME

def admin_required():
    if not is_admin():
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
        "now_info": date_now_info(),
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

    upcoming_receive_cheques = (
        CashDoc.query.filter(
            CashDoc.doc_type == "receive",
            func.lower(func.coalesce(CashDoc.method, "")) == "cheque",
            CashDoc.date >= today,
            CashDoc.date <= horizon,
        )
        .order_by(CashDoc.date.asc())
        .all()
    )

    upcoming_payment_cheques = (
        CashDoc.query.filter(
            CashDoc.doc_type == "payment",
            func.lower(func.coalesce(CashDoc.method, "")) == "cheque",
            CashDoc.date >= today,
            CashDoc.date <= horizon,
        )
        .order_by(CashDoc.date.asc())
        .all()
    )

    def cheque_to_dict(doc: CashDoc):
        return {
            "id": doc.id,
            "number": doc.number,
            "person": doc.person.name if doc.person else "—",
            "amount": float(doc.amount or 0.0),
            "date": to_jdate_str(doc.date),
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
    )

@app.route(URL_PREFIX + "/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(URL_PREFIX + "/")
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        users = load_users_dict()
        if username in users and users[username]["password"] == password:
            login_user(User(username))
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
    if not is_admin(): abort(403)
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
    now_info = _now_info()
    inv_number_generated = jalali_reference("INV", now_info["datetime"])

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
                "date": inv.date,
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
            ))
        if df: cd_q = cd_q.filter(CashDoc.date >= df)
        if dt: cd_q = cd_q.filter(CashDoc.date <= dt)

        for d in cd_q.order_by(CashDoc.id.desc()).limit(500).all():
            rows.append({
                "kind": d.doc_type,
                "id": d.id,
                "number": d.number,
                "date": d.date,
                "person": d.person.name,
                "amount": d.amount,
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
    doc = CashDoc.query.get_or_404(doc_id)
    kind = "دریافت" if doc.doc_type == "receive" else "پرداخت"
    html = f"<b>نوع:</b> {kind}<br><b>شماره:</b> {doc.number}<br><b>تاریخ (شمسی):</b> {to_jdate_str(doc.date)}<br><b>طرف حساب:</b> {doc.person.name}<br><b>مبلغ:</b> {int(doc.amount):,}"
    return render_template("page.html", title="سند نقدی", content=Markup(html), prefix=URL_PREFIX)

# ===================== دریافت وجه =====================
@app.route(URL_PREFIX + "/receive", methods=["GET", "POST"])
@login_required
def receive():
    now_info = _now_info()
    rec_number = jalali_reference("RCV", now_info["datetime"])
    pos_device_key, pos_device_label = _pos_device_config()
    prefill_amount = None
    prefill_note = None
    prefill_person = None
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

        doc = CashDoc(
            doc_type="receive",
            number=number,
            date=rec_date,
            person_id=person.id,
            amount=amount,
            method=method,
            note=note
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
    )

# ===================== ویرایش سند نقدی =====================
@app.route(URL_PREFIX + "/cash/<int:doc_id>/edit", methods=["GET","POST"])
@login_required
def cash_edit(doc_id):
    if not is_admin(): abort(403)
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
    now_info = _now_info()
    pay_number = jalali_reference("PAY", now_info["datetime"])
    pos_device_key, pos_device_label = _pos_device_config()
    prefill_amount = None
    prefill_note = None
    prefill_person = None
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

        doc = CashDoc(
            doc_type="payment",
            number=number,
            date=pay_date,
            person_id=person.id,
            amount=amount,
            method=method,
            note=note
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
    )

# ----------------- Settings/Admin stubs -----------------
@app.route(URL_PREFIX + "/settings", methods=["GET", "POST"])
@login_required
def settings_stub():
    current_key, current_label = _pos_device_config()
    if request.method == "POST":
        key = (request.form.get("pos_device") or "none").strip()
        if key not in dict(POS_DEVICE_CHOICES):
            flash("دستگاه انتخاب‌شده نامعتبر است.", "danger")
            return redirect(URL_PREFIX + "/settings")
        Setting.set("pos_device", key)
        db.session.commit()
        flash("تنظیمات ذخیره شد.", "success")
        return redirect(URL_PREFIX + "/settings")
    return render_template(
        "settings.html",
        prefix=URL_PREFIX,
        pos_choices=POS_DEVICE_CHOICES,
        selected_pos=current_key,
    )

@app.route(URL_PREFIX + "/admin", methods=["GET"])
@login_required
def admin_stub():
    admin_html = "<p>مدیریت/بکاپ (در حال توسعه)</p>"
    return render_template("page.html", title="مدیریت", content=Markup(admin_html), prefix=URL_PREFIX)

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
                return f"{int(f):,}"
            return f"{f:,.2f}".rstrip("0").rstrip(".")
        except Exception:
            return str(val)

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
    # اگر مقدار ناشناس بود، به صورت عمومی جستجو کن
    if not targets.intersection(default_targets):
        targets = default_targets

    ordered_targets = [t for t in ["item", "person", "invoice", "receive", "payment"] if t in targets]

    results = []

    def limit_left() -> int:
        return max(0, limit - len(results))

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
            rows = (
                query.order_by(Entity.level.asc(), Entity.code.asc())
                .limit(remaining)
                .all()
            )
            for e in rows:
                meta_parts = []
                if e.unit:
                    meta_parts.append(e.unit)
                if e.stock_qty is not None:
                    meta_parts.append(f"موجودی: {fmt_number(e.stock_qty)}")
                if e.serial_no:
                    meta_parts.append(e.serial_no)
                results.append({
                    "id": e.id,
                    "type": "item",
                    "code": e.code or "",
                    "name": e.name or "",
                    "stock": float(e.stock_qty or 0.0) if e.stock_qty is not None else None,
                    "price": None,
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
        rows = (
            query.order_by(Entity.level.asc(), Entity.code.asc())
            .limit(remaining)
            .all()
        )
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
                "balance": float(e.balance or 0.0),
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
        rows = (
            query.order_by(Invoice.date.desc(), Invoice.number.desc())
            .limit(remaining)
            .all()
        )
        for inv in rows:
            meta_parts = []
            if inv.date:
                meta_parts.append(inv.date.strftime("%Y-%m-%d"))
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

        rows = (
            query.order_by(CashDoc.date.desc(), CashDoc.number.desc())
            .limit(remaining)
            .all()
        )
        for doc in rows:
            if doc.doc_type not in targets and len(targets) != len(default_targets):
                continue
            meta_parts = []
            if doc.date:
                meta_parts.append(doc.date.strftime("%Y-%m-%d"))
            meta_parts.append(f"مبلغ: {fmt_number(doc.amount)}")
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

if __name__ == "__main__":
    if URL_PREFIX:
        print("Running with URL prefix:", URL_PREFIX)
    print(f"Activity log: {LOG_FILE}")
    app.run(host="0.0.0.0", port=PORT, debug=True)

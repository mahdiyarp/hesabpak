# models/backup_models.py
from extensions import db
from datetime import datetime

class Setting(db.Model):
    __tablename__ = "settings"
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, nullable=True)

    @staticmethod
    def get(key, default=None):
        s = Setting.query.get(key)
        return s.value if s else default

    @staticmethod
    def set(key, value):
        s = Setting.query.get(key)
        if not s:
            s = Setting(key=key, value=value)
            db.session.add(s)
        else:
            s.value = value

class UserSettings(db.Model):
    """تنظیمات شخصی هر کاربر (کلید API، مدل، دستورالعمل‌ها و ...)"""
    __tablename__ = "user_settings"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), nullable=False, unique=True, index=True)
    openai_api_key = db.Column(db.Text, nullable=True)  # کلید API
    openai_model = db.Column(db.String(64), nullable=True)  # مدل (gpt-4o, gpt-4o-mini, ...)
    system_prompt = db.Column(db.Text, nullable=True)  # دستورالعمل‌های سیستمی
    temperature = db.Column(db.Float, nullable=True, default=0.7)  # دمای مدل
    max_tokens = db.Column(db.Integer, nullable=True)  # حداکثر توکن
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @staticmethod
    def get_for_user(username):
        """دریافت تنظیمات کاربر یا ایجاد رکورد جدید"""
        settings = UserSettings.query.filter_by(username=username).first()
        if not settings:
            settings = UserSettings(username=username)
            db.session.add(settings)
            db.session.flush()
        return settings

class BackupLog(db.Model):
    __tablename__ = "backup_log"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user = db.Column(db.String(64))
    reason = db.Column(db.String(128))
    filename = db.Column(db.String(256))
    size = db.Column(db.Integer)


class Token(db.Model):
    """مدل ساده‌ی توکن‌های داخلی که هنگام رویدادهای خاص (مثلاً ثبت کاربر)
    صادر می‌شوند و در زنجیرهٔ محلی نیز ثبت می‌گردند.
    """
    __tablename__ = "tokens"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    owner = db.Column(db.String(64), nullable=False, index=True)  # username
    amount = db.Column(db.Float, nullable=False, default=0.0)
    symbol = db.Column(db.String(32), nullable=False, default="HSP")
    reason = db.Column(db.String(128), nullable=True)
    metadata_json = db.Column(db.Text, nullable=True)
    tx_ref = db.Column(db.String(128), nullable=True)  # optional tx/hash when bridging to real chain

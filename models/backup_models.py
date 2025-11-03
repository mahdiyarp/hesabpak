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

class BackupLog(db.Model):
    __tablename__ = "backup_log"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user = db.Column(db.String(64))
    reason = db.Column(db.String(128))
    filename = db.Column(db.String(256))
    size = db.Column(db.Integer)

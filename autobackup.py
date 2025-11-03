# autobackup.py
# طوری طراحی شده که برای مدل‌های «سند» (مثل Sale, Purchase, Voucher و ...) JSON بکاپ بسازد.
from flask import current_app
from sqlalchemy import event
from sqlalchemy.inspection import inspect
from utils.backup_utils import autosave_record

# لیست مدل‌هایی که سند حسابداری محسوب می‌کنی:
TARGET_MODELS = []

def register_autobackup_for(models_list):
    global TARGET_MODELS
    TARGET_MODELS = models_list

def _obj_to_dict(obj):
    res = {}
    mapper = inspect(obj).mapper
    for col in mapper.columns:
        res[col.key] = getattr(obj, col.key)
    return res

def _attach_listeners(Model):
    @event.listens_for(Model, "after_insert")
    def _after_insert(mapper, connection, target):
        try:
            payload = _obj_to_dict(target)
            autosave_record(current_app, Model.__name__, payload.get("id") or payload.get("uuid") or "new", payload)
        except Exception as e:
            current_app.logger.exception(f"autosave insert failed: {e}")

    @event.listens_for(Model, "after_update")
    def _after_update(mapper, connection, target):
        try:
            payload = _obj_to_dict(target)
            autosave_record(current_app, Model.__name__, payload.get("id") or payload.get("uuid") or "upd", payload)
        except Exception as e:
            current_app.logger.exception(f"autosave update failed: {e}")

def init_autobackup(app):
    with app.app_context():
        for m in TARGET_MODELS:
            _attach_listeners(m)
        app.logger.info(f"[autosave] enabled for {[m.__name__ for m in TARGET_MODELS]}")

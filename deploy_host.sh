#!/usr/bin/env bash
set -euo pipefail

APP_NAME="hesabpak"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$REPO_ROOT}"
PUBLIC_HTML="${PUBLIC_HTML:-$HOME/public_html}"
VENV_DIR="${VENV_DIR:-$HOME/virtualenv/$APP_NAME}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
GIT_BRANCH="${GIT_BRANCH:-main}"
PASSENGER_WSGI="$PUBLIC_HTML/passenger_wsgi.py"
TMP_DIR="$PUBLIC_HTML/tmp"

usage() {
  cat <<USAGE
استفاده: $(basename "$0") <bootstrap|update|restart>

 bootstrap : ساخت یا به‌روزرسانی وِن‌و، نصب پیش‌نیازها و نوشتن فایل passenger_wsgi.py
 update    : git pull از شاخه فعلی، به‌روزرسانی وابستگی‌ها و ریستارت اپلیکیشن
 restart   : ریستارت اپلیکیشن (touch tmp/restart.txt)

متغیرهای قابل تنظیم:
  REPO_DIR       مسیر سورس (پیش‌فرض: مسیر همین اسکریپت)
  PUBLIC_HTML    ریشه دامنه (پیش‌فرض: ~/public_html)
  VENV_DIR       مسیر وِن‌و (پیش‌فرض: ~/virtualenv/hesabpak)
  GIT_REMOTE     نام ریموت (پیش‌فرض: origin)
  GIT_BRANCH     نام شاخه (پیش‌فرض: main)
USAGE
}

ensure_repo() {
  if [[ ! -d "$REPO_DIR/.git" ]]; then
    echo "❌ مسیر $REPO_DIR مخزن گیت نیست." >&2
    exit 1
  fi
}

ensure_venv() {
  if [[ ! -d "$VENV_DIR" ]]; then
    echo "➡️ ایجاد virtualenv در $VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
  # shellcheck disable=SC1090
  source "$VENV_DIR/bin/activate"
  pip install --upgrade pip wheel >/dev/null
  if [[ -f "$REPO_DIR/requirements.txt" ]]; then
    echo "➡️ نصب وابستگی‌ها از requirements.txt"
    pip install -r "$REPO_DIR/requirements.txt"
  fi
  deactivate
}

write_wsgi() {
  mkdir -p "$PUBLIC_HTML"
  cat > "$PASSENGER_WSGI" <<PYCODE
import os, sys
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.environ.get("HESABPAK_APP_DIR", r"$REPO_DIR")
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
venv_site = os.path.join(r"$VENV_DIR", "lib")
if os.path.isdir(venv_site):
    for entry in os.listdir(venv_site):
        site_path = os.path.join(venv_site, entry, "site-packages")
        if os.path.isdir(site_path) and site_path not in sys.path:
            sys.path.insert(0, site_path)
            break
os.environ.setdefault("FLASK_ENV", "production")
os.environ.setdefault("HESABPAK_DATA", os.path.join(APP_ROOT, "data"))
from app import app as application
PYCODE
  echo "✅ passenger_wsgi.py آماده شد در $PASSENGER_WSGI"
}

restart_app() {
  mkdir -p "$TMP_DIR"
  touch "$TMP_DIR/restart.txt"
  echo "🔁 Passenger ریستارت شد."
}

update_repo() {
  ensure_repo
  echo "➡️ دریافت آخرین تغییرات از $GIT_REMOTE/$GIT_BRANCH"
  git -C "$REPO_DIR" fetch "$GIT_REMOTE"
  git -C "$REPO_DIR" pull "$GIT_REMOTE" "$GIT_BRANCH"
}

CMD="${1:-}";
case "$CMD" in
  bootstrap)
    ensure_repo
    ensure_venv
    write_wsgi
    restart_app
    ;;
  update)
    update_repo
    ensure_venv
    restart_app
    ;;
  restart)
    restart_app
    ;;
  ""|-h|--help)
    usage
    ;;
  *)
    echo "دستور ناشناخته: $CMD" >&2
    usage
    exit 1
    ;;
 esac

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
 update    : بکاپ امن، دریافت fast-forward، به‌روزرسانی وابستگی‌ها و ریستارت Passenger
 restart   : ریستارت Passenger (touch tmp/restart.txt)

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

ensure_clean_tree() {
  local status
  status="$(git -C "$REPO_DIR" status --porcelain)"
  if [[ -n "$status" ]]; then
    echo "❌ تغییر محلی ثبت‌نشده وجود دارد؛ برای جلوگیری از از دست رفتن تغییرات، update متوقف شد." >&2
    printf '%s\n' "$status" >&2
    exit 1
  fi
}

ensure_official_remote() {
  local remote_url expected
  remote_url="$(git -C "$REPO_DIR" remote get-url "$GIT_REMOTE" 2>/dev/null || true)"
  expected="https://github.com/mahdiyarp/hesabpak.git"
  remote_url="${remote_url%/}"
  expected="${expected%/}"
  if [[ "$remote_url" != "$expected" ]]; then
    echo "❌ ریموت $GIT_REMOTE مورد انتظار حساب‌پاک نیست: $remote_url" >&2
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

backup_before_update() {
  if [[ ! -f "$REPO_DIR/.env" ]]; then
    echo "⚠️ .env پیدا نشد؛ بکاپ دیتای برنامه قبل از update قابل انجام نیست." >&2
    return 1
  fi
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "⚠️ Python محیط مجازی پیدا نشد؛ بکاپ برنامه قبل از update قابل انجام نیست." >&2
    return 1
  fi
  echo "➡️ ایجاد بکاپ کامل قبل از تغییر کد"
  (
    cd "$REPO_DIR"
    "$VENV_DIR/bin/python" -c 'from app import app; from utils.backup_utils import create_full_backup; print(create_full_backup(app, user="deploy", reason="pre-update"))'
  )
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
  ensure_official_remote
  ensure_clean_tree

  echo "➡️ بررسی آخرین نسخه $GIT_REMOTE/$GIT_BRANCH"
  git -C "$REPO_DIR" fetch "$GIT_REMOTE" --prune

  local local_head remote_head merge_base current_branch
  local_head="$(git -C "$REPO_DIR" rev-parse HEAD)"
  remote_head="$(git -C "$REPO_DIR" rev-parse "$GIT_REMOTE/$GIT_BRANCH")"
  merge_base="$(git -C "$REPO_DIR" merge-base "$local_head" "$remote_head")"
  current_branch="$(git -C "$REPO_DIR" symbolic-ref --short HEAD 2>/dev/null || true)"

  if [[ "$current_branch" != "$GIT_BRANCH" ]]; then
    echo "❌ شاخه فعال '$current_branch' است، نه '$GIT_BRANCH'." >&2
    exit 1
  fi

  if [[ "$local_head" == "$remote_head" ]]; then
    echo "✅ سرور از نظر کد از قبل با $GIT_REMOTE/$GIT_BRANCH همگام است."
    return 10
  fi

  if [[ "$merge_base" != "$local_head" ]]; then
    echo "❌ شاخه محلی قابل fast-forward نیست؛ update برای جلوگیری از merge ناخواسته متوقف شد." >&2
    exit 1
  fi

  echo "🔍 کامیت‌های جدید:"
  git -C "$REPO_DIR" log --oneline --decorate "$local_head..$remote_head"

  if ! backup_before_update; then
    echo "❌ بدون بکاپ کامل، update کد انجام نمی‌شود." >&2
    exit 1
  fi

  git -C "$REPO_DIR" pull --ff-only "$GIT_REMOTE" "$GIT_BRANCH"
  echo "✅ سورس به $(git -C "$REPO_DIR" rev-parse --short HEAD) رسید."
}

CMD="${1:-}"
case "$CMD" in
  bootstrap)
    ensure_repo
    ensure_venv
    write_wsgi
    restart_app
    ;;
  update)
    if update_repo; then
      ensure_venv
      write_wsgi
      restart_app
    else
      code=$?
      [[ "$code" -eq 10 ]] || exit "$code"
      echo "ℹ️ نسخه جدیدی برای استقرار وجود ندارد."
    fi
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

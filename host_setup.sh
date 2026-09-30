#!/usr/bin/env bash
set -euo pipefail

APP_NAME=${APP_NAME:-hesabpak}
REMOTE_NAME=${REMOTE_NAME:-origin}
BRANCH_NAME=${BRANCH_NAME:-main}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO_URL="$(git -C "$SCRIPT_DIR" config --get remote.$REMOTE_NAME.url 2>/dev/null || true)"
REPO_URL=${REPO_URL:-$DEFAULT_REPO_URL}
REPO_PARENT=${REPO_PARENT:-$HOME/repositories}
REPO_DIR=${REPO_DIR:-$REPO_PARENT/$APP_NAME}
DEPLOY_SCRIPT=${DEPLOY_SCRIPT:-$REPO_DIR/deploy_host.sh}
PUBLIC_HTML=${PUBLIC_HTML:-$HOME/public_html}

usage() {
  cat <<USAGE
استفاده: $(basename "$0") <install|update|status> [گزینه‌ها]

گزینه‌های متداول:
  --repo-url URL        آدرس مخزن گیتهاب (پیش‌فرض: ${REPO_URL:-<الزامی>})
  --branch NAME         نام شاخه‌ای که دنبال می‌شود (پیش‌فرض: $BRANCH_NAME)
  --app-name NAME       نام برنامه برای مسیرها (پیش‌فرض: $APP_NAME)
  --repo-dir PATH       مسیر نصب مخزن (پیش‌فرض: $REPO_DIR)
  --deploy-script PATH  مسیر اسکریپت deploy_host.sh (پیش‌فرض: $DEPLOY_SCRIPT)

دستورات:
  install  دانلود/به‌روزرسانی مخزن و اجرای bootstrap برای Passenger
  update   بررسی تغییرات شاخه و در صورت تایید دریافت و استقرار آنها
  status   نمایش وضعیت فعلی مخزن و اختلاف با ریموت
USAGE
}

need_repo_url() {
  if [[ -z "$REPO_URL" ]]; then
    echo "❌ لطفاً آدرس مخزن را با --repo-url مشخص کنید." >&2
    exit 1
  fi
}

ensure_command() {
  local cmd="$1"
  local pkg_hint="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "❌ دستور $cmd پیدا نشد. $pkg_hint" >&2
    exit 1
  fi
}

ensure_tools() {
  ensure_command git "git را نصب کنید."
  ensure_command python3 "python3 را نصب کنید."
}

clone_or_update_repo() {
  mkdir -p "$REPO_PARENT"
  if [[ -d "$REPO_DIR/.git" ]]; then
    echo "ℹ️ مخزن قبلاً وجود دارد؛ برای نصب/بازسازی به‌صورت امن به‌روزرسانی می‌شود."
    git -C "$REPO_DIR" remote set-url "$REMOTE_NAME" "$REPO_URL"
    git -C "$REPO_DIR" fetch "$REMOTE_NAME" --prune
    git -C "$REPO_DIR" checkout "$BRANCH_NAME"
    git -C "$REPO_DIR" pull --ff-only "$REMOTE_NAME" "$BRANCH_NAME"
  else
    echo "⬇️ کلون کردن $REPO_URL در $REPO_DIR"
    git clone --branch "$BRANCH_NAME" "$REPO_URL" "$REPO_DIR"
  fi
}

run_bootstrap() {
  if [[ ! -x "$DEPLOY_SCRIPT" ]]; then
    if [[ -f "$DEPLOY_SCRIPT" ]]; then
      chmod +x "$DEPLOY_SCRIPT"
    else
      echo "❌ اسکریپت $DEPLOY_SCRIPT پیدا نشد." >&2
      exit 1
    fi
  fi
  echo "🚀 اجرای bootstrap جهت آماده‌سازی Passenger"
  (
    cd "$REPO_DIR"
    PUBLIC_HTML="$PUBLIC_HTML" GIT_BRANCH="$BRANCH_NAME" GIT_REMOTE="$REMOTE_NAME" "$DEPLOY_SCRIPT" bootstrap
  )
}

show_diff() {
  local base="$1" head="$2"
  if [[ "$base" == "$head" ]]; then
    echo "✅ کد سرور با $REMOTE_NAME/$BRANCH_NAME همگام است."
    return 1
  fi
  echo "🔍 تغییرات جدید نسبت به نسخه فعلی:"
  git -C "$REPO_DIR" log --oneline --decorate "$base..$head"
  return 0
}

confirm() {
  local prompt="$1"
  read -rp "$prompt [y/N]: " answer
  case "$answer" in
    y|Y|yes|Yes)
      return 0
      ;;
    *)
      echo "⏹️ به‌روزرسانی لغو شد."
      return 1
      ;;
  esac
}

install_flow() {
  need_repo_url
  ensure_tools
  clone_or_update_repo
  run_bootstrap
  echo "✅ نصب اولیه تکمیل شد. دامنه شما باید با passenger_wsgi.py بالا بیاید."
}

update_flow() {
  ensure_tools
  if [[ ! -d "$REPO_DIR/.git" ]]; then
    echo "❌ مخزن $REPO_DIR پیدا نشد. ابتدا دستور install را اجرا کنید." >&2
    exit 1
  fi

  git -C "$REPO_DIR" fetch "$REMOTE_NAME" --prune
  local local_head remote_head merge_base
  local_head="$(git -C "$REPO_DIR" rev-parse HEAD)"
  remote_head="$(git -C "$REPO_DIR" rev-parse "$REMOTE_NAME/$BRANCH_NAME")"
  merge_base="$(git -C "$REPO_DIR" merge-base "$local_head" "$remote_head")"

  if ! show_diff "$local_head" "$remote_head"; then
    return
  fi

  if [[ "$merge_base" != "$local_head" ]]; then
    echo "⚠️ شاخه محلی دارای تغییرات محلی/تاریخی است و fast-forward نیست. ابتدا آن را بررسی کنید." >&2
    exit 1
  fi

  if confirm "آیا تغییرات فوق روی سرور اعمال شود؟"; then
    # deploy_host.sh performs the actual backup + ff-only pull exactly once.
    (
      cd "$REPO_DIR"
      REPO_DIR="$REPO_DIR" PUBLIC_HTML="$PUBLIC_HTML" GIT_BRANCH="$BRANCH_NAME" GIT_REMOTE="$REMOTE_NAME" "$DEPLOY_SCRIPT" update
    )
    echo "✅ به‌روزرسانی با موفقیت انجام شد."
  fi
}

status_flow() {
  ensure_tools
  if [[ ! -d "$REPO_DIR/.git" ]]; then
    echo "ℹ️ مخزن در $REPO_DIR یافت نشد." >&2
    exit 1
  fi
  git -C "$REPO_DIR" status -sb
  git -C "$REPO_DIR" remote -v | grep "^$REMOTE_NAME" || true
  git -C "$REPO_DIR" fetch "$REMOTE_NAME" --prune
  show_diff "$(git -C "$REPO_DIR" rev-parse HEAD)" "$(git -C "$REPO_DIR" rev-parse "$REMOTE_NAME/$BRANCH_NAME")" || true
}

ACTION="${1:-}"
if [[ -z "$ACTION" ]]; then
  usage
  exit 1
fi
shift

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-url)
      REPO_URL="$2"
      shift 2
      ;;
    --branch)
      BRANCH_NAME="$2"
      shift 2
      ;;
    --app-name)
      APP_NAME="$2"
      shift 2
      ;;
    --repo-dir)
      REPO_DIR="$2"
      shift 2
      ;;
    --deploy-script)
      DEPLOY_SCRIPT="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "گزینه ناشناخته: $1" >&2
      usage
      exit 1
      ;;
  esac
done

case "$ACTION" in
  install)
    install_flow
    ;;
  update)
    update_flow
    ;;
  status)
    status_flow
    ;;
  *)
    echo "دستور ناشناخته: $ACTION" >&2
    usage
    exit 1
    ;;
esac

#!/usr/bin/env bash
set -euo pipefail

# Compatibility wrapper.
# The single source of truth for production updates is deploy_host.sh.
REPO_DIR="${REPO_DIR:-$(pwd)}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
GIT_BRANCH="${GIT_BRANCH:-main}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-dir) REPO_DIR="$2"; shift 2;;
    --remote) GIT_REMOTE="$2"; shift 2;;
    --branch) GIT_BRANCH="$2"; shift 2;;
    --no-restart) echo "❌ --no-restart دیگر پشتیبانی نمی‌شود؛ استقرار production باید با restart کامل انجام شود." >&2; exit 2;;
    -h|--help) echo "این فایل فقط wrapper سازگار است؛ مسیر واقعی بروزرسانی deploy_host.sh update است."; exit 0;;
    *) echo "❌ گزینه ناشناخته: $1" >&2; exit 2;;
  esac
done

if [[ ! -d "$REPO_DIR/.git" ]]; then echo "❌ مسیر $REPO_DIR مخزن Git نیست." >&2; exit 1; fi
if [[ ! -x "$REPO_DIR/deploy_host.sh" ]]; then echo "❌ deploy_host.sh در مسیر $REPO_DIR وجود ندارد یا executable نیست." >&2; exit 1; fi

exec env REPO_DIR="$REPO_DIR" GIT_REMOTE="$GIT_REMOTE" GIT_BRANCH="$GIT_BRANCH" PUBLIC_HTML="${PUBLIC_HTML:-$HOME/public_html}" VENV_DIR="${VENV_DIR:-$HOME/virtualenv/hesabpak}" "$REPO_DIR/deploy_host.sh" update

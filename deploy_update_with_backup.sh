#!/usr/bin/env bash
set -euo pipefail

# deploy_update_with_backup.sh
# Usage: ./deploy_update_with_backup.sh [--repo-dir PATH] [--remote NAME] [--branch NAME] [--no-restart]
# Example:
#   ./deploy_update_with_backup.sh --repo-dir ~/hesabpak --remote origin --branch main
# What it does:
#  1) cd into repo
#  2) create a timestamped backup of data/hesabpak.sqlite3 (if exists)
#  3) git fetch && git pull --ff-only
#  4) activate virtualenv (if venv exists) and pip install -r requirements.txt
#  5) run optional post-deploy hook (if present: scripts/post_deploy.sh)
#  6) restart Passenger by touching tmp/restart.txt (or call deploy_host.sh restart if present)

REPO_DIR="${REPO_DIR:-$(pwd)}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
GIT_BRANCH="${GIT_BRANCH:-main}"
NO_RESTART=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-dir)
      REPO_DIR="$2"; shift 2;;
    --remote)
      GIT_REMOTE="$2"; shift 2;;
    --branch)
      GIT_BRANCH="$2"; shift 2;;
    --no-restart)
      NO_RESTART=1; shift;;
    -h|--help)
      echo "Usage: $0 [--repo-dir PATH] [--remote NAME] [--branch NAME] [--no-restart]"; exit 0;;
    *) echo "Unknown arg: $1"; exit 1;;
  esac
done

if [[ ! -d "$REPO_DIR" ]]; then
  echo "❌ Repo dir not found: $REPO_DIR" >&2
  exit 1
fi

cd "$REPO_DIR"

# 1) Backup sqlite (if exists)
DB_PATH="$REPO_DIR/data/hesabpak.sqlite3"
if [[ -f "$DB_PATH" ]]; then
  TIMESTAMP=$(date +%Y%m%dT%H%M%S)
  BACKUP_DIR="$REPO_DIR/data/backups/fast_update"
  mkdir -p "$BACKUP_DIR"
  cp -v "$DB_PATH" "$BACKUP_DIR/hesabpak.sqlite3.$TIMESTAMP.bak"
  echo "Backup created: $BACKUP_DIR/hesabpak.sqlite3.$TIMESTAMP.bak"
else
  echo "No sqlite DB at $DB_PATH; skipping DB backup"
fi

# also snapshot last_autosave and activity log (best-effort)
if [[ -f "$REPO_DIR/data/backups/autosave/_last_autosave.json" ]]; then
  cp -v "$REPO_DIR/data/backups/autosave/_last_autosave.json" "$REPO_DIR/data/backups/fast_update/_last_autosave.json.$TIMESTAMP.bak" || true
fi
if [[ -f "$REPO_DIR/data/activity.log" ]]; then
  cp -v "$REPO_DIR/data/activity.log" "$REPO_DIR/data/backups/fast_update/activity.log.$TIMESTAMP.bak" || true
fi

# 2) Update repo
if [[ ! -d ".git" ]]; then
  echo "❌ Not a git repo: $REPO_DIR" >&2
  exit 1
fi

echo "Fetching updates from $GIT_REMOTE/$GIT_BRANCH..."
git fetch "$GIT_REMOTE" --prune
# make sure local branch exists and track remote
if git show-ref --verify --quiet refs/heads/$GIT_BRANCH; then
  git checkout "$GIT_BRANCH"
else
  git checkout -b "$GIT_BRANCH" "$GIT_REMOTE/$GIT_BRANCH" || git checkout --track "$GIT_REMOTE/$GIT_BRANCH"
fi

# ensure no local, uncommitted changes (fail-fast) unless user stashes
if [[ -n "$(git status --porcelain)" ]]; then
  echo "⚠️ Local changes detected. Please commit/stash them before running this script." >&2
  git status --porcelain
  exit 1
fi

# fast-forward
git pull --ff-only "$GIT_REMOTE" "$GIT_BRANCH"

echo "Repository updated."

# 3) Activate venv and install requirements
if [[ -d "venv" && -f "venv/bin/activate" ]]; then
  echo "Activating virtualenv..."
  # shellcheck disable=SC1091
  source venv/bin/activate
  if command -v pip >/dev/null 2>&1; then
    if [[ -f "requirements.txt" ]]; then
      echo "Installing requirements..."
      pip install -r requirements.txt || echo "pip install failed (continuing)"
    fi
  else
    echo "pip not found inside venv; skipping pip install"
  fi
  deactivate || true
else
  echo "No virtualenv found at $REPO_DIR/venv; skipping venv activation"
fi

# 4) Optional post-deploy hook
if [[ -x "scripts/post_deploy.sh" ]]; then
  echo "Running scripts/post_deploy.sh..."
  bash scripts/post_deploy.sh || echo "post_deploy failed (continuing)"
fi

# 5) Restart passenger (or use deploy_host.sh if present)
if [[ "$NO_RESTART" -eq 0 ]]; then
  if [[ -d "tmp" ]]; then
    mkdir -p tmp
    touch tmp/restart.txt
    echo "Passenger restart triggered (tmp/restart.txt)."
  else
    # fallback: try deploy_host.sh restart
    if [[ -x "deploy_host.sh" ]]; then
      echo "Calling deploy_host.sh restart"
      ./deploy_host.sh restart || echo "deploy_host restart failed"
    else
      echo "No tmp/ directory and no deploy_host.sh; please restart your app manually."
    fi
  fi
else
  echo "Restart suppressed by --no-restart"
fi

echo "Update-with-backup completed."

#!/usr/bin/env bash
set -euo pipefail

# Simple helper to push current branch and optionally trigger remote deploy.
# Usage:
#   ./scripts/push_and_deploy.sh -m "commit message" [--no-deploy]
# Environment variables:
#   REMOTE_HOST   - host to ssh to (optional)
#   REMOTE_USER   - ssh user (defaults to current user)
#   REMOTE_DEPLOY_SCRIPT - path to deploy script on remote (defaults to ~/repositories/hesabpak/deploy_host.sh)
#   GIT_REMOTE    - git remote name (default: origin)
#   GIT_BRANCH    - branch to push (default: current branch)

GIT_REMOTE=${GIT_REMOTE:-origin}
GIT_BRANCH=${GIT_BRANCH:-}
REMOTE_HOST=${REMOTE_HOST:-}
REMOTE_USER=${REMOTE_USER:-}
REMOTE_DEPLOY_SCRIPT=${REMOTE_DEPLOY_SCRIPT:-~/repositories/hesabpak/deploy_host.sh}
NO_DEPLOY=0
MSG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -m|--message)
      shift; MSG="$1"; shift;;
    --no-deploy)
      NO_DEPLOY=1; shift;;
    --remote-host)
      shift; REMOTE_HOST="$1"; shift;;
    --help|-h)
      echo "Usage: $0 -m \"commit message\" [--no-deploy]"; exit 0;;
    *) echo "Unknown arg: $1"; exit 1;;
  esac
done

# Determine branch if not provided
if [[ -z "$GIT_BRANCH" ]]; then
  GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
fi

if [[ -z "$MSG" ]]; then
  echo "Please provide a commit message with -m 'message'" >&2
  exit 1
fi

# Stage and commit any changes
git add -A
if git diff --cached --quiet; then
  echo "No staged changes to commit. Proceeding to push current branch '$GIT_BRANCH'..."
else
  git commit -m "$MSG"
fi

# Push
echo "Pushing to $GIT_REMOTE/$GIT_BRANCH..."
git push "$GIT_REMOTE" "$GIT_BRANCH"

echo "Push complete."

# Optionally trigger remote deploy via SSH
if [[ "$NO_DEPLOY" -eq 0 && -n "$REMOTE_HOST" ]]; then
  SSH_USER=${REMOTE_USER:-$USER}
  echo "Triggering remote deploy on ${SSH_USER}@${REMOTE_HOST} (script: $REMOTE_DEPLOY_SCRIPT)"
  ssh "${SSH_USER}@${REMOTE_HOST}" "bash -lc 'if [[ -x \"$REMOTE_DEPLOY_SCRIPT\" ]]; then $REMOTE_DEPLOY_SCRIPT update; else echo \"deploy script not found or not executable: $REMOTE_DEPLOY_SCRIPT\"; fi'"
  echo "Remote deploy triggered."
else
  if [[ "$NO_DEPLOY" -eq 1 ]]; then
    echo "Skipping remote deploy (--no-deploy)."
  else
    echo "REMOTE_HOST not provided; skipping remote deploy."
  fi
fi

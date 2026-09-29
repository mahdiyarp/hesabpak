#!/usr/bin/env bash
set -euo pipefail

# Compatibility wrapper: use the single hardened deployment implementation.
SCRIPT_URL="https://raw.githubusercontent.com/mahdiyarp/hesabpak/main/deploy_site.sh"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$SCRIPT_URL" | bash
elif command -v wget >/dev/null 2>&1; then
  wget -qO- "$SCRIPT_URL" | bash
else
  echo "خطا: curl یا wget لازم است." >&2
  exit 1
fi

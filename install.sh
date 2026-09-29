#!/usr/bin/env bash
set -euo pipefail

# Legacy compatibility entrypoint.
# Keep installation behavior in deploy_site.sh so permissions, backups and
# the production runtime cannot drift between installers.
SCRIPT_URL="https://raw.githubusercontent.com/mahdiyarp/hesabpak/main/deploy_site.sh"

echo "🚀 حساب‌پاک: استفاده از installer اصلی hardened"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$SCRIPT_URL" | bash
elif command -v wget >/dev/null 2>&1; then
  wget -qO- "$SCRIPT_URL" | bash
else
  echo "خطا: curl یا wget لازم است." >&2
  exit 1
fi

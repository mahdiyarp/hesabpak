#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$ROOT_DIR"

command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
  echo "❌ $PYTHON_BIN پیدا نشد." >&2
  exit 1
}

echo "==> Compile"
"$PYTHON_BIN" -m compileall app.py autobackup.py blueprints models utils

echo "==> Tests"
"$PYTHON_BIN" -m pytest -q

echo "✅ Local verification passed."

#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${1:-$ROOT_DIR/dist}"
STAMP="$(date +%Y%m%d-%H%M%S)"
WORK_DIR="$(mktemp -d)"
BUNDLE_DIR="$WORK_DIR/hesabpak-offline"
ARCHIVE="$OUT_DIR/hesabpak-offline-$STAMP.tar.gz"

cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

command -v python3 >/dev/null 2>&1 || { echo "python3 لازم است." >&2; exit 1; }
python3 -m pip --version >/dev/null 2>&1 || {
  echo "pip برای ساخت بسته آفلاین لازم است." >&2
  exit 1
}

mkdir -p "$BUNDLE_DIR/vendor/wheels" "$OUT_DIR"
cp -a "$ROOT_DIR"/. "$BUNDLE_DIR"/
rm -rf "$BUNDLE_DIR/.git" "$BUNDLE_DIR/data" "$BUNDLE_DIR/dist" "$BUNDLE_DIR/venv" "$BUNDLE_DIR/.ci-venv" "$BUNDLE_DIR/__pycache__"
find "$BUNDLE_DIR" -maxdepth 1 -type f -name '.env*' ! -name '.env.example' -delete
find "$BUNDLE_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} +

echo "⬇️ دریافت wheelهای همه وابستگی‌های runtime..."
python3 -m pip download \
  --dest "$BUNDLE_DIR/vendor/wheels" \
  -r "$BUNDLE_DIR/requirements.txt"

cat > "$BUNDLE_DIR/OFFLINE_BUNDLE.txt" <<EOF
حساب پاک — بسته نصب آفلاین
ساخته‌شده: $STAMP

این بسته باید روی سروری استفاده شود که به اینترنت نیاز ندارد.
تمام وابستگی‌های Python در vendor/wheels قرار گرفته‌اند.
EOF

tar -C "$WORK_DIR" -czf "$ARCHIVE" hesabpak-offline
sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"

echo "✅ بسته آفلاین ساخته شد:"
echo "   $ARCHIVE"
echo "✅ checksum:"
echo "   $ARCHIVE.sha256"
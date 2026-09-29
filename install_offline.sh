#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/var/www/hesabpak}"
BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$APP_DIR/venv"
SERVICE_NAME="${SERVICE_NAME:-hesabpak}"
SERVICE_USER="${SERVICE_USER:-www-data}"
PORT="${PORT:-8000}"

if [ "$(id -u)" -ne 0 ]; then
  echo "این installer باید با root اجرا شود: sudo bash install_offline.sh" >&2
  exit 1
fi

for cmd in python3 openssl nginx systemctl; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "❌ دستور $cmd روی سرور موجود نیست؛ در محیط کاملاً آفلاین باید پیش‌نیاز سیستم از قبل نصب شده باشد." >&2
    exit 1
  }
done

id "$SERVICE_USER" >/dev/null 2>&1 || {
  echo "❌ کاربر سرویس $SERVICE_USER وجود ندارد." >&2
  exit 1
}

[ -d "$BUNDLE_DIR/vendor/wheels" ] || {
  echo "❌ vendor/wheels پیدا نشد؛ این فایل باید داخل بسته آفلاین اجرا شود." >&2
  exit 1
}

mkdir -p "$APP_DIR"
if [ -f "$APP_DIR/.env" ]; then
  echo "❌ نصب روی مسیر دارای .env متوقف شد. برای بروزرسانی آفلاین از فرآیند migration/backup مخصوص استفاده کنید." >&2
  exit 1
fi

echo "📦 کپی سورس به $APP_DIR"
cp -a "$BUNDLE_DIR"/. "$APP_DIR"/
rm -rf "$APP_DIR/.git" "$APP_DIR/data" "$APP_DIR/__pycache__"
mkdir -p "$APP_DIR/data/backups" "$APP_DIR/data/uploads" "$APP_DIR/data/fiscal_cases" "$APP_DIR/tmp"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip --no-index --find-links "$APP_DIR/vendor/wheels"
python -m pip install --no-index --find-links "$APP_DIR/vendor/wheels" -r "$APP_DIR/requirements.txt"
deactivate

umask 077
SECRET_KEY_VALUE="$(openssl rand -hex 32)"
ADMIN_PASSWORD_VALUE="$(openssl rand -hex 12)"
CREDENTIAL_KEY_VALUE="$(openssl rand -hex 32)"
cat > "$APP_DIR/.env" <<EOF
FLASK_ENV=production
PORT=$PORT
SECRET_KEY=$SECRET_KEY_VALUE
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$ADMIN_PASSWORD_VALUE
DATA_DIR=data
SESSION_COOKIE_SECURE=false
MAX_UPLOAD_MB=15
DEMO_MODE=false
CREDENTIAL_ENCRYPTION_KEY=$CREDENTIAL_KEY_VALUE
EOF
chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

cat > "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=hesabpak Flask app (offline bundle)
After=network.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$APP_DIR
Environment=FLASK_APP=app.py
Environment=PORT=$PORT
Environment=URL_PREFIX=
EnvironmentFile=-$APP_DIR/.env
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=$APP_DIR/data $APP_DIR/tmp
ExecStart=$VENV_DIR/bin/gunicorn -b 127.0.0.1:$PORT "app:app"
Restart=always

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/nginx/sites-available/hesabpak <<EOF
server {
  listen 80;
  server_name _;

  location /static/ {
    alias $APP_DIR/static/;
    access_log off;
  }

  location / {
    proxy_pass http://127.0.0.1:$PORT;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
  }
}
EOF

ln -sf /etc/nginx/sites-available/hesabpak /etc/nginx/sites-enabled/hesabpak
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
nginx -t
systemctl restart nginx

echo
echo "✅ نصب آفلاین حساب پاک تکمیل شد."
echo "🔐 رمز مدیر اولیه داخل $APP_DIR/.env قرار دارد."
echo "⚠️ قبل از دسترسی عمومی، HTTPS را فعال کنید و SESSION_COOKIE_SECURE=true بگذارید."
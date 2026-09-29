#!/usr/bin/env bash
set -euo pipefail

# Simple deployment helper for Ubuntu 22.04+ (requires sudo)
# Usage: sudo bash deploy_site.sh

REPO_URL="https://github.com/mahdiyarp/hesabpak.git"
APP_DIR="/var/www/hesabpak"
VENV_DIR="$APP_DIR/venv"
SERVICE_NAME="hesabpak"
USER="www-data"
GROUP="www-data"
PORT=8000

echo "Deploying hesabpak to $APP_DIR"

# install prerequisites
apt-get update
apt-get install -y python3 python3-venv python3-pip git nginx openssl

# clone or pull
if [ -d "$APP_DIR/.git" ]; then
  echo "Updating existing repo"
  cd "$APP_DIR"
  if [ -n "$(git status --porcelain)" ]; then
    echo "Refusing to deploy over local uncommitted changes."
    exit 1
  fi

  # Create a full application backup before changing the deployed code.
  if [ -f "$APP_DIR/.env" ] && [ -x "$VENV_DIR/bin/python" ]; then
    echo "Creating pre-update full backup..."
    source "$VENV_DIR/bin/activate"
    DATA_DIR="${DATA_DIR:-$APP_DIR/data}" "$VENV_DIR/bin/python" -c 'from app import app; from utils.backup_utils import create_full_backup; print(create_full_backup(app, user="deploy", reason="pre-update"))'
    deactivate
  else
    echo "Existing deployment has no usable environment yet; skipping pre-update app backup."
  fi

  git fetch origin main --prune
  git merge --ff-only origin/main
else
  echo "Cloning repository"
  git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"

# Run the application as an unprivileged service account.
chown -R "$USER:$GROUP" "$APP_DIR"
mkdir -p "$APP_DIR/data/backups" "$APP_DIR/data/uploads" "$APP_DIR/data/fiscal_cases" "$APP_DIR/tmp"
chown -R "$USER:$GROUP" "$APP_DIR/data" "$APP_DIR/tmp"

# Generate production configuration only on first install.
if [ ! -f "$APP_DIR/.env" ]; then
  umask 077
  SECRET_KEY_VALUE="$(openssl rand -hex 32)"
  ADMIN_PASSWORD_VALUE="$(openssl rand -hex 12)"
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
CREDENTIAL_ENCRYPTION_KEY=$(openssl rand -base64 32 | tr -d '\n')
EOF
  chown "$USER:$GROUP" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
fi

if ! grep -q '^CREDENTIAL_ENCRYPTION_KEY=' "$APP_DIR/.env"; then
  umask 077
  printf '\nCREDENTIAL_ENCRYPTION_KEY=%s\n' "$(openssl rand -base64 32 | tr -d '\n')" >> "$APP_DIR/.env"
  chown "$USER:$GROUP" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
fi

# create venv and install requirements
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip
if [ -f requirements.txt ]; then
  pip install -r requirements.txt
fi

# create simple systemd service
cat > /etc/systemd/system/$SERVICE_NAME.service <<EOF
[Unit]
Description=hesabpak Flask app
After=network.target

[Service]
User=$USER
Group=$GROUP
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

systemctl daemon-reload
systemctl enable --now $SERVICE_NAME

# simple nginx site
cat > /etc/nginx/sites-available/hesabpak <<EOF
server {
  listen 80;
  server_name hesabpak.com www.hesabpak.com;

  location /static/ {
    alias $APP_DIR/static/;
    access_log off;
  }

  location / {
    proxy_pass http://127.0.0.1:$PORT;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
  }
}
EOF

ln -sf /etc/nginx/sites-available/hesabpak /etc/nginx/sites-enabled/hesabpak
nginx -t
systemctl restart nginx

echo "Deployment complete. Visit http://hesabpak.com (ensure DNS points to this host)"
echo "WARNING: this script currently serves HTTP only. Enable HTTPS before exposing the service publicly."
echo "Generated admin password is stored in $APP_DIR/.env."

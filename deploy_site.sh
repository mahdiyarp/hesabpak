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
apt-get install -y python3 python3-venv python3-pip git nginx

# clone or pull
if [ -d "$APP_DIR/.git" ]; then
  echo "Updating existing repo"
  cd "$APP_DIR"
  git pull origin main || true
else
  echo "Cloning repository"
  git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"

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
ExecStart=$VENV_DIR/bin/gunicorn -b 0.0.0.0:$PORT "app:app"
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

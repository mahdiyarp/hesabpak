#!/bin/bash
# نصب سریع حساب‌پاک - بدون نیاز به GitHub
# فقط کپی و پیست کنید!

set -e
echo "🚀 نصب حساب‌پاک..."

# نصب وابستگی‌ها
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv git nginx supervisor sqlite3 openssl -qq

# ایجاد دایرکتوری و انتخاب کاربر غیر root برای سرویس
APP_DIR="/var/www/hesabpak"
RUN_USER="${SUDO_USER:-$USER}"
if [ "$RUN_USER" = "root" ]; then
    RUN_USER="www-data"
fi
sudo mkdir -p "$APP_DIR"
sudo chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"
cd "$APP_DIR"

# دانلود کد
if [ ! -d ".git" ]; then
    git clone https://github.com/mahdiyarp/hesabpak.git .
else
    if [ -n "$(git status --porcelain)" ]; then
        echo "خطا: تغییر محلی ثبت‌نشده وجود دارد؛ نصب روی آن متوقف شد." >&2
        exit 1
    fi
    git fetch origin main --prune
    git merge --ff-only origin/main
fi

# تنظیمات
if [ ! -f .env ]; then
    SECRET=$(openssl rand -hex 32)
    ADMIN_PASS=$(openssl rand -hex 12)
    cat > .env << EOF
FLASK_ENV=production
PORT=8000
SECRET_KEY=$SECRET
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$ADMIN_PASS
DATA_DIR=data
SESSION_COOKIE_SECURE=false
MAX_UPLOAD_MB=15
EOF
    sudo chown "$RUN_USER:$RUN_USER" .env
    chmod 600 .env
fi

# Python setup
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

# دیتابیس
mkdir -p data/backups/autosave
python3 -c "from app import app, db; app.app_context().push(); db.create_all()"



# Supervisor
sudo tee /etc/supervisor/conf.d/hesabpak.conf > /dev/null << EOF
[program:hesabpak]
directory=$APP_DIR
command=$APP_DIR/venv/bin/gunicorn --bind 127.0.0.1:8000 "app:app"
user=$RUN_USER
environment=FLASK_ENV="production"
autostart=true
autorestart=true
stdout_logfile=/var/log/hesabpak.log
EOF

sudo mkdir -p /var/log
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl restart hesabpak 2>/dev/null || sudo supervisorctl start hesabpak

# Nginx
sudo tee /etc/nginx/sites-available/hesabpak > /dev/null << 'EOF'
server {
    listen 80;
    server_name _;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location /static {
        alias /var/www/hesabpak/static;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/hesabpak /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl restart nginx

echo ""
echo "✅ نصب موفق!"
echo "🌐 آدرس: http://$(hostname -I | awk '{print $1}')"
echo "👤 نام کاربری مدیر: admin"
echo "🔐 رمز مدیر در $APP_DIR/.env تولید شده است."
echo "📝 لاگ: sudo tail -f /var/log/hesabpak.log"
echo "⚠️ این نصب HTTP-only است؛ قبل از دسترسی عمومی HTTPS را فعال کنید."

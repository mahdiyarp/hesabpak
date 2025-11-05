#!/bin/bash
# نصب سریع حساب‌پاک - بدون نیاز به GitHub
# فقط کپی و پیست کنید!

set -e
echo "🚀 نصب حساب‌پاک..."

# نصب وابستگی‌ها
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv git nginx supervisor sqlite3 -qq

# ایجاد دایرکتوری
APP_DIR="/var/www/hesabpak"
sudo mkdir -p $APP_DIR
sudo chown -R $USER:$USER $APP_DIR
cd $APP_DIR

# دانلود کد
if [ ! -d ".git" ]; then
    git clone https://github.com/mahdiyarp/hesabpak.git .
else
    git pull
fi

# Python setup
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

# دیتابیس
mkdir -p data/backups/autosave
python3 -c "from app import app, db; app.app_context().push(); db.create_all()"

# تنظیمات
[ ! -f .env ] && cat > .env << 'EOF'
PORT=8000
SECRET_KEY=$(openssl rand -hex 32)
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin123
DATA_DIR=data
EOF

# Supervisor
sudo tee /etc/supervisor/conf.d/hesabpak.conf > /dev/null << EOF
[program:hesabpak]
directory=$APP_DIR
command=$APP_DIR/venv/bin/python app.py
user=$USER
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
echo "👤 ورود: admin / admin123"
echo "📝 لاگ: sudo tail -f /var/log/hesabpak.log"

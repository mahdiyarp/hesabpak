#!/bin/bash
# اسکریپت استقرار خودکار حساب‌پاک
# نسخه: 1.0
# تاریخ: 2025-11-05

set -e  # در صورت بروز خطا، اجرا متوقف شود

echo "🚀 شروع استقرار خودکار حساب‌پاک..."
echo "=================================================="

# رنگ‌ها برای خروجی بهتر
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# 1. بروزرسانی سیستم
echo -e "${BLUE}📦 بروزرسانی لیست بسته‌های سیستم...${NC}"
sudo apt-get update -qq

# 2. نصب وابستگی‌های سیستمی
echo -e "${BLUE}📦 نصب وابستگی‌های مورد نیاز...${NC}"
sudo apt-get install -y python3 python3-pip python3-venv git nginx supervisor sqlite3 -qq

# 3. ایجاد دایرکتوری پروژه
APP_DIR="/var/www/hesabpak"
echo -e "${BLUE}📁 ایجاد دایرکتوری پروژه: ${APP_DIR}${NC}"
sudo mkdir -p $APP_DIR
sudo chown -R $USER:$USER $APP_DIR
cd $APP_DIR

# 4. دانلود یا بروزرسانی کد از GitHub
if [ -d ".git" ]; then
    echo -e "${BLUE}🔄 بروزرسانی کد از GitHub...${NC}"
    git pull origin main
else
    echo -e "${BLUE}📥 دانلود کد از GitHub...${NC}"
    # اگر دایرکتوری خالی نیست، ابتدا خالی کن
    rm -rf $APP_DIR/*
    git clone https://github.com/mahdiyarp/hesabpak.git .
fi

# 5. ایجاد محیط مجازی Python
echo -e "${BLUE}🐍 ایجاد محیط مجازی Python...${NC}"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate

# 6. نصب پکیج‌های Python
echo -e "${BLUE}📦 نصب پکیج‌های Python...${NC}"
pip install --upgrade pip -q
pip install -r requirements.txt -q

# 7. ایجاد دایرکتوری داده
echo -e "${BLUE}📁 ایجاد دایرکتوری داده...${NC}"
mkdir -p data/backups/autosave

# 8. ایجاد فایل .env اگر وجود ندارد
if [ ! -f ".env" ]; then
    echo -e "${YELLOW}⚙️  ایجاد فایل تنظیمات .env${NC}"
    SECRET=$(openssl rand -hex 32)
    ADMIN_PASS=$(openssl rand -hex 12)
    cat > .env << EOF
PORT=8000
SECRET_KEY=$SECRET
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$ADMIN_PASS
DATA_DIR=data
URL_PREFIX=
EOF
fi

# 9. راه‌اندازی دیتابیس
echo -e "${BLUE}🗄️  راه‌اندازی دیتابیس...${NC}"
python3 << 'PYEOF'
from app import app, db
with app.app_context():
    db.create_all()
    print('✅ دیتابیس آماده شد')
PYEOF

# 10. پیکربندی Supervisor برای اجرای برنامه
echo -e "${BLUE}⚙️  پیکربندی Supervisor...${NC}"
sudo tee /etc/supervisor/conf.d/hesabpak.conf > /dev/null << EOF
[program:hesabpak]
directory=$APP_DIR
command=$APP_DIR/venv/bin/python app.py
user=$USER
autostart=true
autorestart=true
redirect_stderr=true
stdout_logfile=/var/log/hesabpak/app.log
stderr_logfile=/var/log/hesabpak/error.log
environment=PATH="$APP_DIR/venv/bin"
EOF

# ایجاد دایرکتوری لاگ
sudo mkdir -p /var/log/hesabpak
sudo chown -R $USER:$USER /var/log/hesabpak

# 11. پیکربندی Nginx
echo -e "${BLUE}🌐 پیکربندی Nginx...${NC}"
sudo tee /etc/nginx/sites-available/hesabpak > /dev/null << 'EOF'
server {
    listen 80;
    server_name _;  # یا دامنه شما: example.com

    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
        
        # WebSocket support (اگر در آینده نیاز باشد)
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    location /static {
        alias /var/www/hesabpak/static;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
}
EOF

# فعال‌سازی سایت
sudo ln -sf /etc/nginx/sites-available/hesabpak /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default

# تست پیکربندی Nginx
echo -e "${BLUE}🔍 بررسی پیکربندی Nginx...${NC}"
sudo nginx -t

# 12. راه‌اندازی سرویس‌ها
echo -e "${BLUE}🔄 راه‌اندازی سرویس‌ها...${NC}"
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl restart hesabpak
sudo systemctl restart nginx

# 13. نمایش وضعیت
echo ""
echo "=================================================="
echo -e "${GREEN}✅ استقرار با موفقیت انجام شد!${NC}"
echo "=================================================="
echo ""
echo -e "${YELLOW}📊 وضعیت سرویس‌ها:${NC}"
sudo supervisorctl status hesabpak
echo ""
echo -e "${YELLOW}🌐 دسترسی به برنامه:${NC}"
echo "   http://$(hostname -I | awk '{print $1}')"
echo "   http://localhost"
echo ""
echo -e "${YELLOW}👤 اطلاعات ورود پیش‌فرض:${NC}"
echo "   نام کاربری: admin"
echo "   رمز عبور: admin123"
echo ""
echo -e "${YELLOW}📝 لاگ‌ها:${NC}"
echo "   برنامه: /var/log/hesabpak/app.log"
echo "   خطاها: /var/log/hesabpak/error.log"
echo "   Nginx: /var/log/nginx/access.log"
echo ""
echo -e "${YELLOW}⚙️  دستورات مفید:${NC}"
echo "   مشاهده لاگ: sudo tail -f /var/log/hesabpak/app.log"
echo "   ری‌استارت: sudo supervisorctl restart hesabpak"
echo "   وضعیت: sudo supervisorctl status hesabpak"
echo "   توقف: sudo supervisorctl stop hesabpak"
echo ""
echo -e "${GREEN}🎉 حساب‌پاک آماده استفاده است!${NC}"

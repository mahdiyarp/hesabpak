# 🚀 دستورالعمل استقرار سریع حساب‌پاک

## روش 1️⃣: نصب یک‌خطی (توصیه می‌شود)

فقط این دستور را در ترمینال هاست کپی کنید:

```bash
curl -fsSL https://raw.githubusercontent.com/mahdiyarp/hesabpak/main/deploy_auto.sh | sudo bash
```

یا اگر `curl` ندارید، از `wget` استفاده کنید:

```bash
wget -qO- https://raw.githubusercontent.com/mahdiyarp/hesabpak/main/deploy_auto.sh | sudo bash
```

---

## روش 2️⃣: دستی (اگر روش اول کار نکرد)

### قدم 1: دانلود اسکریپت
```bash
wget https://raw.githubusercontent.com/mahdiyarp/hesabpak/main/deploy_auto.sh
```

### قدم 2: اجرای مجوز اجرا
```bash
chmod +x deploy_auto.sh
```

### قدم 3: اجرا
```bash
sudo ./deploy_auto.sh
```

---

## روش 3️⃣: نصب دستی کامل

اگر هر دو روش بالا کار نکرد:

```bash
# 1. بروزرسانی سیستم
sudo apt-get update && sudo apt-get upgrade -y

# 2. نصب وابستگی‌ها
sudo apt-get install -y python3 python3-pip python3-venv git nginx supervisor sqlite3

# 3. ایجاد دایرکتوری پروژه
sudo mkdir -p /var/www/hesabpak
sudo chown -R $USER:$USER /var/www/hesabpak
cd /var/www/hesabpak

# 4. دانلود کد
git clone https://github.com/mahdiyarp/hesabpak.git .

# 5. محیط مجازی Python
python3 -m venv venv
source venv/bin/activate

# 6. نصب پکیج‌ها
pip install --upgrade pip
pip install -r requirements.txt

# 7. ایجاد دایرکتوری داده
mkdir -p data/backups/autosave

# 8. تنظیمات محیطی
cat > .env << 'EOF'
PORT=8000
SECRET_KEY=your-secret-key-here-change-me
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin123
DATA_DIR=data
URL_PREFIX=
EOF

# 9. راه‌اندازی دیتابیس
python3 -c "from app import app, db; app.app_context().push(); db.create_all(); print('✅ Database ready')"

# 10. پیکربندی Supervisor
sudo tee /etc/supervisor/conf.d/hesabpak.conf > /dev/null << EOF
[program:hesabpak]
directory=/var/www/hesabpak
command=/var/www/hesabpak/venv/bin/python app.py
user=$USER
autostart=true
autorestart=true
redirect_stderr=true
stdout_logfile=/var/log/hesabpak/app.log
stderr_logfile=/var/log/hesabpak/error.log
EOF

sudo mkdir -p /var/log/hesabpak
sudo chown -R $USER:$USER /var/log/hesabpak

# 11. پیکربندی Nginx
sudo tee /etc/nginx/sites-available/hesabpak > /dev/null << 'EOF'
server {
    listen 80;
    server_name _;
    
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
    
    location /static {
        alias /var/www/hesabpak/static;
        expires 30d;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/hesabpak /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default

# 12. راه‌اندازی
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start hesabpak
sudo systemctl restart nginx

echo "✅ نصب کامل شد! برنامه در http://$(hostname -I | awk '{print $1}') در دسترس است"
```

---

## 🔍 بررسی وضعیت

```bash
# وضعیت سرویس
sudo supervisorctl status hesabpak

# مشاهده لاگ زنده
sudo tail -f /var/log/hesabpak/app.log

# ری‌استارت
sudo supervisorctl restart hesabpak
```

---

## 🔒 تنظیمات امنیتی (بعد از نصب)

### تغییر رمز ادمین:
1. وارد سیستم شوید
2. به قسمت تنظیمات بروید
3. رمز عبور را تغییر دهید

### فعال‌سازی HTTPS (با Let's Encrypt):
```bash
sudo apt-get install certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

### تغییر SECRET_KEY:
```bash
cd /var/www/hesabpak
# تولید کلید جدید
openssl rand -hex 32
# در فایل .env قرار دهید
nano .env
# ری‌استارت
sudo supervisorctl restart hesabpak
```

---

## 🆘 عیب‌یابی

### برنامه اجرا نمی‌شود:
```bash
sudo supervisorctl tail hesabpak stderr
```

### Nginx خطا می‌دهد:
```bash
sudo nginx -t
sudo tail -f /var/log/nginx/error.log
```

### دسترسی به دیتابیس:
```bash
cd /var/www/hesabpak
sqlite3 data/hesabpak.sqlite3
.tables
.quit
```

---

## 📊 اطلاعات پورت‌ها

- **8000**: برنامه Flask (داخلی)
- **80**: Nginx (خارجی)
- **443**: HTTPS (اگر SSL فعال باشد)

---

## 🔄 بروزرسانی

```bash
cd /var/www/hesabpak
git pull origin main
source venv/bin/activate
pip install -r requirements.txt
sudo supervisorctl restart hesabpak
```

---

**✅ موفق باشید!**

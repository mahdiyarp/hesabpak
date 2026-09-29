# 🚀 نصب یک‌دستوری حساب‌پاک

## کد نصب (فقط کپی و پیست کنید):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/mahdiyarp/hesabpak/main/install.sh)
```

### یا اگر `curl` ندارید:

```bash
bash <(wget -qO- https://raw.githubusercontent.com/mahdiyarp/hesabpak/main/install.sh)
```

---

## ✅ بعد از نصب:

- **آدرس سایت**: `http://IP_SERVER`
- **نام کاربری**: `admin`
- **رمز عبور**: هنگام نصب به‌صورت تصادفی در `.env` تولید می‌شود.

برای مشاهده رمز مدیر روی سرور:
`sudo grep '^ADMIN_PASSWORD=' /var/www/hesabpak/.env`

---

## 🔧 دستورات مفید:

```bash
# مشاهده لاگ
sudo tail -f /var/log/hesabpak.log

# ری‌استارت
sudo supervisorctl restart hesabpak

# وضعیت
sudo supervisorctl status hesabpak

# توقف
sudo supervisorctl stop hesabpak

# راه‌اندازی مجدد
sudo supervisorctl start hesabpak
```

---

## 🔄 بروزرسانی:

```bash
cd /var/www/hesabpak
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo supervisorctl restart hesabpak
```

---

## 🛡️ امنیت:

### فعال‌سازی HTTPS (رایگان):
```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
```

### تغییر رمز ادمین:
1. وارد سایت شوید
2. وارد بخش «مدیریت کاربران» شوید
3. برای کاربر `admin` رمز جدید تعیین و ذخیره کنید

---

## 📞 پشتیبانی:

- **GitHub**: https://github.com/mahdiyarp/hesabpak
- **مشکلات**: https://github.com/mahdiyarp/hesabpak/issues

---

**نصب موفقیت‌آمیز! 🎉**

# 🚀 استقرار سریع حساب‌پاک

همهٔ روش‌های نصب از مسیر hardened واحد استفاده می‌کنند.

## نصب روی Ubuntu

```bash
curl -fsSL https://raw.githubusercontent.com/mahdiyarp/hesabpak/main/deploy_site.sh | sudo bash
```

یا:

```bash
wget -qO- https://raw.githubusercontent.com/mahdiyarp/hesabpak/main/deploy_site.sh | sudo bash
```

مسیر نصب، production mode، Gunicorn، Nginx، secret تصادفی و بکاپ کامل قبل از update را تنظیم می‌کند.

## بررسی سرویس

```bash
sudo systemctl status hesabpak --no-pager
sudo journalctl -u hesabpak -n 100 --no-pager
sudo nginx -t
```

## HTTPS

نصب پایه HTTP است؛ قبل از دسترسی عمومی HTTPS را فعال کنید.

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

پس از HTTPS مقدار `SESSION_COOKIE_SECURE=true` را در `.env` قرار دهید.

## بروزرسانی امن

```bash
cd /var/www/hesabpak
sudo bash deploy_site.sh
```


## اعتبارسنجی محلی

برای اجرای همان compile و pytest مورد انتظار CI روی سرور یا سیستم توسعه:

```bash
chmod +x scripts/verify.sh
./scripts/verify.sh
```

این مسیر به GitHub Actions وابسته نیست و برای زمانی که runner گیت‌هاب در دسترس نباشد به‌عنوان verification محلی استفاده می‌شود.

## Paki و محیط آزمایشی

نسخه اصلی رابط کاربری از هویت تعاملی Paki با لوگوی hp استفاده می‌کند و برای نصب شدن روی دستگاه، manifest و service worker محلی دارد.

برای یک **سرور دمو جداگانه**، فایل `.env` را از نمونه کپی کنید و این مقادیر را تنظیم کنید:

```env
FLASK_ENV=production
DEMO_MODE=true
DEMO_USERNAME=demo
DEMO_PASSWORD=demo123
DATA_DIR=data-demo
CREDENTIAL_ENCRYPTION_KEY=<stable-random-secret>
```

در این حالت ورود آزمایشی از `/demo/start` انجام می‌شود. فضای دمو قبل از هر شروع دوباره پاک و seed می‌شود و با خروج از دمو نیز تمام داده‌های محیط آزمایشی، بکاپ‌ها و آپلودها پاک می‌شوند. **دمو را هرگز روی DATA_DIR نسخه واقعی حساب پاک قرار ندهید.**

کلید `CREDENTIAL_ENCRYPTION_KEY` باید در مهاجرت و restore حفظ شود؛ کلیدهای AI جدید در SQLite به‌صورت رمزنگاری‌شده ذخیره می‌شوند.

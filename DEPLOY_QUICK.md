# 🚀 استقرار سریع حساب‌پاک

همهٔ روش‌های نصب و بروزرسانی از مسیرهای hardened واحد استفاده می‌کنند.

## نصب روی Ubuntu

```bash
curl -fsSL https://raw.githubusercontent.com/mahdiyarp/hesabpak/main/deploy_site.sh | sudo bash
```

یا:

```bash
wget -qO- https://raw.githubusercontent.com/mahdiyarp/hesabpak/main/deploy_site.sh | sudo bash
```

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

در هاست Python/Passenger:

```bash
./host_setup.sh update --repo-url https://github.com/mahdiyarp/hesabpak.git --branch main
```

## اعتبارسنجی محلی

برای اجرای verification مستقل از GitHub Actions:

```bash
chmod +x scripts/verify.sh
./scripts/verify.sh
```

این اسکریپت علاوه بر compile و pytest، همهٔ اسکریپت‌های `.sh` پروژه را با `bash -n` و فایل‌های ضروری runtime را نیز بررسی می‌کند.

## صحت نسخه لایو

بعد از استقرار:

```
/healthz
```

پاسخ شامل `status`، `version` و `commit` است تا نسخه واقعی کد در حال اجرا مشخص شود.

## Paki و دمو

دمو فقط روی instance و DATA_DIR جدا اجرا شود. فضای دمو قبل از شروع دوباره reset می‌شود و هنگام logout پاک‌سازی می‌شود.

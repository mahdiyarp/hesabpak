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

# نصب آفلاین حساب پاک

برای فروش/استقرار روی سروری که دسترسی اینترنت ندارد، ابتدا روی یک سیستم آنلاین بسته را بسازید:

```bash
chmod +x build_offline_bundle.sh
./build_offline_bundle.sh
```

فایل خروجی در `dist/` همراه checksum ساخته می‌شود. فایل tar.gz را به سرور آفلاین منتقل و استخراج کنید:

```bash
tar -xzf hesabpak-offline-YYYYMMDD-HHMMSS.tar.gz
cd hesabpak-offline
sudo bash install_offline.sh
```

installer برای وابستگی‌های Python از `vendor/wheels` با `--no-index` استفاده می‌کند و در مرحله نصب به GitHub یا PyPI وصل نمی‌شود.

پیش‌نیازهای سیستمی مثل Python3/venv، OpenSSL، Nginx و systemd باید از قبل روی سرور آفلاین موجود باشند؛ installer عمداً در محیط بدون اینترنت `apt` اجرا نمی‌کند.

نصب آفلاین برای **نصب جدید** طراحی شده است و اگر مسیر مقصد از قبل `.env` داشته باشد عمداً متوقف می‌شود تا داده موجود بدون backup/migration دستکاری نشود.

دمو نیز باید روی یک instance و `DATA_DIR` جدا اجرا شود. این دمو برای یک instance آزمایشی مستقل/تک‌محیطی طراحی شده است؛ برای چند مشتری همزمان، برای هر مشتری یک instance و `DATA_DIR` جدا ایجاد کنید. کلید `CREDENTIAL_ENCRYPTION_KEY` را در migration/restore حفظ کنید.
# نصب حساب‌پاک

برای نصب جدید، از مسیر hardened استفاده کنید:

```bash
curl -fsSL https://raw.githubusercontent.com/mahdiyarp/hesabpak/main/deploy_site.sh | sudo bash
```

یا از ورودی سازگاری قدیمی استفاده کنید:

```bash
curl -fsSL https://raw.githubusercontent.com/mahdiyarp/hesabpak/main/install.sh | sudo bash
```

`install.sh` و `deploy_auto.sh` فقط wrapper هستند و منطق مستقل ندارند؛ بنابراین نصب قدیمی با مسیر production اصلی یکسان باقی می‌ماند.

پس از نصب، وضعیت سرویس:

```bash
sudo systemctl status hesabpak --no-pager
sudo journalctl -u hesabpak -n 100 --no-pager
```

نصب پایه HTTP است. قبل از دسترسی عمومی HTTPS را فعال کنید و سپس `SESSION_COOKIE_SECURE=true` را در `.env` قرار دهید.

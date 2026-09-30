# استقرار حساب‌پاک

مسیر production اصلی deploy_site.sh است و مسیر هاست Python/Passenger از deploy_host.sh استفاده می‌کند.

## Ubuntu / systemd
نصب جدید:

    curl -fsSL https://raw.githubusercontent.com/mahdiyarp/hesabpak/main/deploy_site.sh | sudo bash

بررسی:

    sudo systemctl status hesabpak --no-pager
    sudo journalctl -u hesabpak -n 100 --no-pager
    sudo nginx -t

قبل از دسترسی عمومی HTTPS را فعال کنید و در .env مقدار SESSION_COOKIE_SECURE=true قرار دهید.

## Passenger / هاست Python
نصب اولیه:

    chmod +x host_setup.sh deploy_host.sh
    ./host_setup.sh install --repo-url https://github.com/mahdiyarp/hesabpak.git --branch main

بروزرسانی:

    ./host_setup.sh update --repo-url https://github.com/mahdiyarp/hesabpak.git --branch main

مسیر بروزرسانی امن قبل از تغییر کد، وضعیت مخزن و remote رسمی را بررسی می‌کند، بکاپ کامل می‌سازد، فقط fast-forward را قبول می‌کند، وابستگی‌ها را نصب می‌کند و Passenger را restart می‌کند.

deploy_update_with_backup.sh فقط wrapper سازگار است و منطق مستقل ندارد؛ بنابراین خطاهای نسخه قدیمی دیگر نمی‌توانند باعث ادامه استقرار پس از شکست dependency یا restart شوند.

## صحت نسخه لایو
پس از استقرار endpoint /healthz باید در دسترس باشد. پاسخ شامل status، version و commit است تا نسخه واقعی کد در حال اجرا قابل تشخیص باشد.

## بکاپ و دمو
قبل از بروزرسانی production بکاپ کامل ساخته می‌شود. دمو فقط روی instance و DATA_DIR جدا اجرا شود. جزئیات در OFFLINE_INSTALL.md و DEPLOY_QUICK.md آمده است.

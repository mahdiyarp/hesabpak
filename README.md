# حساب پاک | HesabPak

نرم‌افزار حسابداری فارسی و RTL با هویت تعاملی Paki.

## قابلیت‌های اصلی

- فروش و خرید و مدیریت فاکتورها
- دریافت و پرداخت و صندوق/بانک
- اشخاص، کالاها و موجودی
- گزارش‌ها و سال‌های مالی
- جست‌وجوی سراسری
- بکاپ کامل و بازیابی
- ارسال بکاپ به ایمیل با SMTP
- دستیار هوشمند برای متن و تصویر فاکتور، با تأیید قبل از ثبت
- ثبت ledger مقاوم در برابر دستکاری برای ردیابی تغییرات
- کنترل دسترسی و سخت‌گیری‌های امنیتی برای استقرار
- رابط فارسی، RTL و واکنش‌گرا
- Paki تعاملی با حالت‌های blink / happy / thinking / idea / alert / sleepy
- manifest و service worker برای نصب‌پذیری و cache شدن assetهای محلی
- رمزنگاری credentialهای AI در حالت ذخیره‌شده

## نصب سریع

برای Ubuntu:

```bash
curl -fsSL https://raw.githubusercontent.com/mahdiyarp/hesabpak/main/deploy_site.sh | sudo bash
```

جزئیات استقرار در DEPLOY_QUICK.md و README_DEPLOY.md قرار دارد.

## محیط آزمایشی

دمو باید روی یک instance و DATA_DIR جدا اجرا شود:

```env
DEMO_MODE=true
DATA_DIR=data-demo
DEMO_USERNAME=demo
DEMO_PASSWORD=demo123
```

شروع دمو از /demo/start است. هر شروع دمو فضای آزمایشی را از نو می‌سازد و خروج از دمو workspace آزمایشی را پاک می‌کند. این حالت را روی داده واقعی فعال نکنید.

## نسخه

نسخه توسعه فعلی: 0.9.0

برای انتشار production نهایی، بررسی CI و release رسمی باید سبز و تکمیل شود.
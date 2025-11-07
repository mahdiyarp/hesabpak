# -*- coding: utf-8 -*-
"""
ابزارهای ساده برای تشخیص نوع ورودی بانکی (شماره کارت، شبا/IBAN، شماره حساب)
و استخراج اطلاعات پایه مانند BIN یا قطعات شبا.

تذکر: این ماژول یک MVP سبک است و نگاشت کامل BIN/کد بانک را ندارد.
می‌توانید دیکشنری‌های `BIN_TO_BANK` و `IBAN_BANK_CODES` را با داده‌های کامل‌تری تمدید کنید.
"""
from __future__ import annotations
import re
from typing import Optional, Dict, Any


# نمونه‌نگاشت‌های آزمایشی — کامل نیستند و باید در تولید تکمیل شوند.
BIN_TO_BANK: Dict[str, str] = {
    # نمونه: '627412': 'بانک ملت'  # <-- مقدار واقعی را در محیط خود تکمیل کنید
}

# نگاشت کد داخل شبا (IBAN) به نام بانک — این نگاشت نیز نمونه است
IBAN_BANK_CODES: Dict[str, str] = {
    # مثال: '017': 'بانک ملی ایران'
}


def _only_digits(s: str) -> str:
    return ''.join(ch for ch in (s or '') if ch.isdigit())


def detect_type(value: str) -> str:
    """تشخیص نوع ورودی: 'shaba' | 'card' | 'account' | 'unknown'"""
    if not value:
        return 'unknown'
    v = value.strip()
    # شبا معمولاً با IR شروع می‌شود و شامل 26 کاراکتر است (IR + 24 رقم)
    if re.match(r'^(IR|ir)[0-9]{24}$', v):
        return 'shaba'
    digits = _only_digits(v)
    # کارت بانکی معمولاً 16 رقم است (در ایران بعضی کارت‌ها 16 رقم هستند)
    if len(digits) in (13, 16, 19):
        return 'card'
    # شماره حساب‌ها طول‌های مختلف دارند؛ اگر 10-18 رقم است آن را شماره حساب درنظر می‌گیریم
    if 8 <= len(digits) <= 24:
        # از شبا جدا شده است، اما اگر با IR شروع نمی‌کند ممکن است شماره حساب باشد
        return 'account'
    return 'unknown'


def detect_bin(card_number: str) -> Optional[str]:
    d = _only_digits(card_number)
    if len(d) < 6:
        return None
    return d[:6]


def detect_bank_from_bin(bin6: str) -> Optional[Dict[str, Any]]:
    if not bin6:
        return None
    name = BIN_TO_BANK.get(bin6)
    if name:
        return {'method': 'bin', 'bank': name, 'bin': bin6}
    return None


def detect_bank_from_iban(iban: str) -> Optional[Dict[str, Any]]:
    if not iban:
        return None
    m = re.match(r'^(IR|ir)([0-9]{24})$', iban)
    if not m:
        return None
    digits = m.group(2)
    # در برخی نگارش‌ها کد بانک در چند رقم ابتدایی شبا قابل استخراج است.
    # این پیاده‌سازی تلاش می‌کند 3 تا 5 رقم اول را بررسی کند.
    for l in (4, 3, 5):
        code = digits[:l]
        name = IBAN_BANK_CODES.get(code)
        if name:
            return {'method': 'iban', 'bank': name, 'code': code}
    return None


def detect_bank(value: str) -> Dict[str, Any]:
    """تحلیل یک مقدار متنی و برگرداندن نوع و حدس بانک (در صورت وجود).

    خروجی نمونه:
    {
      'type': 'card',
      'normalized': '6037991234567890',
      'bin': '603799',
      'bank': {'method':'bin','bank':'بانک ملت'} or None,
    }
    """
    v = (value or '').strip()
    result: Dict[str, Any] = {'type': 'unknown', 'raw': v, 'normalized': None, 'bin': None, 'bank': None}

    if not v:
        return result

    typ = detect_type(v)
    result['type'] = typ

    if typ == 'shaba':
        result['normalized'] = v.upper()
        b = detect_bank_from_iban(result['normalized'])
        if b:
            result['bank'] = b
        return result

    digits = _only_digits(v)
    result['normalized'] = digits
    if typ == 'card':
        bin6 = detect_bin(digits)
        result['bin'] = bin6
        b = detect_bank_from_bin(bin6) if bin6 else None
        if b:
            result['bank'] = b
        return result

    if typ == 'account':
        # تلاش محدود برای تشخیص از روی قطعات حساب
        # (در MVP پیشنهاد می‌کنیم نگاشت دقیق را در تنظیمات وارد کنید)
        # همچنین اگر کاربر یک شبا بدون پیشوند IR فرستاد، آن را هم بررسی می‌کنیم
        if digits.startswith('98') and len(digits) == 26:
            # ممکن است شبا بدون 'IR' باشد
            pseudo = 'IR' + digits[2:]
            b = detect_bank_from_iban(pseudo)
            if b:
                result['bank'] = b
        return result

    return result

# -*- coding: utf-8 -*-
"""
ابزارهای اعتبارسنجی و تشخیص اطلاعات بانکی ایران.

- شماره کارت: نرمال‌سازی ارقام + Luhn + تشخیص BIN
- شبا/IBAN: نرمال‌سازی + طول/ساختار + ISO 7064 Mod-97 + تشخیص کد بانک
- شماره حساب: فقط تشخیص تقریبی؛ شماره حساب استاندارد واحدی ندارد.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional


PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


# پیشوندهای شناخته‌شدهٔ کارت بانکی. نگاشت‌های ناشناخته به‌صورت امن None می‌مانند.
BIN_TO_BANK: Dict[str, str] = {
    "603799": "بانک ملی ایران",
    "621986": "بانک سامان",
    "589210": "بانک سپه",
    "639346": "بانک سینا",
    "627648": "بانک توسعه صادرات",
    "639607": "بانک سرمایه",
    "627961": "بانک صنعت و معدن",
    "504706": "بانک شهر",
    "636214": "بانک آینده",
    "603770": "بانک کشاورزی",
    "502938": "بانک دی",
    "628023": "بانک مسکن",
    "603769": "بانک صادرات ایران",
    "627760": "پست بانک ایران",
    "610433": "بانک ملت",
    "502908": "بانک توسعه تعاون",
    "627353": "بانک تجارت",
    "627412": "بانک اقتصاد نوین",
    "589463": "بانک رفاه کارگران",
    "622106": "بانک پارسیان",
    "507677": "موسسه اعتباری نور",
    "502229": "بانک پاسارگاد",
    "606256": "موسسه اعتباری ملل",
    "639599": "بانک قوامین",
    "606373": "بانک قرض‌الحسنه مهر ایران",
    "627488": "بانک کارآفرین",
    "505416": "بانک گردشگری",
}


# کدهای سه‌رقمی بانک در شبا. کد در شماره شبا جایگاه ۴ تا ۶ (پس از IR و دو رقم کنترل) است.
IBAN_BANK_CODES: Dict[str, str] = {
    "010": "بانک مرکزی جمهوری اسلامی ایران",
    "011": "بانک صنعت و معدن",
    "012": "بانک ملت",
    "013": "بانک رفاه کارگران",
    "014": "بانک مسکن",
    "015": "بانک سپه",
    "016": "بانک کشاورزی",
    "017": "بانک ملی ایران",
    "018": "بانک تجارت",
    "019": "بانک صادرات ایران",
    "020": "بانک توسعه صادرات ایران",
    "021": "پست بانک ایران",
    "022": "بانک توسعه تعاون",
    "053": "بانک کارآفرین",
    "054": "بانک پارسیان",
    "055": "بانک اقتصاد نوین",
    "056": "بانک سامان",
    "057": "بانک پاسارگاد",
    "058": "بانک سرمایه",
    "059": "بانک سینا",
    "060": "بانک قرض‌الحسنه مهر ایران",
    "061": "بانک شهر",
    "062": "بانک آینده",
    "064": "بانک گردشگری",
    "066": "بانک دی",
    "069": "بانک ایران‌زمین",
    "070": "بانک قرض‌الحسنه رسالت",
    "075": "موسسه اعتباری ملل",
    "078": "بانک خاورمیانه",
}


def normalize_digits(value: str) -> str:
    text = (value or "").strip().translate(PERSIAN_DIGITS).translate(ARABIC_DIGITS)
    return text.replace(" ", "").replace("‌", "")


def _only_digits(s: str) -> str:
    return "".join(ch for ch in normalize_digits(s) if ch.isdigit())


def normalize_card(card_number: str) -> str:
    return _only_digits(card_number)


def normalize_iban(iban: str) -> str:
    value = normalize_digits(iban).upper().replace("-", "")
    return value


def validate_card(card_number: str) -> bool:
    """Validate Iranian card length and Luhn checksum."""
    digits = normalize_card(card_number)
    if len(digits) not in (16, 19):
        return False
    if not digits.isdigit():
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def validate_iban(iban: str) -> bool:
    """Validate Iranian IBAN using its 26-character structure and Mod-97."""
    value = normalize_iban(iban)
    if not re.fullmatch(r"IRd{24}", value):
        return False
    rearranged = value[4:] + value[:4]
    numeric = "".join(str(ord(ch) - 55) if ch.isalpha() else ch for ch in rearranged)
    remainder = 0
    for ch in numeric:
        remainder = (remainder * 10 + int(ch)) % 97
    return remainder == 1


def detect_type(value: str) -> str:
    """تشخیص نوع ورودی: shaba | card | account | unknown."""
    if not value:
        return "unknown"
    v = normalize_iban(value)
    if re.fullmatch(r"IRd{24}", v):
        return "shaba"
    digits = _only_digits(value)
    if len(digits) in (16, 19):
        return "card"
    if 8 <= len(digits) <= 24:
        return "account"
    return "unknown"


def detect_bin(card_number: str) -> Optional[str]:
    digits = normalize_card(card_number)
    return digits[:6] if len(digits) >= 6 else None


def detect_bank_from_bin(bin6: str) -> Optional[Dict[str, Any]]:
    if not bin6:
        return None
    name = BIN_TO_BANK.get(str(bin6)[:6])
    return {"method": "bin", "bank": name, "bin": str(bin6)[:6]} if name else None


def detect_bank_from_iban(iban: str) -> Optional[Dict[str, Any]]:
    value = normalize_iban(iban)
    if not re.fullmatch(r"IRd{24}", value):
        return None
    bank_code = value[4:7]
    name = IBAN_BANK_CODES.get(bank_code)
    return {"method": "iban", "bank": name, "code": bank_code} if name else None


def detect_bank(value: str) -> Dict[str, Any]:
    """تحلیل ورودی و تشخیص نوع، اعتبار و بانک در صورت وجود."""
    raw = (value or "").strip()
    result: Dict[str, Any] = {
        "type": "unknown",
        "raw": raw,
        "normalized": None,
        "valid": False,
        "bin": None,
        "bank": None,
    }
    if not raw:
        return result

    typ = detect_type(raw)
    result["type"] = typ

    if typ == "shaba":
        normalized = normalize_iban(raw)
        result["normalized"] = normalized
        result["valid"] = validate_iban(normalized)
        result["bank"] = detect_bank_from_iban(normalized)
        return result

    digits = normalize_card(raw)
    result["normalized"] = digits

    if typ == "card":
        result["valid"] = validate_card(digits)
        result["bin"] = detect_bin(digits)
        result["bank"] = detect_bank_from_bin(result["bin"])
        return result

    if typ == "account":
        # شماره حساب فرمت/کنترل عمومی واحدی ندارد؛ فقط نوع را تشخیص می‌دهیم.
        result["valid"] = bool(8 <= len(digits) <= 24)
        return result

    return result

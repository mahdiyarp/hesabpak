"""Utilities for rendering Persian words for numbers/amounts."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Union

NumberLike = Union[int, float, str, Decimal]


_ONES = [
    "صفر",
    "یک",
    "دو",
    "سه",
    "چهار",
    "پنج",
    "شش",
    "هفت",
    "هشت",
    "نه",
]

_TEENS = {
    10: "ده",
    11: "یازده",
    12: "دوازده",
    13: "سیزده",
    14: "چهارده",
    15: "پانزده",
    16: "شانزده",
    17: "هفده",
    18: "هجده",
    19: "نوزده",
}

_TENS = {
    2: "بیست",
    3: "سی",
    4: "چهل",
    5: "پنجاه",
    6: "شصت",
    7: "هفتاد",
    8: "هشتاد",
    9: "نود",
}

_HUNDREDS = {
    1: "صد",
    2: "دویست",
    3: "سیصد",
    4: "چهارصد",
    5: "پانصد",
    6: "ششصد",
    7: "هفتصد",
    8: "هشتصد",
    9: "نهصد",
}

_GROUPS = [
    (0, ""),
    (1, "هزار"),
    (2, "میلیون"),
    (3, "میلیارد"),
    (4, "تریلیون"),
]


def _chunk_to_words(n: int) -> str:
    parts: list[str] = []
    hundreds = n // 100
    tens_units = n % 100

    if hundreds:
        parts.append(_HUNDREDS[hundreds])
    if tens_units:
        if tens_units < 10:
            parts.append(_ONES[tens_units])
        elif tens_units in _TEENS:
            parts.append(_TEENS[tens_units])
        else:
            tens = tens_units // 10
            units = tens_units % 10
            tens_word = _TENS.get(tens)
            if tens_word:
                parts.append(tens_word)
            if units:
                parts.append(_ONES[units])
    return " و ".join(parts)


def number_to_persian_words(value: NumberLike) -> str:
    """Convert a number to Persian words (supports up to the trillions)."""
    try:
        number = Decimal(str(value).replace(",", "")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except Exception:
        raise ValueError("value is not a valid number")

    if number == 0:
        return _ONES[0]

    negative = number < 0
    number = abs(int(number))

    words: list[str] = []
    group_index = 0
    while number > 0:
        chunk = number % 1000
        if chunk:
            chunk_words = _chunk_to_words(chunk)
            group_title = _GROUPS[group_index][1]
            if group_title:
                words.append(f"{chunk_words} {group_title}" if chunk_words else group_title)
            else:
                words.append(chunk_words)
        number //= 1000
        group_index += 1
        if group_index >= len(_GROUPS) and number:
            # safeguard: extend with generic power naming
            _GROUPS.append((group_index, f"۱۰^{group_index*3}"))

    final = " و ".join(reversed([w for w in words if w]))
    return f"منفی {final}" if negative else final


def amount_to_toman_words(value: NumberLike) -> str:
    """Return a sentence describing the amount in tomans."""
    words = number_to_persian_words(value)
    return f"{words} تومان"

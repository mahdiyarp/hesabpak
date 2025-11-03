# utils/date_utils.py
"""Utility helpers for working with Gregorian and Jalali dates.

The project relies on these helpers as the single source of truth for any
conversion between Gregorian and Jalali calendars.  All modules (both server
and client side) should import the helpers from here instead of reimplementing
their own conversions so that the behaviour stays consistent across the
application.
"""

from __future__ import annotations

from datetime import datetime, date
from typing import Dict, Optional, Tuple, Union

# --- تبدیل میلادی به جلالی (بدون وابستگی خارجی) ---
def g2j(gy:int, gm:int, gd:int):
    g_d_m = [0,31,59,90,120,151,181,212,243,273,304,334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gm > 2 and (gy + 1) or gy
    days = (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) - 80 + gd + g_d_m[gm - 1]
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + (days // 31)
        jd = 1 + (days % 31)
    else:
        jm = 7 + ((days - 186) // 30)
        jd = 1 + ((days - 186) % 30)
    return jy, jm, jd


DateLike = Union[datetime, date]


def _extract_gregorian_parts(d: DateLike) -> Tuple[int, int, int]:
    """Return Gregorian ``(year, month, day)`` parts from ``date``/``datetime``."""

    if isinstance(d, datetime):
        return d.year, d.month, d.day
    if isinstance(d, date):
        return d.year, d.month, d.day
    raise TypeError("Unsupported date object")


def to_jdate_parts(d: DateLike) -> Tuple[int, int, int]:
    """Convert a Gregorian ``date``/``datetime`` to a Jalali tuple (jy, jm, jd)."""

    y, m, day = _extract_gregorian_parts(d)
    return g2j(y, m, day)


def to_jdate_str(d: DateLike, sep: str = "-") -> str:
    """Return Jalali date string (``YYYY{sep}MM{sep}DD``) for ``d``.

    If the input is not a ``date``/``datetime`` object the value is returned as
    is so that templates continue to display whatever text they already
    contain.
    """

    try:
        if isinstance(d, (datetime, date)):
            jy, jm, jd = to_jdate_parts(d)
            return f"{jy:04d}{sep}{jm:02d}{sep}{jd:02d}"
        return d
    except Exception:
        return d


def today_greg_date() -> date:
    """Return today's date in the Gregorian calendar."""

    return date.today()


def today_jalali_str(sep: str = "-") -> str:
    """Return today's date in the Jalali calendar as a formatted string."""

    return to_jdate_str(date.today(), sep=sep)


def now_greg_datetime() -> datetime:
    """Return current time as a ``datetime`` instance in local timezone."""

    return datetime.now()


def parse_gregorian_date(
    value: Optional[str],
    fallback: Optional[date] = None,
    *,
    allow_none: bool = False,
) -> Optional[date]:
    """Parse ``YYYY-MM-DD`` strings into ``date`` objects with graceful fallback."""

    raw = (value or "").strip()
    try:
        if not raw:
            raise ValueError("Empty date")
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except Exception:
        if allow_none and fallback is None:
            return None
        return fallback if fallback is not None else today_greg_date()


def jalali_reference_core(dt: Optional[datetime] = None) -> str:
    """Return ``YYYYMMDD-HHMMSS`` using Jalali date parts from ``dt``."""

    dt = dt or now_greg_datetime()
    jy, jm, jd = to_jdate_parts(dt)
    return f"{jy:04d}{jm:02d}{jd:02d}-{dt:%H%M%S}"


def jalali_reference(prefix: str, dt: Optional[datetime] = None) -> str:
    """Generate a reference code using Jalali date/time and provided prefix."""

    core = jalali_reference_core(dt)
    return f"{prefix}-{core}"


def now_info(dt: Optional[datetime] = None) -> Dict[str, Union[str, datetime]]:
    """Return a dictionary with consistently formatted now/today information."""

    dt = dt or now_greg_datetime()
    return {
        "datetime": dt,
        "greg_date": dt.date().isoformat(),
        "jalali_date": to_jdate_str(dt.date()),
        "jalali_reference": jalali_reference_core(dt),
    }

# اعداد فارسی
_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
def fa_digits(s: str) -> str:
    try:
        return str(s).translate(_PERSIAN_DIGITS)
    except Exception:
        return str(s)


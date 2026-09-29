# -*- coding: utf-8 -*-
"""
مدیریت نرخ‌های ارز و طلا.

نرخ‌ها به‌صورت snapshot محلی در `data/rates.json` ذخیره می‌شوند و در صورت
فعال‌بودن updater می‌توانند به‌طور دوره‌ای از منابع بیرونی تازه‌سازی شوند.
"""

from __future__ import annotations
import json
from pathlib import Path
import os
import tempfile
from typing import Dict, Any, Optional
import logging
import re
import threading
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

DATA_FILE = Path(__file__).resolve().parents[1] / 'data' / 'rates.json'
DEFAULT_REFRESH_SECONDS = 60
_updater_thread = None
_stop_event = None
logger = logging.getLogger(__name__)


def _ensure_path():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_rates() -> Dict[str, Any]:
    _ensure_path()
    try:
        if DATA_FILE.exists():
            return json.loads(DATA_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    # مقادیر پیش‌فرض تستی
    return {
        'updated_at': None,
        'currencies': {
            'USD': {'rate': None, 'unit': 'تومان'},
            'EUR': {'rate': None, 'unit': 'تومان'},
        },
        'gold': {
            'gram_18': None,
            'coin_full': None,
        }
    }


def _validate_rate_snapshot(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize the public rates snapshot shape."""
    if not isinstance(payload, dict):
        raise ValueError("ساختار snapshot نرخ‌ها باید یک شیء JSON باشد.")

    normalized = {
        "updated_at": payload.get("updated_at"),
        "currencies": {},
        "gold": {},
    }
    currencies = payload.get("currencies", {})
    if currencies is not None and not isinstance(currencies, dict):
        raise ValueError("بخش ارزهای snapshot نامعتبر است.")
    for code, entry in (currencies or {}).items():
        code = str(code).strip().upper()
        if not code or not isinstance(entry, dict):
            continue
        rate = entry.get("rate")
        if rate is not None:
            try:
                rate = float(rate)
            except (TypeError, ValueError):
                raise ValueError(f"نرخ ارز «{code}» نامعتبر است.")
            if rate < 0:
                raise ValueError(f"نرخ ارز «{code}» نمی‌تواند منفی باشد.")
        unit = str(entry.get("unit") or "تومان").strip()[:32]
        normalized["currencies"][code] = {"rate": rate, "unit": unit}

    gold = payload.get("gold", {})
    if gold is not None and not isinstance(gold, dict):
        raise ValueError("بخش طلا و سکه snapshot نامعتبر است.")
    for key, value in (gold or {}).items():
        key = str(key).strip()
        if not key:
            continue
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"مقدار طلا «{key}» نامعتبر است.")
            if value < 0:
                raise ValueError(f"مقدار طلا «{key}» نمی‌تواند منفی باشد.")
        normalized["gold"][key] = value

    return normalized


def save_rates(payload: Dict[str, Any]) -> None:
    """Persist the rates snapshot atomically after structural validation."""
    payload = _validate_rate_snapshot(payload)
    _ensure_path()
    fd, tmp_name = tempfile.mkstemp(
        prefix=".rates-", suffix=".json.tmp", dir=str(DATA_FILE.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, DATA_FILE)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except TypeError:
            if tmp_path.exists():
                tmp_path.unlink()


def set_rate(kind: str, key: str, value: Any) -> None:
    r = load_rates()
    if kind == 'currency':
        r.setdefault('currencies', {})
        r['currencies'].setdefault(key, {})
        r['currencies'][key]['rate'] = value
    elif kind == 'gold':
        r.setdefault('gold', {})
        r['gold'][key] = value
    r['updated_at'] = datetime.utcnow().isoformat()
    save_rates(r)


def get_rate_snapshot() -> Dict[str, Any]:
    return load_rates()


# --- Automatic fetchers -------------------------------------------------
def _parse_number(text: str) -> Optional[float]:
    """Parse a number from text, removing commas and Persian digits.

    Returns float or None on failure.
    """
    if not text:
        return None
    # replace Persian digits with Latin
    persian_digits = '۰۱۲۳۴۵۶۷۸۹'
    arabic_digits = '٠١٢٣٤٥٦٧٨٩'
    latin_digits = '0123456789'
    trans = {}
    trans.update({ord(a): b for a, b in zip(persian_digits, latin_digits)})
    trans.update({ord(a): b for a, b in zip(arabic_digits, latin_digits)})
    t = text.translate(trans)
    t = t.replace('٬', '').replace('،', '').replace('٫', '.')
    # remove non-digit, non-dot, non-comma
    t = re.sub(r"[^0-9.,-]", "", t)
    t = t.replace(',', '')
    t = t.replace('٬', '')
    try:
        return float(t)
    except Exception:
        return None


def fetch_usd_to_toman() -> Optional[float]:
    """Fetch USD -> Toman using exchangerate.host (USD -> IRR then divide by 10).

    Returns rate in Toman (IRR/10) or None.
    """
    try:
        url = 'https://api.exchangerate.host/latest?base=USD&symbols=IRR'
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        irrs = data.get('rates', {}).get('IRR')
        if irrs is None:
            logger.debug('exchangerate.host returned no IRR')
            return None
        # IRR per USD -> convert to Toman (1 Toman = 10 IRR)
        toman = float(irrs) / 10.0
        return toman
    except Exception as e:
        logger.debug('fetch_usd_to_toman failed: %s', e)
        return None


def fetch_gold_from_tgju() -> Dict[str, Optional[float]]:
    """Scrape tgju.org for common gold prices.

    Returns dict with keys 'gram_18' and 'coin_full' or None values on failure.
    Note: scraping is fragile; this function uses heuristics and falls back safely.
    """
    out = {'gram_18': None, 'coin_full': None}
    try:
        url = 'https://www.tgju.org/'
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        text = soup.get_text(separator=' ', strip=True)
        # Try to locate "هر گرم طلای 18 عیار" or similar label
        # We'll search near keywords and then extract following numbers
        # This is heuristic and may need tuning for the real website layout.
        # Find matches for numbers followed by 'گرم' or 'سکه' in Persian or English
        # First try to find a common label
        m = re.search(r'(?:طلای\s*18[^\d\n\r]*)([\d,٬\.\s۰-۹]+)', text)
        if m:
            g = _parse_number(m.group(1))
            out['gram_18'] = g
        # For full coin (سکه امامی, تمام) try to find 'سکه' + number
        m2 = re.search(r'(?:سکه[^\d\n\r]*)([\d,٬\.\s۰-۹]+)', text)
        if m2:
            c = _parse_number(m2.group(1))
            out['coin_full'] = c
        return out
    except Exception as e:
        logger.debug('fetch_gold_from_tgju failed: %s', e)
        return out


def fetch_and_update(save: bool = True) -> Dict[str, Any]:
    """Try to fetch currency and gold rates and update the local snapshot.

    Returns a dict with fetched values.
    """
    snapshot = load_rates()
    usd = fetch_usd_to_toman()
    gold = fetch_gold_from_tgju()
    if usd is not None:
        snapshot.setdefault('currencies', {})
        snapshot['currencies'].setdefault('USD', {})
        snapshot['currencies']['USD']['rate'] = usd
        snapshot['currencies']['USD']['unit'] = 'تومان'
    if gold:
        snapshot.setdefault('gold', {})
        if gold.get('gram_18') is not None:
            snapshot['gold']['gram_18'] = gold['gram_18']
        if gold.get('coin_full') is not None:
            snapshot['gold']['coin_full'] = gold['coin_full']
    snapshot['updated_at'] = datetime.utcnow().isoformat()
    if save:
        try:
            save_rates(snapshot)
        except Exception as e:
            logger.debug('failed to save rates snapshot: %s', e)
    return snapshot


def _updater_loop(interval_seconds: int):
    global _stop_event
    while not _stop_event.is_set():
        try:
            fetch_and_update(save=True)
        except Exception:
            logger.debug('background fetch_and_update raised', exc_info=True)
        # sleep in small increments so stop event is more responsive
        waited = 0
        while waited < interval_seconds and not _stop_event.is_set():
            time.sleep(1)
            waited += 1


def start_background_updater(interval_seconds: int = DEFAULT_REFRESH_SECONDS, run_on_start: bool = True) -> None:
    """Start a bounded background updater for rates."""
    global _updater_thread, _stop_event
    try:
        interval_seconds = max(10, min(int(interval_seconds), 3600))
    except (TypeError, ValueError):
        interval_seconds = DEFAULT_REFRESH_SECONDS
    if _updater_thread is not None and _updater_thread.is_alive():
        return
    _stop_event = threading.Event()
    if run_on_start:
        try:
            fetch_and_update(save=True)
        except Exception:
            logger.debug('initial fetch failed', exc_info=True)
    _updater_thread = threading.Thread(target=_updater_loop, args=(interval_seconds,), daemon=True)
    _updater_thread.start()


def stop_background_updater() -> None:
    global _stop_event, _updater_thread
    if _stop_event is None:
        return
    _stop_event.set()
    if _updater_thread is not None:
        _updater_thread.join(timeout=5)
    _updater_thread = None
    _stop_event = None


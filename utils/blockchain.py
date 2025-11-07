"""Utility helpers for the internal transparency blockchain."""

import hashlib
import json
from datetime import datetime
from typing import Iterable, Tuple

BLOCKCHAIN_DIFFICULTY = 3


def canonical_json_dumps(data) -> str:
    """Return a stable JSON representation suitable for hashing."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def timestamp_to_hash_input(dt: datetime) -> str:
    """Normalise timestamps used in hashing to a consistent UTC string."""
    return dt.replace(tzinfo=None).isoformat(timespec="microseconds")


def calculate_hash(index: int, timestamp_str: str, previous_hash: str, nonce: int, data_str: str) -> str:
    payload = f"{index}|{timestamp_str}|{previous_hash}|{nonce}|{data_str}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def mine_block(
    index: int,
    timestamp_str: str,
    previous_hash: str,
    data_str: str,
    *,
    difficulty: int = BLOCKCHAIN_DIFFICULTY,
) -> Tuple[int, str]:
    """Naively mine a block until the hash satisfies the difficulty prefix."""
    prefix = "0" * max(1, difficulty)
    nonce = 0
    while True:
        digest = calculate_hash(index, timestamp_str, previous_hash, nonce, data_str)
        if digest.startswith(prefix):
            return nonce, digest
        nonce += 1


def _block_attr(obj, name: str):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name)


def validate_chain(blocks: Iterable, *, difficulty: int = BLOCKCHAIN_DIFFICULTY):
    """Validate a sequence of blocks.

    Returns a tuple of (is_valid, issues).
    """
    issues: list[str] = []
    blocks = list(blocks)
    if not blocks:
        return True, issues

    prefix = "0" * max(1, difficulty)
    for i, block in enumerate(blocks):
        index = _block_attr(block, "index")
        previous_hash = _block_attr(block, "previous_hash") or ""
        block_hash = _block_attr(block, "hash") or ""
        nonce = _block_attr(block, "nonce") or 0
        timestamp = _block_attr(block, "timestamp")
        data_str = _block_attr(block, "data") or ""

        if timestamp is None:
            issues.append(f"بلوک {index} زمان ثبت مشخصی ندارد.")
            continue

        if hasattr(timestamp, "isoformat"):
            timestamp_str = timestamp_to_hash_input(timestamp)
        else:
            timestamp_str = str(timestamp)

        expected = calculate_hash(index, timestamp_str, previous_hash, nonce, data_str)
        if expected != block_hash:
            issues.append(f"هش بلوک {index} با داده‌های آن همخوان نیست.")

        if i > 0:
            prev_block = blocks[i - 1]
            prev_hash_expected = _block_attr(prev_block, "hash")
            if previous_hash != prev_hash_expected:
                issues.append(f"زنجیره بلوک {index} به بلوک قبلی متصل نیست.")
        elif previous_hash != "0" * 64:
            issues.append("بلوک جنسیس باید به هش صفر متصل باشد.")

        if not block_hash.startswith(prefix):
            issues.append(f"بلوک {index} با سختی {difficulty} مطابقت ندارد.")

        if index != i:
            issues.append(f"شماره‌گذاری بلوک {index} متوالی نیست.")

    return len(issues) == 0, issues


__all__ = [
    "BLOCKCHAIN_DIFFICULTY",
    "canonical_json_dumps",
    "calculate_hash",
    "mine_block",
    "timestamp_to_hash_input",
    "validate_chain",
]

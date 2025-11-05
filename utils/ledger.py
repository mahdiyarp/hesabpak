"""
Lightweight local append-only ledger (blockchain-like) for Hesab Pak.
- Stores chain in data/chain.json (configurable via HESABPAK_LEDGER_FILE env or app.config)
- Each block: {index, ts, prev_hash, data, hash, signature}
- Hash = sha256(prev_hash + json(data, sort_keys=True) + ts)
- Signature = HMAC-SHA256(hash, SECRET_KEY)

This is intentionally simple: it provides tamper-evidence and easy verification
without requiring a public blockchain. Later you can replace append_event with
an on-chain transaction (web3.py) and record tx_ref in the Token model.
"""
from __future__ import annotations

import os
import json
import hmac
import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional

DEFAULT_FILENAME = os.environ.get("HESABPAK_LEDGER_FILE") or "data/chain.json"


def _now_ts() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, path: Optional[str] = None, secret: Optional[str] = None):
        self.path = path or DEFAULT_FILENAME
        self.secret = secret or os.environ.get("SECRET_KEY", "change-me-please")
        # ensure folder exists
        d = os.path.dirname(self.path) or "data"
        os.makedirs(d, exist_ok=True)
        if not os.path.exists(self.path):
            self._init_chain()

    def _init_chain(self) -> None:
        genesis = {
            "index": 0,
            "ts": _now_ts(),
            "prev_hash": "0" * 64,
            "data": {"event": "genesis", "note": "Hesab Pak local ledger"},
        }
        genesis["hash"] = _sha256_hex(json.dumps(genesis["data"], sort_keys=True) + genesis["prev_hash"] + genesis["ts"])
        genesis["sig"] = hmac.new(self.secret.encode("utf-8"), genesis["hash"].encode("utf-8"), hashlib.sha256).hexdigest()
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump([genesis], fh, ensure_ascii=False, indent=2)

    def _read_chain(self) -> List[Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return []

    def get_chain(self) -> List[Dict[str, Any]]:
        return self._read_chain()

    def last_block(self) -> Optional[Dict[str, Any]]:
        chain = self._read_chain()
        if not chain:
            return None
        return chain[-1]

    def append_event(self, event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        chain = self._read_chain()
        last = chain[-1] if chain else None
        prev_hash = last["hash"] if last else "0" * 64
        index = (last["index"] + 1) if last else 1
        ts = _now_ts()
        data = {"event": event, "payload": payload}
        raw = json.dumps(data, sort_keys=True, ensure_ascii=False)
        h = _sha256_hex(raw + prev_hash + ts)
        sig = hmac.new(self.secret.encode("utf-8"), h.encode("utf-8"), hashlib.sha256).hexdigest()
        block = {
            "index": index,
            "ts": ts,
            "prev_hash": prev_hash,
            "data": data,
            "hash": h,
            "sig": sig,
        }
        chain.append(block)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(chain, fh, ensure_ascii=False, indent=2)
        return block

    def verify_chain(self) -> bool:
        chain = self._read_chain()
        if not chain:
            return True
        prev_hash = "0" * 64
        for blk in chain:
            expected_hash = _sha256_hex(json.dumps(blk.get("data", {}), sort_keys=True, ensure_ascii=False) + prev_hash + (blk.get("ts") or ""))
            if blk.get("hash") != expected_hash:
                return False
            expected_sig = hmac.new(self.secret.encode("utf-8"), expected_hash.encode("utf-8"), hashlib.sha256).hexdigest()
            if blk.get("sig") != expected_sig:
                return False
            prev_hash = blk.get("hash")
        return True


# convenience module-level instance helpers
_default = None


def get_ledger(path: Optional[str] = None, secret: Optional[str] = None) -> Ledger:
    global _default
    if _default is None:
        _default = Ledger(path=path, secret=secret)
    return _default


if __name__ == "__main__":
    l = get_ledger()
    print("Chain length:", len(l.get_chain()))
    print("Verified:", l.verify_chain())

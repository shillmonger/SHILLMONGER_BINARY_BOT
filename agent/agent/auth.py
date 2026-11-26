"""Authentication primitives for agent <-> sidecar tunnel messages.

Phase B ships the primitives + tests only; the tunnel client wires them in
Phase C. Scheme (mirrors the sidecar's HMAC style):

  signature = HMAC_SHA256(secret, message).hexdigest()

Tunnel AUTH frame message: "{agentId}.{unix_ts}.{nonce}"
Per-message signing:      "{unix_ts}.{correlation_id}.{canonical_json}"
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Optional

AUTH_MAX_AGE_SECONDS = 60
NONCE_CACHE_CAP = 4096


def sign(secret: str, message: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_signature(secret: str, message: str, signature: str) -> bool:
    expected = sign(secret, message)
    a = expected.encode("utf-8")
    b = (signature or "").encode("utf-8")
    return len(a) == len(b) and hmac.compare_digest(a, b)


def new_nonce() -> str:
    return secrets.token_hex(16)


def build_auth_message(agent_id: str, timestamp: Optional[int] = None,
                       nonce: Optional[str] = None) -> tuple[str, str, str]:
    """Return (message, timestamp_str, nonce) for an AUTH frame."""
    ts = str(int(timestamp if timestamp is not None else time.time()))
    n = nonce or new_nonce()
    return f"{agent_id}.{ts}.{n}", ts, n


def verify_auth_message(
    secret: str,
    agent_id: str,
    message: str,
    signature: str,
    *,
    max_age_seconds: int = AUTH_MAX_AGE_SECONDS,
    now: Optional[float] = None,
) -> bool:
    """Verify an AUTH message: format, agent id, freshness, signature."""
    parts = (message or "").split(".")
    if len(parts) != 3:
        return False
    msg_agent, ts_raw, _nonce = parts
    if msg_agent != agent_id:
        return False
    try:
        ts = int(ts_raw)
    except ValueError:
        return False
    now_ts = int(now if now is not None else time.time())
    if abs(now_ts - ts) > max_age_seconds:
        return False
    return verify_signature(secret, message, signature)


def canonical_json(payload: Any) -> str:
    """Deterministic JSON for per-message signing."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True,
                      ensure_ascii=False)


def sign_message(secret: str, timestamp: int, correlation_id: str,
                 payload: Any) -> tuple[str, str]:
    """Return (message_to_sign, signature) for a data frame."""
    body = canonical_json(payload)
    message = f"{timestamp}.{correlation_id}.{body}"
    return message, sign(secret, message)


def verify_message(secret: str, message: str, signature: str) -> bool:
    return verify_signature(secret, message, signature)


class NonceCache:
    """Bounded replay cache — remembers nonces until they age out."""

    def __init__(self, max_age_seconds: int = AUTH_MAX_AGE_SECONDS,
                 capacity: int = NONCE_CACHE_CAP) -> None:
        self._max_age = max_age_seconds
        self._capacity = capacity
        self._seen: dict[str, float] = {}

    def seen(self, nonce: str, now: Optional[float] = None) -> bool:
        """Record a nonce; returns True if it was ALREADY seen (replay)."""
        now_ts = now if now is not None else time.time()
        self._evict(now_ts)
        if nonce in self._seen:
            return True
        if len(self._seen) >= self._capacity:
            # Drop oldest entries rather than growing unbounded.
            for k in sorted(self._seen, key=self._seen.get)[: self._capacity // 4]:
                self._seen.pop(k, None)
        self._seen[nonce] = now_ts
        return False

    def _evict(self, now_ts: float) -> None:
        cutoff = now_ts - self._max_age
        stale = [k for k, ts in self._seen.items() if ts < cutoff]
        for k in stale:
            self._seen.pop(k, None)

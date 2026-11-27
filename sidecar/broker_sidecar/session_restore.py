"""Broker session auto-restore after sidecar restart.

Broker sessions are in-memory only, so every sidecar restart drops them
and live order placement fails with "no active session" until users
reconnect manually. This module lets the sidecar pull the credentials it
needs from the Next.js app's internal restore endpoint (HMAC-signed,
same scheme as the Telegram ingest forwarder) and rebuild sessions on
startup.

Safety:
  - Only runs when BROKER_SESSION_RESTORE is not 'off' (default: on).
  - Only restores sessions the app reports — the app itself only returns
    DEMO-mode accounts with stored credentials and a healthy connection
    status. REAL-mode accounts can never be restored.
  - OTP/2FA accounts cannot be auto-restored; they surface as
    requiresOtp and the user completes them through the normal UI flow.
  - Credentials are used transiently for the connect handshake and are
    never logged or persisted by the sidecar.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger("broker_sidecar.session_restore")

_FETCH_TIMEOUT = 15


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def enabled() -> bool:
    return _env("BROKER_SESSION_RESTORE").lower() not in ("off", "0", "false", "no")


def restore_url() -> Optional[str]:
    """Explicit SESSION_RESTORE_URL wins; otherwise derive the app origin
    from BINARY_INGEST_URL (same host the listener already posts to)."""
    explicit = _env("SESSION_RESTORE_URL")
    if explicit:
        return explicit
    ingest = _env("BINARY_INGEST_URL")
    if ingest:
        parsed = urlparse(ingest)
        if parsed.scheme and parsed.netloc:
            return (
                f"{parsed.scheme}://{parsed.netloc}"
                "/api/internal/binary-brokers/restore-sessions"
            )
    return None


def _sign(raw_body: str, timestamp: int, secret: str) -> str:
    """HMAC-SHA256 over '{timestamp}.{raw_body}' — mirrors lib/signal-ingest.ts."""
    return hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.{raw_body}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def fetch_restore_sessions() -> list[dict[str, Any]]:
    """GET the app's restore list. Returns [] when unconfigured; raises on
    transport/HTTP errors so the caller can retry."""
    import requests  # runtime dep — lazy import like message_listener

    url = restore_url()
    secret = _env("WORKER_HMAC_SECRET")
    if not url or not secret:
        logger.info("session restore not configured (url/secret missing)")
        return []
    ts = int(time.time())
    resp = requests.get(
        url,
        headers={"X-Timestamp": str(ts), "X-Signature": _sign("", ts, secret)},
        timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    sessions = data.get("sessions") if isinstance(data, dict) else None
    return sessions if isinstance(sessions, list) else []

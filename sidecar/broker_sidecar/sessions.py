"""Per-user broker session registry.

Keyed by ``(broker, user_id)`` so every application user gets an isolated
broker session. A failure on one broker/user never touches another session.
Passwords live only in memory for the lifetime of the session and are
never logged or returned by the API.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("broker_sidecar.sessions")

BROKERS = ("quotex", "iqoption")


@dataclass
class SessionMeta:
    """Sanitized, API-safe description of a live broker session."""

    connected: bool = False
    account_id: Optional[str] = None
    account_currency: Optional[str] = None
    account_mode: Optional[str] = None  # "PRACTICE" | "DEMO" | "REAL" ...
    balance: Optional[float] = None
    email: Optional[str] = None
    connected_at: Optional[float] = None
    last_check: Optional[float] = None
    last_error: Optional[str] = None
    latency_ms: Optional[float] = None
    requires_otp: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "account": {
                "accountId": self.account_id,
                "accountCurrency": self.account_currency,
                "accountMode": self.account_mode,
                "balance": self.balance,
                "email": self.email,
            },
            "email": self.email,
            "connectedAt": self.connected_at,
            "lastCheck": self.last_check,
            "lastError": self.last_error,
            "latencyMs": self.latency_ms,
            "requiresOtp": self.requires_otp,
        }


class BrokerSession:
    """Wraps one live broker client with its own lock and metadata."""

    def __init__(self, broker: str, user_id: str, email: str, password: str):
        self.broker = broker
        self.user_id = user_id
        self.email = email
        self._password = password
        self.meta = SessionMeta(email=email)
        self.client: Any = None
        self.lock = asyncio.Lock()
        self._otp_code: Optional[str] = None
        self.created_at = time.time()

    @property
    def password(self) -> str:
        return self._password

    def set_otp(self, code: str) -> None:
        self._otp_code = code
        self.meta.requires_otp = False

    def consume_otp(self) -> Optional[str]:
        code, self._otp_code = self._otp_code, None
        return code

    def key(self) -> str:
        return f"{self.broker}:{self.user_id}"


class SessionStore:
    """Thread-safe registry of live sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, BrokerSession] = {}
        self._guard = threading.Lock()

    def get(self, broker: str, user_id: str) -> Optional[BrokerSession]:
        with self._guard:
            return self._sessions.get(f"{broker}:{user_id}")

    def put(self, session: BrokerSession) -> None:
        with self._guard:
            self._sessions[session.key()] = session

    def drop(self, broker: str, user_id: str) -> Optional[BrokerSession]:
        with self._guard:
            return self._sessions.pop(f"{broker}:{user_id}", None)

    def list_keys(self) -> list[str]:
        with self._guard:
            return sorted(self._sessions.keys())


store = SessionStore()

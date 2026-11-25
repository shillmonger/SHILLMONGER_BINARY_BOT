"""Shared data types: connection states, status snapshots and error classes."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


class ConnectionState(str, enum.Enum):
    """Lifecycle state of one broker connection. Each broker owns its own state."""

    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    RECONNECTING = "RECONNECTING"
    UNSUPPORTED = "UNSUPPORTED"
    ERROR = "ERROR"

    def __str__(self) -> str:
        return self.value

    @property
    def is_up(self) -> bool:
        """True when a verified session exists (healthy or degraded)."""
        return self in (ConnectionState.CONNECTED, ConnectionState.DEGRADED)


class BrokerError(Exception):
    """Base class for connector errors."""


class IntegrationUnsupportedError(BrokerError):
    """No supported/authorized integration is available for this broker."""


class ConnectionTimeoutError(BrokerError):
    """A connection attempt exceeded its timeout."""


class PolicyViolationError(BrokerError):
    """Safety rule violated (for example: the account is not a DEMO account)."""


class AdapterLoadError(BrokerError):
    """The configured adapter class could not be imported or is invalid."""


@dataclass(frozen=True)
class HealthResult:
    """Outcome of one verification of a broker session."""

    ok: bool
    latency_ms: Optional[float] = None
    account_mode: Optional[str] = None
    error: Optional[str] = None
    timed_out: bool = False
    fatal: bool = False  # safety violation: do not retry


@dataclass(frozen=True)
class BrokerStatus:
    """Immutable snapshot of one broker's state, safe to hand to the UI."""

    broker: str
    state: ConnectionState
    configured_mode: str
    account_mode: Optional[str]  # verified by the adapter, None until verified
    last_check: Optional[datetime]
    latency_ms: Optional[float]
    detail: Optional[str]
    simulated: bool
    connection_attempts: int
    connected_since: Optional[datetime]

"""Broker adapter interface plus the two built-in adapters.

An *adapter* is the only piece that talks to a broker. The connectors (lifecycle,
health monitoring, reconnection, state) are broker-agnostic and never touch a
broker protocol themselves.

Built in:
* ``UnsupportedAdapter`` - default. Honest placeholder: reports UNSUPPORTED and never
  pretends to connect.
* ``SimulatedAdapter``   - in-memory fake for tests and ``run_both.py --simulate``.
  Always flagged ``simulated = True`` and labelled as such on the dashboard.

To use a real, authorized integration, subclass ``BrokerAdapter`` and set
``IQ_OPTION_ADAPTER`` / ``QUOTEX_ADAPTER`` to ``"package.module:ClassName"``.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from abc import ABC, abstractmethod
from typing import Optional

from .config import BrokerSettings
from .models import AdapterLoadError, IntegrationUnsupportedError


class BrokerAdapter(ABC):
    """Contract between a connector and a broker integration (all methods async).

    Implementations MUST:
    * raise from ``connect`` if the session could not be established;
    * make ``is_connected`` reflect the real session (not a cached login flag);
    * report the true account type from ``get_account_mode`` ("DEMO"/"PRACTICE" or
      anything else, which the connector treats as a safety violation);
    * never log or raise messages containing credentials.
    """

    #: True only for fakes. Shown on the dashboard so a simulation can never be
    #: mistaken for a real broker session.
    simulated: bool = False

    def __init__(self, settings: BrokerSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    @abstractmethod
    async def connect(self) -> None:
        """Establish a DEMO session or raise."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the session. Must be safe to call repeatedly."""

    @abstractmethod
    async def is_connected(self) -> bool:
        """Verify, against the broker, that the session is alive."""

    @abstractmethod
    async def get_account_mode(self) -> str:
        """Return the active account type, e.g. "DEMO" or "REAL"."""

    async def ping(self) -> Optional[float]:
        """Round-trip latency in milliseconds, or None when unknown."""
        return None


class UnsupportedAdapter(BrokerAdapter):
    """Default adapter: no authorized integration configured."""

    def __init__(
        self,
        settings: BrokerSettings,
        logger: logging.Logger,
        reason: str = "No authorized integration is configured.",
    ) -> None:
        super().__init__(settings, logger)
        self.reason = reason

    async def connect(self) -> None:
        raise IntegrationUnsupportedError(self.reason)

    async def disconnect(self) -> None:
        return None

    async def is_connected(self) -> bool:
        return False

    async def get_account_mode(self) -> str:
        return "UNKNOWN"


class SimulatedAdapter(BrokerAdapter):
    """Deterministic in-memory fake. Never touches the network."""

    simulated = True

    def __init__(
        self,
        settings: BrokerSettings,
        logger: logging.Logger,
        *,
        latency_ms: float = 35.0,
        connect_delay: float = 0.05,
        fail_connects: int = 0,
        drop_after: Optional[float] = None,
        mode: str = "DEMO",
        hang_connect: bool = False,
        hang_health: bool = False,
    ) -> None:
        super().__init__(settings, logger)
        self.latency_ms = latency_ms
        self.connect_delay = connect_delay
        self.fail_connects = fail_connects  # first N connect() calls fail
        self.drop_after = drop_after  # seconds until the fake link drops
        self.mode = mode
        self.hang_connect = hang_connect
        self.hang_health = hang_health  # tests may flip this at runtime
        self.connect_calls = 0
        self.disconnect_calls = 0
        self._connected = False
        self._connected_at = 0.0

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.hang_connect:
            await asyncio.sleep(3600)
        await asyncio.sleep(self.connect_delay)
        if self.connect_calls <= self.fail_connects:
            raise ConnectionError("Simulated connection failure")
        self._connected = True
        self._connected_at = time.monotonic()

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    async def is_connected(self) -> bool:
        if self.hang_health:
            await asyncio.sleep(3600)
        if (
            self._connected
            and self.drop_after is not None
            and time.monotonic() - self._connected_at >= self.drop_after
        ):
            self._connected = False  # simulated network drop
        return self._connected

    async def get_account_mode(self) -> str:
        return self.mode

    async def ping(self) -> Optional[float]:
        return self.latency_ms if self._connected else None


def load_adapter(spec: str, settings: BrokerSettings, logger: logging.Logger) -> BrokerAdapter:
    """Instantiate ``"package.module:ClassName"`` and validate that it is an adapter."""
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise AdapterLoadError(f"Adapter must be 'package.module:ClassName', got {spec!r}")
    try:
        cls = getattr(importlib.import_module(module_name), attr)
    except Exception as exc:
        raise AdapterLoadError(
            f"Could not load adapter {spec!r}: {type(exc).__name__}"
        ) from None
    if not (isinstance(cls, type) and issubclass(cls, BrokerAdapter)):
        raise AdapterLoadError(f"{spec!r} is not a BrokerAdapter subclass")
    return cls(settings, logger)

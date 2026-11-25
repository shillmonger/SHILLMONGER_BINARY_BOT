"""Broker-agnostic connection lifecycle: connect, verify, monitor, reconnect.

Each broker gets its OWN ``BaseConnector`` subclass instance with its own settings,
logger, state and asyncio task. Nothing is shared between instances.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Optional

from .adapters import BrokerAdapter, UnsupportedAdapter, load_adapter
from .config import DEMO_MODES, BrokerSettings
from .logger import Redactor, build_logger, close_logger
from .models import (
    BrokerError,
    BrokerStatus,
    ConnectionState,
    ConnectionTimeoutError,
    HealthResult,
    IntegrationUnsupportedError,
    PolicyViolationError,
)

AdapterFactory = Callable[[BrokerSettings, logging.Logger], BrokerAdapter]


def normalize_mode(raw: Optional[str]) -> str:
    value = (raw or "").strip().upper()
    if value in DEMO_MODES:
        return "DEMO"
    return value or "UNKNOWN"


async def run_health_check(adapter: BrokerAdapter, *, timeout: float) -> HealthResult:
    """Verify a session against the adapter. Never raises (except cancellation)."""

    async def probe() -> HealthResult:
        if not await adapter.is_connected():
            return HealthResult(ok=False, error="Broker reports no active session")
        mode = normalize_mode(await adapter.get_account_mode())
        if mode != "DEMO":
            return HealthResult(
                ok=False,
                fatal=True,
                account_mode=mode,
                error=f"Account mode {mode!r} is not permitted (DEMO only)",
            )
        latency = await adapter.ping()
        return HealthResult(ok=True, latency_ms=latency, account_mode=mode)

    try:
        return await asyncio.wait_for(probe(), timeout=timeout)
    except asyncio.TimeoutError:
        return HealthResult(
            ok=False, timed_out=True, error=f"Health check timed out after {timeout:g}s"
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return HealthResult(ok=False, error=f"{type(exc).__name__}: {exc}")


class BaseConnector:
    """Lifecycle + monitoring for one broker. Subclasses only supply identity."""

    LABEL = "BROKER"  # shown on the dashboard
    KEY = "broker"  # stable lookup key
    LOG_FILE = "broker.log"
    UNSUPPORTED_REASON = "No authorized integration is configured."

    def __init__(
        self,
        settings: BrokerSettings,
        logger: logging.Logger,
        redactor: Redactor,
        *,
        adapter_factory: Optional[AdapterFactory] = None,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self._redact = redactor
        self._adapter_factory = adapter_factory
        self._adapter: Optional[BrokerAdapter] = None
        self._task: Optional["asyncio.Task[None]"] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._state = ConnectionState.DISCONNECTED
        self._detail: Optional[str] = None
        self._last_check: Optional[datetime] = None
        self._latency_ms: Optional[float] = None
        self._verified_mode: Optional[str] = None
        self._connected_since: Optional[datetime] = None
        self._attempts = 0
        self._ever_connected = False

    # ------------------------------------------------------------------ hooks
    @classmethod
    def read_settings(cls, env: Mapping[str, str]) -> BrokerSettings:
        raise NotImplementedError

    async def check_health(self, adapter: BrokerAdapter) -> HealthResult:
        return await run_health_check(adapter, timeout=self.settings.health_timeout)

    def default_adapter(self) -> BrokerAdapter:
        return UnsupportedAdapter(self.settings, self.logger, self.UNSUPPORTED_REASON)

    # ------------------------------------------------------------ construction
    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        log_dir: Path,
        log_level: str = "INFO",
        adapter_factory: Optional[AdapterFactory] = None,
    ) -> "BaseConnector":
        settings = cls.read_settings(env)
        redactor = Redactor([settings.email.reveal(), settings.password.reveal()])
        logger = build_logger(
            cls.KEY.upper(), Path(log_dir) / cls.LOG_FILE, level=log_level, redactor=redactor
        )
        return cls(settings, logger, redactor, adapter_factory=adapter_factory)

    # ------------------------------------------------------------ public state
    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def is_monitoring(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> BrokerStatus:
        adapter = self._adapter
        return BrokerStatus(
            broker=self.LABEL,
            state=self._state,
            configured_mode=self.settings.account_mode,
            account_mode=self._verified_mode if self._state.is_up else None,
            last_check=self._last_check,
            latency_ms=self._latency_ms if self._state.is_up else None,
            detail=self._detail,
            simulated=bool(getattr(adapter, "simulated", False)),
            connection_attempts=self._attempts,
            connected_since=self._connected_since if self._state.is_up else None,
        )

    def describe_error(self, exc: BaseException) -> str:
        """Credential-safe one-line description of an exception."""
        text = str(exc).strip()
        return self._redact(f"{type(exc).__name__}: {text}" if text else type(exc).__name__)

    def mark_error(self, message: str) -> None:
        self._set_state(ConnectionState.ERROR, message)

    def close_logging(self) -> None:
        close_logger(self.logger)

    # --------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        """Begin connecting and monitoring in the background. Returns immediately."""
        if self.is_monitoring:
            return
        self._stop_event = asyncio.Event()
        s = self.settings
        self.logger.info("Starting connector (account mode: %s)", s.account_mode)
        if s.config_error:
            self.logger.error("Configuration rejected: %s", s.config_error)
            self._set_state(ConnectionState.ERROR, f"Configuration error: {s.config_error}")
            return
        if not s.enabled:
            self._set_state(ConnectionState.DISCONNECTED, "Disabled by configuration")
            return
        try:
            if self._adapter_factory is not None:
                self._adapter = self._adapter_factory(s, self.logger)
            elif s.adapter:
                self._adapter = load_adapter(s.adapter, s, self.logger)
            else:
                self._adapter = self.default_adapter()
        except Exception as exc:
            self._set_state(
                ConnectionState.ERROR, f"Adapter could not be created: {self.describe_error(exc)}"
            )
            return
        self._task = asyncio.create_task(self._run(), name=f"{self.KEY}-connector")

    async def stop(self) -> None:
        """Stop monitoring and close the session. Safe to call repeatedly."""
        self.logger.info("Shutdown requested")
        if self._stop_event is not None:
            self._stop_event.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover - supervisor already guards
                self.logger.error("Connector task ended with %s", self.describe_error(exc))
        await self._safe_disconnect()
        self._connected_since = None
        if self._state not in (ConnectionState.UNSUPPORTED, ConnectionState.ERROR):
            self._set_state(ConnectionState.DISCONNECTED, "Stopped")
        self.logger.info("Shutdown complete")

    # ------------------------------------------------------------- supervision
    async def _run(self) -> None:
        try:
            await self._supervise()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # last line of defence: never crash the app
            self.logger.error("Unexpected supervisor error: %s", self.describe_error(exc))
            self._set_state(ConnectionState.ERROR, f"Unexpected error: {self.describe_error(exc)}")

    async def _supervise(self) -> None:
        s = self.settings
        failures = 0
        while not self._stop_event.is_set():
            self._attempts += 1
            state = (
                ConnectionState.RECONNECTING
                if (self._ever_connected or failures)
                else ConnectionState.CONNECTING
            )
            self._set_state(state, None)
            self.logger.info("Connection attempt #%d (%s)", self._attempts, state.value)
            try:
                await self._connect_and_verify()
                failures = 0
                await self._monitor()
            except asyncio.CancelledError:
                raise
            except IntegrationUnsupportedError as exc:
                self._last_check = datetime.now()
                self.logger.warning("Unsupported: %s", self._redact(str(exc)))
                self._set_state(ConnectionState.UNSUPPORTED, str(exc))
                return
            except PolicyViolationError as exc:
                self.logger.error("Safety stop: %s", self._redact(str(exc)))
                await self._safe_disconnect()
                self._set_state(ConnectionState.ERROR, str(exc))
                return
            except Exception as exc:
                failures += 1
                message = self.describe_error(exc)
                self.logger.error("Connection attempt failed: %s", message)
                await self._safe_disconnect()
                self._set_state(ConnectionState.DISCONNECTED, message)
            if self._stop_event.is_set():
                break
            if s.max_reconnect_attempts and failures >= s.max_reconnect_attempts:
                last = self._detail
                self.logger.error("Giving up after %d failed attempts", failures)
                self._set_state(
                    ConnectionState.ERROR,
                    f"Giving up after {failures} failed attempts. Last error: {last}",
                )
                return
            delay = min(s.reconnect_max_delay, s.reconnect_base_delay * (2 ** max(failures - 1, 0)))
            self.logger.info("Reconnection scheduled in %.2fs", delay)
            await self._sleep(delay)

    async def _connect_and_verify(self) -> None:
        adapter, s = self._adapter, self.settings
        try:
            await asyncio.wait_for(adapter.connect(), timeout=s.connect_timeout)
        except asyncio.TimeoutError:
            raise ConnectionTimeoutError(
                f"Connection timed out after {s.connect_timeout:g}s"
            ) from None
        # A returned connect() proves nothing: verify the session explicitly.
        result = await self.check_health(adapter)
        self._record_check(result)
        if result.fatal:
            raise PolicyViolationError(result.error or "Safety violation")
        if not result.ok:
            raise BrokerError(f"Connection could not be verified: {result.error}")
        self._ever_connected = True
        self._connected_since = datetime.now()
        self.logger.info("Connection verified (mode=%s)", result.account_mode)
        self._apply_healthy(result)

    async def _monitor(self) -> None:
        s = self.settings
        consecutive = 0
        while not self._stop_event.is_set():
            await self._sleep(s.health_interval)
            if self._stop_event.is_set():
                return
            result = await self.check_health(self._adapter)
            self._record_check(result)
            if result.ok:
                consecutive = 0
                self.logger.info(
                    "Health check OK (latency=%s)",
                    f"{result.latency_ms:.0f} ms" if result.latency_ms is not None else "unknown",
                )
                self._apply_healthy(result)
            elif result.fatal:
                raise PolicyViolationError(result.error or "Safety violation")
            else:
                consecutive += 1
                message = self._redact(result.error or "unknown error")
                self.logger.warning(
                    "Health check failed (%d/%d): %s",
                    consecutive,
                    s.failures_before_disconnect,
                    message,
                )
                if consecutive >= s.failures_before_disconnect:
                    self.logger.warning("Disconnection detected")
                    await self._safe_disconnect()
                    self._connected_since = None
                    self._set_state(ConnectionState.DISCONNECTED, f"Connection lost: {message}")
                    return
                self._set_state(
                    ConnectionState.DEGRADED,
                    f"Health check failed ({consecutive}/{s.failures_before_disconnect}): {message}",
                )

    # ----------------------------------------------------------------- helpers
    def _record_check(self, result: HealthResult) -> None:
        self._last_check = datetime.now()
        self._latency_ms = result.latency_ms if result.ok else None

    def _apply_healthy(self, result: HealthResult) -> None:
        self._verified_mode = result.account_mode
        slow = (
            result.latency_ms is not None
            and result.latency_ms > self.settings.degraded_latency_ms
        )
        if slow:
            self._set_state(ConnectionState.DEGRADED, f"High latency ({result.latency_ms:.0f} ms)")
        else:
            self._set_state(ConnectionState.CONNECTED, None)

    def _set_state(self, state: ConnectionState, detail: Optional[str]) -> None:
        previous = self._state
        self._state = state
        self._detail = self._redact(detail) if detail else None
        if previous != state:
            log = self.logger.error if state is ConnectionState.ERROR else self.logger.info
            log("State %s -> %s%s", previous.value, state.value, f" ({self._detail})" if self._detail else "")

    async def _sleep(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _safe_disconnect(self) -> None:
        adapter = self._adapter
        if adapter is None:
            return
        try:
            await asyncio.wait_for(adapter.disconnect(), timeout=min(5.0, self.settings.connect_timeout))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning("Disconnect raised %s", self.describe_error(exc))

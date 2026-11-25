"""Shared helpers for the tests.

ALL tests in this folder are SIMULATED: they use ``SimulatedAdapter`` or small fake
adapters. They need no credentials, no network, and they cannot place trades
(this version contains no trading code at all). Nothing here talks to a real broker.
"""

import _bootstrap  # noqa: F401  (must be first)

import asyncio
import shutil
import tempfile
import time
from pathlib import Path

from BINARY_TRADING_BOTS.common.adapters import BrokerAdapter, SimulatedAdapter
from BINARY_TRADING_BOTS.common.models import ConnectionState as S

PASSWORD = "S3cr3t-Pa55-XYZ"  # fake value used to prove it never leaks
EMAIL = "trader@example.invalid"


def fast_env(prefix, **overrides):
    """Environment with tiny timers so tests finish quickly."""
    env = {
        f"{prefix}_EMAIL": EMAIL,
        f"{prefix}_PASSWORD": PASSWORD,
        f"{prefix}_CONNECT_TIMEOUT": "0.3",
        f"{prefix}_HEALTH_INTERVAL": "0.05",
        f"{prefix}_HEALTH_TIMEOUT": "0.2",
        f"{prefix}_FAILURES_BEFORE_DISCONNECT": "2",
        f"{prefix}_MAX_RECONNECT_ATTEMPTS": "3",
        f"{prefix}_RECONNECT_BASE_DELAY": "0.02",
        f"{prefix}_RECONNECT_MAX_DELAY": "0.05",
    }
    env.update({f"{prefix}_{k}": v for k, v in overrides.items()})
    return env


async def wait_until(predicate, timeout=4.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def sim_factory(created, **kwargs):
    """Adapter factory that records every SimulatedAdapter it creates."""

    def factory(settings, logger):
        adapter = SimulatedAdapter(settings, logger, **kwargs)
        created.append(adapter)
        return adapter

    return factory


class LeakyAdapter(BrokerAdapter):
    """Raises errors that contain credentials, to prove they are redacted."""

    async def connect(self):
        raise RuntimeError(f"login failed password={PASSWORD} for {EMAIL}")

    async def disconnect(self):
        return None

    async def is_connected(self):
        return False

    async def get_account_mode(self):
        return "UNKNOWN"


class CrashingAdapter(BrokerAdapter):
    """Raises an unusual exception type from every call."""

    async def connect(self):
        raise KeyError("boom")

    async def disconnect(self):
        raise OSError("disconnect exploded")

    async def is_connected(self):
        raise KeyError("boom")

    async def get_account_mode(self):
        raise KeyError("boom")


class ConnectorContract:
    """Behaviour every broker connector must satisfy. Subclass together with
    ``unittest.IsolatedAsyncioTestCase`` and set the four class attributes."""

    CONNECTOR = None
    PREFIX = ""
    LABEL = ""
    LOG_FILE = ""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="broker_test_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def make(self, factory=None, **overrides):
        connector = self.CONNECTOR.from_env(
            fast_env(self.PREFIX, **overrides),
            log_dir=self._tmp,
            log_level="DEBUG",
            adapter_factory=factory,
        )

        async def shutdown():
            await connector.stop()
            connector.close_logging()

        self.addAsyncCleanup(shutdown)
        return connector

    def read_log(self):
        return (Path(self._tmp) / self.LOG_FILE).read_text(encoding="utf-8")

    # ------------------------------------------------------------ initialization
    async def test_initialization_is_idle_and_disconnected(self):
        c = self.make()
        status = c.status()
        self.assertEqual(c.LABEL, self.LABEL)
        self.assertEqual(status.state, S.DISCONNECTED)
        self.assertFalse(c.is_monitoring)
        self.assertIsNone(status.last_check)
        self.assertIsNone(status.latency_ms)
        self.assertEqual(status.configured_mode, "DEMO")

    async def test_default_adapter_reports_unsupported_and_never_connected(self):
        c = self.make()
        await c.start()
        self.assertTrue(await wait_until(lambda: c.state is S.UNSUPPORTED))
        status = c.status()
        self.assertFalse(status.simulated)
        self.assertIsNone(status.account_mode)  # nothing verified
        self.assertIn("ADAPTER", status.detail.upper())
        self.assertTrue(await wait_until(lambda: not c.is_monitoring))
        self.assertIsNot(c.state, S.CONNECTED)

    async def test_disabled_connector_does_nothing(self):
        created = []
        c = self.make(sim_factory(created), ENABLED="false")
        await c.start()
        self.assertEqual(c.state, S.DISCONNECTED)
        self.assertIn("Disabled", c.status().detail)
        self.assertFalse(c.is_monitoring)
        self.assertEqual(created, [])

    # --------------------------------------------------- verified status reporting
    async def test_connection_is_verified_before_reporting_connected(self):
        created = []
        c = self.make(sim_factory(created, latency_ms=35.0))
        await c.start()
        self.assertTrue(await wait_until(lambda: c.state is S.CONNECTED))
        status = c.status()
        self.assertEqual(status.account_mode, "DEMO")
        self.assertEqual(status.latency_ms, 35.0)
        self.assertTrue(status.simulated)  # a simulation is always labelled
        self.assertIsNotNone(status.last_check)
        self.assertTrue(
            await wait_until(lambda: "Health check OK" in self.read_log()),
            "no successful health check was logged",
        )

    async def test_high_latency_is_reported_as_degraded(self):
        created = []
        c = self.make(sim_factory(created, latency_ms=900.0), DEGRADED_LATENCY_MS="500")
        await c.start()
        self.assertTrue(await wait_until(lambda: c.state is S.DEGRADED))
        self.assertIn("latency", c.status().detail.lower())

    # --------------------------------------------------------- failure handling
    async def test_connection_failure_retries_then_recovers(self):
        created = []
        c = self.make(sim_factory(created, fail_connects=1))
        await c.start()
        self.assertTrue(await wait_until(lambda: c.state is S.CONNECTED))
        self.assertEqual(created[0].connect_calls, 2)
        self.assertGreaterEqual(c.status().connection_attempts, 2)
        self.assertIn("Simulated connection failure", self.read_log())

    async def test_gives_up_with_error_after_max_attempts(self):
        created = []
        c = self.make(sim_factory(created, fail_connects=99), MAX_RECONNECT_ATTEMPTS="2")
        await c.start()
        self.assertTrue(await wait_until(lambda: c.state is S.ERROR))
        self.assertEqual(created[0].connect_calls, 2)
        self.assertIn("Giving up after 2", c.status().detail)
        self.assertTrue(await wait_until(lambda: not c.is_monitoring))

    async def test_connection_timeout_is_reported(self):
        created = []
        c = self.make(sim_factory(created, hang_connect=True), CONNECT_TIMEOUT="0.1", MAX_RECONNECT_ATTEMPTS="1")
        await c.start()
        self.assertTrue(await wait_until(lambda: c.state is S.ERROR))
        self.assertIn("timed out", c.status().detail)

    async def test_health_check_timeout_is_detected(self):
        created = []
        c = self.make(sim_factory(created))
        await c.start()
        self.assertTrue(await wait_until(lambda: c.state is S.CONNECTED))
        created[0].hang_health = True  # broker stops answering
        self.assertTrue(await wait_until(lambda: c.state is not S.CONNECTED))
        self.assertTrue(await wait_until(lambda: "timed out" in self.read_log()))

    async def test_unexpected_adapter_exceptions_do_not_escape(self):
        c = self.make(lambda s, l: CrashingAdapter(s, l), MAX_RECONNECT_ATTEMPTS="2")
        await c.start()
        self.assertTrue(await wait_until(lambda: c.state is S.ERROR))
        self.assertIn("KeyError", c.status().detail)
        await c.stop()  # also survives an adapter whose disconnect() raises

    # ------------------------------------------------------------- reconnection
    async def test_disconnect_is_detected_and_reconnected(self):
        created = []
        c = self.make(sim_factory(created, drop_after=0.15))
        await c.start()
        self.assertTrue(await wait_until(lambda: created and created[0].connect_calls >= 2 and c.state is S.CONNECTED))
        log = self.read_log()
        self.assertIn("Disconnection detected", log)
        self.assertIn("RECONNECTING", log)

    # ------------------------------------------------------------ safety / DEMO
    async def test_live_mode_configuration_is_refused_without_connecting(self):
        created = []
        c = self.make(sim_factory(created), ACCOUNT_MODE="LIVE")
        await c.start()
        self.assertEqual(c.state, S.ERROR)
        self.assertIn("not permitted", c.status().detail)
        self.assertEqual(created, [])
        self.assertFalse(c.is_monitoring)

    async def test_non_demo_account_reported_by_adapter_is_rejected(self):
        created = []
        c = self.make(sim_factory(created, mode="REAL"))
        await c.start()
        self.assertTrue(await wait_until(lambda: c.state is S.ERROR))
        self.assertIn("not permitted", c.status().detail)
        self.assertGreaterEqual(created[0].disconnect_calls, 1)
        self.assertEqual(created[0].connect_calls, 1)  # no retry on a safety stop

    # ---------------------------------------------------------------- secrets
    async def test_credentials_never_reach_logs_or_status(self):
        c = self.make(lambda s, l: LeakyAdapter(s, l), MAX_RECONNECT_ATTEMPTS="2")
        await c.start()
        self.assertTrue(await wait_until(lambda: c.state is S.ERROR))
        await c.stop()
        for text in (self.read_log(), c.status().detail, repr(c.settings), str(c.settings)):
            self.assertNotIn(PASSWORD, text)
            self.assertNotIn(EMAIL, text)

    # ---------------------------------------------------------------- shutdown
    async def test_graceful_stop_disconnects_and_is_repeatable(self):
        created = []
        c = self.make(sim_factory(created))
        await c.start()
        self.assertTrue(await wait_until(lambda: c.state is S.CONNECTED))
        await c.stop()
        self.assertEqual(c.state, S.DISCONNECTED)
        self.assertFalse(c.is_monitoring)
        self.assertGreaterEqual(created[0].disconnect_calls, 1)
        await c.stop()  # idempotent
        log = self.read_log()
        self.assertIn("Shutdown requested", log)
        self.assertIn("Shutdown complete", log)

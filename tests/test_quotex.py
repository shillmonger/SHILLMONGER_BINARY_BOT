"""Quotex connector tests - SIMULATED only (no credentials, no network, no trades)."""

import _bootstrap  # noqa: F401
import unittest
from pathlib import Path

from _support import ConnectorContract, fast_env
from BINARY_TRADING_BOTS.quotex.connector import QuotexConnector


class TestQuotexConnector(ConnectorContract, unittest.IsolatedAsyncioTestCase):
    CONNECTOR = QuotexConnector
    PREFIX = "QUOTEX"
    LABEL = "QUOTEX"
    LOG_FILE = "quotex.log"

    async def test_reads_only_its_own_environment_variables(self):
        env = fast_env("IQ_OPTION", ENABLED="false", ACCOUNT_MODE="LIVE")
        settings = QuotexConnector.read_settings(env)
        self.assertTrue(settings.enabled)
        self.assertIsNone(settings.config_error)

    async def test_writes_to_its_own_log_file_only(self):
        c = self.make()
        await c.start()
        self.assertIn("QUOTEX", self.read_log())
        self.assertFalse((Path(self._tmp) / "iq_option.log").exists())


if __name__ == "__main__":
    unittest.main()

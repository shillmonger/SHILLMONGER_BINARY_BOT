"""IQ Option connector tests - SIMULATED only (no credentials, no network, no trades)."""

import _bootstrap  # noqa: F401
import unittest

from _support import ConnectorContract, fast_env
from BINARY_TRADING_BOTS.iq_option.connector import IQOptionConnector


class TestIQOptionConnector(ConnectorContract, unittest.IsolatedAsyncioTestCase):
    CONNECTOR = IQOptionConnector
    PREFIX = "IQ_OPTION"
    LABEL = "IQ OPTION"
    LOG_FILE = "iq_option.log"

    async def test_reads_only_its_own_environment_variables(self):
        env = fast_env("QUOTEX", ENABLED="false", ACCOUNT_MODE="LIVE")
        settings = IQOptionConnector.read_settings(env)
        self.assertTrue(settings.enabled)
        self.assertIsNone(settings.config_error)

    async def test_writes_to_its_own_log_file_only(self):
        c = self.make()
        await c.start()
        self.assertIn("IQ_OPTION", self.read_log())
        self.assertFalse((__import__("pathlib").Path(self._tmp) / "quotex.log").exists())


if __name__ == "__main__":
    unittest.main()

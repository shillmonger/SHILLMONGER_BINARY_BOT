"""Configuration and redaction tests (pure unit tests, no I/O beyond temp files)."""

import _bootstrap  # noqa: F401
import tempfile
import unittest
from pathlib import Path

from BINARY_TRADING_BOTS.common.config import (
    SecretStr,
    build_environment,
    parse_env_file,
    read_broker_settings,
    read_manager_settings,
)
from BINARY_TRADING_BOTS.common.logger import Redactor


class TestConfig(unittest.TestCase):
    def test_secret_str_never_prints_value(self):
        s = SecretStr("hunter2")
        self.assertNotIn("hunter2", repr(s))
        self.assertNotIn("hunter2", str(s))
        self.assertEqual(s.reveal(), "hunter2")

    def test_defaults_are_safe(self):
        s = read_broker_settings({}, "QUOTEX")
        self.assertEqual(s.account_mode, "DEMO")
        self.assertIsNone(s.config_error)
        self.assertEqual(s.adapter, "")  # => UNSUPPORTED placeholder
        self.assertFalse(s.password)

    def test_live_mode_is_blocked_not_silently_accepted(self):
        s = read_broker_settings({"QUOTEX_ACCOUNT_MODE": "REAL"}, "QUOTEX")
        self.assertIn("not permitted", s.config_error)
        self.assertEqual(s.account_mode, "DEMO")

    def test_invalid_numbers_are_reported_per_broker(self):
        s = read_broker_settings({"IQ_OPTION_CONNECT_TIMEOUT": "abc", "IQ_OPTION_ENABLED": "maybe"}, "IQ_OPTION")
        self.assertIn("IQ_OPTION_CONNECT_TIMEOUT", s.config_error)
        self.assertIn("IQ_OPTION_ENABLED", s.config_error)

    def test_env_file_parsing_and_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# comment\nA=1\nexport B='two words'\nC=3 # inline\nD=\"q\"\nbad line\n", encoding="utf-8"
            )
            self.assertEqual(parse_env_file(path), {"A": "1", "B": "two words", "C": "3", "D": "q"})
            merged = build_environment(path, {"A": "override"})
            self.assertEqual(merged["A"], "override")
            self.assertEqual(merged["C"], "3")
        self.assertEqual(parse_env_file(Path("does-not-exist.env")), {})

    def test_manager_settings_fall_back_on_bad_values(self):
        m = read_manager_settings({"BROKER_LOG_LEVEL": "LOUD", "BROKER_DASHBOARD_REFRESH": "0"})
        self.assertEqual(m.log_level, "INFO")
        self.assertEqual(m.refresh_interval, 1.0)
        self.assertEqual(len(m.errors), 2)


class TestRedactor(unittest.TestCase):
    def test_masks_known_secrets_and_patterns(self):
        r = Redactor(["topsecret"])
        self.assertNotIn("topsecret", r("login with topsecret failed"))
        self.assertEqual(r("password=abc123 ok"), "password=*** ok")
        self.assertNotIn("abc.def", r("Authorization: Bearer abc.def"))
        self.assertNotIn("zzz", r("session_id: zzz"))


if __name__ == "__main__":
    unittest.main()

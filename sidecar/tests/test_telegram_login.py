"""Tests for the local QR-login helper without contacting Telegram."""

import asyncio
import sys
from pathlib import Path

import pytest
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import telegram_login  # noqa: E402


class FakeQRLogin:
    def __init__(self, *, expire_once=False, require_password=False):
        self.url = "tg://login?token=first"
        self.expire_once = expire_once
        self.require_password = require_password
        self.wait_calls = 0
        self.recreate_calls = 0

    async def wait(self):
        self.wait_calls += 1
        if self.expire_once and self.wait_calls == 1:
            raise asyncio.TimeoutError
        if self.require_password:
            raise SessionPasswordNeededError(request=None)

    async def recreate(self):
        self.recreate_calls += 1
        self.url = "tg://login?token=refreshed"


class FakeClient:
    def __init__(self, qr_login):
        self._qr_login = qr_login
        self.passwords = []

    async def qr_login(self):
        return self._qr_login

    async def sign_in(self, *, password):
        self.passwords.append(password)
        if len(self.passwords) == 1:
            raise PasswordHashInvalidError(request=None)


async def test_login_with_qr_refreshes_expired_qr(monkeypatch, capsys):
    qr_login = FakeQRLogin(expire_once=True)
    client = FakeClient(qr_login)
    rendered = []
    monkeypatch.setattr(telegram_login, "display_qr", rendered.append)

    await telegram_login.login_with_qr(client)

    assert qr_login.recreate_calls == 1
    assert qr_login.wait_calls == 2
    assert rendered == ["tg://login?token=first", "tg://login?token=refreshed"]
    assert "QR expired" in capsys.readouterr().out


async def test_login_with_qr_handles_two_step_password(monkeypatch):
    qr_login = FakeQRLogin(require_password=True)
    client = FakeClient(qr_login)
    monkeypatch.setattr(telegram_login, "display_qr", lambda _url: None)
    monkeypatch.setattr(telegram_login.getpass, "getpass", lambda _prompt: "local-test-password")

    await telegram_login.login_with_qr(client)

    assert client.passwords == ["local-test-password", "local-test-password"]


def test_save_session_string_updates_empty_entry_without_printing(tmp_path, capsys):
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"KEEP_THIS=value\r\nTELEGRAM_SESSION_STRING=\r\n")

    telegram_login.save_session_string(env_path, "test-session-string")

    contents = env_path.read_bytes()
    assert b"KEEP_THIS=value\r\n" in contents
    assert b"TELEGRAM_SESSION_STRING=test-session-string\r\n" in contents
    assert capsys.readouterr().out == ""


def test_save_session_string_refuses_to_overwrite_existing_session(tmp_path):
    env_path = tmp_path / ".env"
    original = "TELEGRAM_SESSION_STRING=existing-session\n"
    env_path.write_text(original, encoding="utf-8")

    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        telegram_login.save_session_string(env_path, "new-session")

    assert env_path.read_text(encoding="utf-8") == original

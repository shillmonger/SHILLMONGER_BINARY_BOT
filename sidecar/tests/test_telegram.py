"""Tests for the sidecar Telegram service — Telethon client is faked."""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from broker_sidecar import telegram_service as tgmod
from broker_sidecar.telegram_service import TelegramManager


def make_channel(**kw):
    from telethon.tl.types import Channel

    defaults = dict(
        id=kw.pop("id", 123),
        title=kw.pop("title", "Signals Group"),
        photo=None,
        date=datetime.datetime.now(),
        access_hash=0,
        broadcast=False,
        megagroup=True,
        username=kw.pop("username", "signals"),
        participants_count=kw.pop("participants_count", 100),
    )
    defaults.update(kw)
    return Channel(**defaults)


def make_chat(**kw):
    from telethon.tl.types import Chat

    defaults = dict(
        id=kw.pop("id", 55),
        title=kw.pop("title", "Basic Group"),
        photo=None,
        participants_count=kw.pop("participants_count", 7),
        date=datetime.datetime.now(),
        version=1,
    )
    defaults.update(kw)
    return Chat(**defaults)


class FakeClient:
    def __init__(self, authorized=True, dialogs=None, me=None, fail_connect=None):
        self._authorized = authorized
        self._dialogs = dialogs or []
        self._me = me or SimpleNamespace(phone="+100", username="me", first_name="A", last_name="B")
        self._fail = fail_connect
        self.disconnected = False
        self.connected = False

    async def connect(self):
        if self._fail:
            raise self._fail
        self.connected = True

    async def is_user_authorized(self):
        return self._authorized

    def is_connected(self):
        return self.connected

    async def get_me(self):
        return self._me

    async def get_dialogs(self):
        return self._dialogs

    async def disconnect(self):
        self.disconnected = True
        self.connected = False


def dialog(entity, dialog_id, name):
    return SimpleNamespace(id=dialog_id, name=name, entity=entity)


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setenv("API_ID", "123")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("PHONE_NUMBER", "+100")
    m = TelegramManager()
    yield m


async def test_status_defaults(manager):
    d = manager.to_dict()
    assert d["status"] == "disconnected"
    assert d["connected"] is False
    assert d["configured"] is True


async def test_connect_requires_session_string(manager, monkeypatch):
    monkeypatch.delenv("TELEGRAM_SESSION_STRING", raising=False)
    monkeypatch.setenv("ALLOW_INTERACTIVE_LOGIN", "false")
    result = await manager.connect()
    assert result["status"] == "auth_required"
    assert "TELEGRAM_SESSION_STRING" in (result["lastError"] or "")


async def test_connect_success(manager, monkeypatch):
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "fake-session")
    fake = FakeClient(authorized=True)
    monkeypatch.setattr(manager, "_new_client", lambda: fake)
    result = await manager.connect()
    assert result["status"] == "connected"
    assert result["phone"] == "***100"
    assert result["username"] == "me"
    # Idempotent second call does not rebuild the client
    await manager.connect()
    assert manager.client is fake


async def test_connect_unauthorized_session(manager, monkeypatch):
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "fake-session")
    fake = FakeClient(authorized=False)
    monkeypatch.setattr(manager, "_new_client", lambda: fake)
    result = await manager.connect()
    assert result["status"] == "auth_required"
    assert result["authorized"] is False
    assert result["transportConnected"] is False
    assert "transport connected" in result["lastError"]
    assert fake.disconnected is True


async def test_connect_failure_closes_candidate_client(manager, monkeypatch):
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "fake-session")
    fake = FakeClient(fail_connect=ConnectionError("offline"))
    monkeypatch.setattr(manager, "_new_client", lambda: fake)

    result = await manager.connect()

    assert result["status"] == "error"
    assert result["connected"] is False
    assert fake.disconnected is True


async def test_invalid_string_session_is_reported_without_echoing_value(manager, monkeypatch):
    from broker_sidecar.telegram_service import TelegramAuthRequired

    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "private-invalid-session")

    def invalid_session(_value):
        raise ValueError("session parse error")

    monkeypatch.setattr(tgmod, "StringSession", invalid_session)

    with pytest.raises(TelegramAuthRequired, match="TELEGRAM_SESSION_STRING is invalid"):
        manager._new_client()


async def test_get_status_refreshes_authorization(manager, monkeypatch):
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "fake-session")
    fake = FakeClient(authorized=True)
    monkeypatch.setattr(manager, "_new_client", lambda: fake)
    await manager.connect()

    fake._authorized = False
    status = await manager.get_status()

    assert status["status"] == "auth_required"
    assert status["transportConnected"] is True
    assert status["authorized"] is False
    assert "transport is connected" in status["lastError"]


async def test_auto_start_does_not_create_duplicate_clients(manager, monkeypatch):
    monkeypatch.setattr(tgmod, "tg", manager)
    monkeypatch.setenv("TELEGRAM_AUTO_START", "true")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "fake-session")
    fake = FakeClient()
    created = []

    def make_client():
        created.append(fake)
        return fake

    monkeypatch.setattr(manager, "_new_client", make_client)

    await tgmod.auto_start_if_enabled()
    await tgmod.auto_start_if_enabled()

    assert created == [fake]
    assert manager.client is fake
    assert manager.to_dict()["authorized"] is True


async def test_disconnect(manager, monkeypatch):
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "fake-session")
    fake = FakeClient()
    monkeypatch.setattr(manager, "_new_client", lambda: fake)
    await manager.connect()
    result = await manager.disconnect()
    assert result["status"] == "disconnected"
    assert fake.disconnected is True


async def test_fetch_groups(manager, monkeypatch):
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "fake-session")
    supergroup = make_channel(id=-100111, title="Gold OTC", username="goldotc", participants_count=500)
    channel = make_channel(
        id=-100333,
        title="Broadcast Channel",
        username="broadcast",
        broadcast=True,
        megagroup=False,
    )
    basic = make_chat(id=-200222, title="IQ VIP", participants_count=30)
    user_dialog = SimpleNamespace(id=999, name="Some User", entity=SimpleNamespace())
    fake = FakeClient(
        dialogs=[
            dialog(supergroup, -100111, "Gold OTC"),
            dialog(channel, -100333, "Broadcast Channel"),
            dialog(basic, -200222, "IQ VIP"),
            user_dialog,
        ]
    )
    monkeypatch.setattr(manager, "_new_client", lambda: fake)
    await manager.connect()

    result = await manager.fetch_groups()
    assert result["ok"] is True
    assert result["count"] == 3  # user dialog excluded
    by_id = {g["id"]: g for g in result["groups"]}
    assert by_id["-100111"]["type"] == "supergroup"
    assert by_id["-100111"]["username"] == "goldotc"
    assert by_id["-100111"]["participantsCount"] == 500
    assert by_id["-200222"]["type"] == "group"
    assert by_id["-100333"]["type"] == "channel"
    assert "phone" not in by_id["-100111"]
    assert manager.groups_synced_at is not None
    assert manager.to_dict()["groupCount"] == 3


async def test_fetch_groups_requires_connection(manager):
    from broker_sidecar.telegram_service import TelegramAuthRequired

    with pytest.raises(TelegramAuthRequired):
        await manager.fetch_groups()


async def test_fetch_groups_rechecks_authorization(manager, monkeypatch):
    from broker_sidecar.telegram_service import TelegramAuthRequired

    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "fake-session")
    fake = FakeClient(authorized=True)
    monkeypatch.setattr(manager, "_new_client", lambda: fake)
    await manager.connect()
    fake._authorized = False

    with pytest.raises(TelegramAuthRequired, match="no longer authorized"):
        await manager.fetch_groups()

    assert manager.status == "auth_required"


async def test_fetch_groups_flood_wait(manager, monkeypatch):
    from telethon.errors import FloodWaitError

    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "fake-session")

    class FloodClient(FakeClient):
        async def get_dialogs(self):
            raise FloodWaitError(request=None, capture=0)

    # FloodWaitError needs seconds — construct via __new__ path used by telethon
    err = FloodWaitError.__new__(FloodWaitError)
    Exception.__init__(err)
    err.seconds = 42

    class FloodClient2(FakeClient):
        async def get_dialogs(self):
            raise err

    fake = FloodClient2()
    monkeypatch.setattr(manager, "_new_client", lambda: fake)
    await manager.connect()

    from broker_sidecar.telegram_service import TelegramFloodWait

    with pytest.raises(TelegramFloodWait) as excinfo:
        await manager.fetch_groups()
    assert excinfo.value.wait_seconds == 42.0


async def test_auto_start_disabled(manager, monkeypatch):
    monkeypatch.setenv("TELEGRAM_AUTO_START", "false")
    await tgmod.auto_start_if_enabled()
    assert manager is not tgmod.tg  # fixture manager untouched; global unchanged

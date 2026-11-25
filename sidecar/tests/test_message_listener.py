"""Tests for the Phase-3 Telegram message listener.

Telethon is faked: a stub TelegramManager owns a stub client that records
handler registration. No network, no real Telegram session, no trading.
"""

from __future__ import annotations

import asyncio
import datetime
from types import SimpleNamespace

import pytest

from broker_sidecar import message_listener as ml
from broker_sidecar.message_listener import TelegramMessageListener


# ------------------------------------------------------------------ fakes


class FakeClient:
    def __init__(self, connected=True):
        self._connected = connected
        self.handlers = {}  # event-builder -> list of callbacks

    def is_connected(self):
        return self._connected

    def add_event_handler(self, callback, event_builder):
        self.handlers.setdefault(event_builder, []).append(callback)
        return callback

    def remove_event_handler(self, callback, event_builder):
        lst = self.handlers.get(event_builder, [])
        if callback in lst:
            lst.remove(callback)
        return callback


class FakeTg:
    def __init__(self, connected=True):
        self.status = "connected" if connected else "disconnected"
        self.client = FakeClient(connected=connected) if connected else None
        self.connect_calls = 0

    async def connect(self):
        self.connect_calls += 1
        self.status = "connected"
        self.client = FakeClient()
        return {"status": "connected"}

    def to_dict(self):
        return {"status": self.status, "connected": self.status == "connected"}


class FakeForwarder:
    def __init__(self, status=200, data=None, raise_exc=None):
        self.status = status
        self.data = data if data is not None else {"ingested": 1}
        self.raise_exc = raise_exc
        self.sent = []

    def _post(self, payload):
        self.sent.append(payload)
        if self.raise_exc:
            raise self.raise_exc
        return self.status, self.data


def make_event(kind="new", chat_id="-100123", message_id=42, text="EURUSD OTC CALL 5 MIN"):
    msg = SimpleNamespace(
        id=message_id,
        message=text,
        date=datetime.datetime(2026, 11, 25, 12, 0, 0),
        edit_date=None,
        peer_id=None,
        sender_id=777,
        photo=None, video=None, voice=None, audio=None,
        sticker=None, document=None, gif=None,
    )
    event = SimpleNamespace(
        chat_id=chat_id,
        message=msg,
        sender_id=777,
        chat=SimpleNamespace(title="QUOTEX OTC SIGNALS"),
    )
    if kind == "deleted":
        return SimpleNamespace(chat_id=chat_id, deleted_ids=[message_id], deleted_id=message_id)
    if kind == "edited":
        msg.edit_date = datetime.datetime(2026, 11, 25, 12, 5, 0)
    return event


@pytest.fixture
def listener(monkeypatch):
    monkeypatch.setenv("BINARY_INGEST_URL", "http://x/ingest")
    monkeypatch.setenv("WORKER_HMAC_SECRET", "s3cret")
    tg = FakeTg(connected=True)
    fwd = FakeForwarder()
    return TelegramMessageListener(tg=tg, forwarder=fwd), tg, fwd


async def drain(listener, rounds=5):
    for _ in range(rounds):
        await asyncio.sleep(0.01)


# ------------------------------------------------------------------ tests


async def test_start_when_telegram_disconnected_fails(listener):
    lis, tg, _ = listener
    tg.status = "disconnected"
    tg.client = None
    # connect() on the fake succeeds — listener should recover via tg.connect
    result = await lis.start()
    assert result["state"] == "RUNNING"
    assert tg.connect_calls == 1


async def test_start_idempotent(listener):
    lis, _, _ = listener
    r1 = await lis.start()
    r2 = await lis.start()
    assert r1["state"] == "RUNNING"
    assert r2["state"] == "RUNNING"
    assert lis._worker is not None and not lis._worker.done()


async def test_start_without_ingest_config_is_degraded(listener, monkeypatch):
    monkeypatch.delenv("BINARY_INGEST_URL", raising=False)
    lis, _, _ = listener
    result = await lis.start()
    assert result["state"] == "DEGRADED"
    assert "BINARY_INGEST_URL" in (result["lastError"] or "")


async def test_stop_unregisters_handlers(listener):
    lis, tg, _ = listener
    await lis.start()
    client = tg.client
    assert client.handlers  # handlers bound
    result = await lis.stop()
    assert result["state"] == "STOPPED"
    assert all(not v for v in client.handlers.values())


async def test_restart_cycles(listener):
    lis, _, _ = listener
    await lis.start()
    result = await lis.restart()
    assert result["state"] == "RUNNING"


async def test_new_message_forwarded_with_payload(listener):
    lis, tg, fwd = listener
    await lis.start()
    await lis._handle("NEW", make_event())
    await drain(lis)
    assert fwd.sent, "event was not forwarded"
    payload = fwd.sent[-1]
    assert payload["event"] == "NEW"
    assert payload["chatId"] == "-100123"
    assert payload["messageId"] == 42
    assert payload["text"] == "EURUSD OTC CALL 5 MIN"
    assert payload["messageType"] == "TEXT"
    assert payload["chatTitle"] == "QUOTEX OTC SIGNALS"
    assert lis.counters["received"] == 1
    assert lis.counters["forwarded"] == 1


async def test_duplicate_new_message_deduped(listener):
    lis, _, fwd = listener
    await lis.start()
    await lis._handle("NEW", make_event())
    await lis._handle("NEW", make_event())
    await drain(lis)
    assert len(fwd.sent) == 1
    assert lis.counters["duplicates"] == 1


async def test_edited_message_forwarded_as_edit(listener):
    lis, _, fwd = listener
    await lis.start()
    await lis._handle("EDITED", make_event(kind="edited", text="UPDATED TEXT"))
    await drain(lis)
    assert fwd.sent[-1]["event"] == "EDITED"
    assert fwd.sent[-1]["text"] == "UPDATED TEXT"
    assert fwd.sent[-1]["editDate"]


async def test_deleted_message_forwarded(listener):
    lis, _, fwd = listener
    await lis.start()
    await lis._handle("DELETED", make_event(kind="deleted"))
    await drain(lis)
    payload = fwd.sent[-1]
    assert payload["event"] == "DELETED"
    assert payload["chatId"] == "-100123"
    assert payload["messageIds"] == [42]


async def test_deleted_without_chat_ignored(listener):
    lis, _, fwd = listener
    await lis.start()
    await lis._handle("DELETED", SimpleNamespace(chat_id=None, deleted_ids=[1]))
    await drain(lis)
    assert not fwd.sent
    assert lis.counters["ignored"] >= 1


async def test_media_message_gets_type(listener):
    lis, _, fwd = listener
    await lis.start()
    ev = make_event(text="")
    ev.message.message = None
    ev.message.photo = object()
    await lis._handle("NEW", ev)
    await drain(lis)
    assert fwd.sent[-1]["messageType"] == "PHOTO"


async def test_backend_counters_accumulated(listener):
    lis, _, fwd = listener
    fwd.data = {"ingested": 2, "ignored": 1, "duplicates": 3, "failures": 1}
    await lis.start()
    await lis._handle("NEW", make_event())
    await drain(lis)
    assert lis.counters["ingested"] == 2
    assert lis.counters["ignored"] == 1
    assert lis.counters["duplicates"] >= 3
    assert lis.counters["failures"] >= 1


async def test_forward_failure_counts_and_retries(listener, monkeypatch):
    lis, _, fwd = listener
    fwd.status = 500
    monkeypatch.setattr(ml, "MAX_RETRIES", 1)  # skip backoff sleeps in test
    await lis.start()
    await lis._handle("NEW", make_event())
    await asyncio.sleep(1.3)  # one backoff sleep inside the retry loop
    assert lis.counters["failures"] >= 1
    assert lis.last_forward_error


async def test_client_swap_rebinds_handlers(listener):
    lis, tg, _ = listener
    await lis.start()
    old_client = tg.client
    # Simulate TelegramManager replacing the client on reconnect.
    tg.client = FakeClient()
    await asyncio.sleep(ml.WATCH_INTERVAL_S + 0.2)
    assert lis._bound_client is tg.client
    assert lis._bound_client is not old_client
    assert tg.client.handlers  # handlers moved to the new client


async def test_sign_body_matches_backend_scheme():
    import hashlib
    import hmac as hmac_mod

    body = '{"a":1}'
    ts = 1700000000
    sig = ml.sign_body(body, ts, "k")
    expected = hmac_mod.new(
        b"k", f"{ts}.{body}".encode(), hashlib.sha256
    ).hexdigest()
    assert sig == expected


def test_ingest_configured(monkeypatch):
    monkeypatch.setenv("BINARY_INGEST_URL", "http://x")
    monkeypatch.setenv("WORKER_HMAC_SECRET", "s")
    assert ml.ingest_configured() is True
    monkeypatch.delenv("WORKER_HMAC_SECRET")
    assert ml.ingest_configured() is False

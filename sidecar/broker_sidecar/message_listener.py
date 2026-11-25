"""Telegram message listener for the broker sidecar.

Attaches Telethon event handlers (NewMessage / MessageEdited /
MessageDeleted) to the SINGLE shared client owned by
``telegram_service.tg`` — no second TelegramClient is ever created.

Normalized events are forwarded to the Next.js backend's internal ingest
endpoint (``BINARY_INGEST_URL``) as HMAC-SHA256-signed JSON, the same
scheme SHILLMONGER_BOT's worker uses (``WORKER_HMAC_SECRET``). The backend
performs the BINARY_PROVIDER lookup, broker routing and MongoDB
persistence — this module never touches the database and never performs
any trade action.

Env:
  BINARY_INGEST_URL        — e.g. http://127.0.0.1:3000/api/internal/binary-telegram/ingest
  WORKER_HMAC_SECRET       — shared secret with the Next.js app
  TELEGRAM_LISTENER_AUTO_START — start the listener on sidecar startup
  LISTENER_FORWARD_TIMEOUT — per-request timeout seconds (default 10)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections import OrderedDict
from typing import Any, Optional

from . import telegram_service
from .errors import BrokerUnavailableError

logger = logging.getLogger("broker_sidecar.listener")

try:
    from telethon import events, utils  # noqa: F401

    _HAVE_TELETHON = True
except Exception:  # pragma: no cover
    events = None  # type: ignore
    utils = None  # type: ignore
    _HAVE_TELETHON = False


# ------------------------------------------------------------------ states

LISTENER_STATES = (
    "STOPPED",
    "STARTING",
    "RUNNING",
    "RECONNECTING",
    "DEGRADED",
    "ERROR",
)

# Bounded in-memory structures — the listener must not grow without limit.
MAX_SEEN_KEYS = 5000          # dedup window for recently forwarded events
MAX_QUEUE_SIZE = 1000         # outbound forward queue
MAX_RETRIES = 3               # HTTP forward attempts per event
MAX_EDITS_KEPT = 20           # mirrored in the backend model
WATCH_INTERVAL_S = 3.0        # client-swap / disconnect watcher period


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def ingest_configured() -> bool:
    return bool(_env("BINARY_INGEST_URL") and _env("WORKER_HMAC_SECRET"))


def _forward_timeout() -> float:
    try:
        return max(1.0, float(_env("LISTENER_FORWARD_TIMEOUT", "10")))
    except ValueError:
        return 10.0


# ------------------------------------------------------------- normalization


def _peer_chat_id(event: Any, message: Any = None) -> Optional[str]:
    """Marked Telegram chat id (e.g. -100xxxxxxxxxx) matching dialog.id."""
    chat_id = getattr(event, "chat_id", None)
    if chat_id is None and message is not None:
        peer = getattr(message, "peer_id", None)
        if peer is not None and utils is not None:
            try:
                chat_id = utils.get_peer_id(peer)
            except Exception:
                chat_id = None
    if chat_id is None:
        return None
    return str(chat_id)


def _sender_name(sender: Any) -> Optional[str]:
    if sender is None:
        return None
    parts = [
        getattr(sender, "first_name", None),
        getattr(sender, "last_name", None),
    ]
    name = " ".join(p for p in parts if p)
    if not name:
        name = getattr(sender, "username", None) or ""
    return name or None


def _message_type(message: Any) -> str:
    text = getattr(message, "message", None)
    if isinstance(text, str) and text.strip():
        return "TEXT"
    # No text — classify supported media generically; the backend stores
    # the raw type and treats non-TEXT payloads as unsupported for parsing.
    for attr, label in (
        ("photo", "PHOTO"),
        ("video", "VIDEO"),
        ("voice", "VOICE"),
        ("audio", "AUDIO"),
        ("sticker", "STICKER"),
        ("document", "DOCUMENT"),
        ("gif", "GIF"),
    ):
        if getattr(message, attr, None) is not None:
            return label
    return "UNSUPPORTED"


async def _sender_display(event: Any) -> tuple[Optional[str], Optional[str]]:
    """(sender_id, sender_name) — best effort, never raises."""
    sender_id = getattr(event, "sender_id", None)
    if sender_id is None:
        msg = getattr(event, "message", None)
        sender_id = getattr(msg, "sender_id", None) if msg is not None else None
    sender_name = None
    get_sender = getattr(event, "get_sender", None)
    if callable(get_sender):
        try:
            sender = await asyncio.wait_for(get_sender(), timeout=5)
            sender_name = _sender_name(sender)
            if sender_id is None:
                sender_id = getattr(sender, "id", None)
        except Exception:
            pass
    return (str(sender_id) if sender_id is not None else None, sender_name)


async def normalize_event(event_kind: str, event: Any) -> Optional[dict[str, Any]]:
    """Convert a Telethon event into the ingest payload, or None."""
    if event_kind == "DELETED":
        chat_id = _peer_chat_id(event)
        deleted_ids = [
            int(i) for i in (getattr(event, "deleted_ids", None) or [])
            if isinstance(i, int)
        ]
        if not chat_id or not deleted_ids:
            return None
        return {
            "event": "DELETED",
            "chatId": chat_id,
            "messageIds": deleted_ids,
        }

    message = getattr(event, "message", None)
    if message is None:
        return None
    message_id = getattr(message, "id", None)
    chat_id = _peer_chat_id(event, message)
    if message_id is None or chat_id is None:
        return None

    sender_id, sender_name = await _sender_display(event)
    msg_date = getattr(message, "date", None)
    payload: dict[str, Any] = {
        "event": "EDITED" if event_kind == "EDITED" else "NEW",
        "chatId": chat_id,
        "messageId": int(message_id),
        "senderId": sender_id,
        "senderName": sender_name,
        "text": getattr(message, "message", None) or "",
        "messageDate": msg_date.isoformat() if msg_date else None,
        "messageType": _message_type(message),
    }
    edit_date = getattr(message, "edit_date", None)
    if edit_date is not None:
        payload["editDate"] = edit_date.isoformat()
    chat = getattr(event, "chat", None)
    title = getattr(chat, "title", None) if chat is not None else None
    if title:
        payload["chatTitle"] = title
    return payload


# ------------------------------------------------------------- signing/http


def sign_body(raw_body: str, timestamp: int, secret: str) -> str:
    """HMAC-SHA256 over '{timestamp}.{raw_body}' — mirrors lib/signal-ingest.ts."""
    return hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.{raw_body}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class Forwarder:
    """Signs and POSTs normalized events to the Next.js ingest endpoint."""

    def __init__(self) -> None:
        self._session = None  # lazily created requests.Session

    def _post(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        import requests  # runtime dep — imported lazily for tests

        url = _env("BINARY_INGEST_URL")
        secret = _env("WORKER_HMAC_SECRET")
        if not url or not secret:
            raise BrokerUnavailableError("binary ingest endpoint is not configured")
        raw_body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        ts = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "X-Timestamp": str(ts),
            "X-Signature": sign_body(raw_body, ts, secret),
        }
        if self._session is None:
            self._session = requests.Session()
        resp = self._session.post(
            url, data=raw_body.encode("utf-8"), headers=headers,
            timeout=_forward_timeout(),
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        return resp.status_code, data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- listener


class TelegramMessageListener:
    """Managed lifecycle wrapper around Telethon message events.

    Idempotent: ``start()`` on a running listener is a no-op. Handlers are
    bound to whatever client ``telegram_service.tg`` currently owns; a
    watcher task re-binds them if the Telegram client is ever replaced
    (reconnect / re-auth) and flips the state to RECONNECTING/RUNNING.
    """

    def __init__(self, tg: Any = None, forwarder: Any = None) -> None:
        self.tg = tg if tg is not None else telegram_service.tg
        self.forwarder = forwarder or Forwarder()
        self.state = "STOPPED"
        self.last_error: Optional[str] = None
        self.started_at: Optional[float] = None
        self.stopped_at: Optional[float] = None
        self.last_event_at: Optional[float] = None
        self.last_forward_ok_at: Optional[float] = None
        self.last_forward_error: Optional[str] = None
        self._bound_client: Any = None
        self._new_handler = None
        self._edit_handler = None
        self._delete_handler = None
        self._queue: Optional[asyncio.Queue] = None
        self._worker: Optional[asyncio.Task] = None
        self._watcher: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._seen: OrderedDict[tuple[str, int], None] = OrderedDict()
        self.counters = {
            "received": 0,
            "forwarded": 0,
            "ingested": 0,
            "ignored": 0,
            "duplicates": 0,
            "failures": 0,
        }

    # ------------------------------------------------------------- status

    def to_dict(self) -> dict[str, Any]:
        tg_status = self.tg.to_dict() if hasattr(self.tg, "to_dict") else {}
        return {
            "state": self.state,
            "running": self.state == "RUNNING",
            "startedAt": self.started_at,
            "stoppedAt": self.stopped_at,
            "lastEventAt": self.last_event_at,
            "lastForwardOkAt": self.last_forward_ok_at,
            "lastError": self.last_error,
            "lastForwardError": self.last_forward_error,
            "ingestConfigured": ingest_configured(),
            "telegramStatus": tg_status.get("status"),
            "telegramConnected": tg_status.get("connected", False),
            "counters": dict(self.counters),
        }

    # ------------------------------------------------------------ helpers

    def _mark_seen(self, chat_id: str, message_id: int) -> bool:
        """True if this (chat,message) was already forwarded recently."""
        key = (chat_id, int(message_id))
        if key in self._seen:
            self._seen.move_to_end(key)
            return True
        self._seen[key] = None
        while len(self._seen) > MAX_SEEN_KEYS:
            self._seen.popitem(last=False)
        return False

    def _remember_duplicate(self, chat_id: str, message_id: int) -> None:
        key = (chat_id, int(message_id))
        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > MAX_SEEN_KEYS:
            self._seen.popitem(last=False)

    # ------------------------------------------------------------ handlers

    async def _handle(self, kind: str, event: Any) -> None:
        try:
            payload = await normalize_event(kind, event)
        except Exception as exc:
            self.counters["failures"] += 1
            logger.warning("listener normalize failed (%s): %s", kind, exc)
            return
        if payload is None:
            self.counters["ignored"] += 1
            return

        chat_id = payload.get("chatId") or ""
        if kind == "DELETED":
            ids = payload.get("messageIds") or []
            # Deleted events may cover several ids — dedup on first id only
            # for the seen-window; the backend dedups per message anyway.
            first_id = ids[0] if ids else 0
            if self._mark_seen(chat_id, first_id):
                self.counters["duplicates"] += 1
                return
        else:
            mid = int(payload.get("messageId") or 0)
            # Edits of the same message are legitimate re-forwards — only
            # dedup exact NEW deliveries.
            if kind == "NEW" and self._mark_seen(chat_id, mid):
                self.counters["duplicates"] += 1
                return

        self.counters["received"] += 1
        self.last_event_at = time.time()
        if self._queue is None:
            self.counters["failures"] += 1
            return
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.counters["failures"] += 1
            self.last_forward_error = "forward queue full — event dropped"
            logger.warning("listener forward queue full; event dropped")

    async def _on_new_message(self, event: Any) -> None:
        await self._handle("NEW", event)

    async def _on_message_edited(self, event: Any) -> None:
        await self._handle("EDITED", event)

    async def _on_message_deleted(self, event: Any) -> None:
        await self._handle("DELETED", event)

    # ------------------------------------------------------------- forward

    async def _send_once(self, payload: dict[str, Any]) -> tuple[bool, Optional[str]]:
        """Returns (ok, retryable_error)."""
        try:
            status, data = await asyncio.to_thread(self.forwarder._post, payload)
        except Exception as exc:
            return False, str(exc)[:200]
        if 200 <= status < 300:
            # Accumulate backend-reported ingestion counters.
            for key in ("ingested", "ignored", "duplicates", "failures", "stored", "failed"):
                val = data.get(key)
                if isinstance(val, int) and key in self.counters:
                    self.counters[key] += val
            self.counters["forwarded"] += 1
            self.last_forward_ok_at = time.time()
            self.last_forward_error = None
            if self.state == "DEGRADED":
                self.state = "RUNNING" if self._tg_connected() else "RECONNECTING"
            return True, None
        if status == 429 or status >= 500:
            return False, f"HTTP {status}"
        # 4xx — payload rejected; retrying won't help.
        detail = data.get("error") or data.get("detail") or f"HTTP {status}"
        return False, f"rejected: {str(detail)[:150]}"

    async def _drain_queue(self) -> None:
        assert self._queue is not None
        while True:
            payload = await self._queue.get()
            try:
                last_err: Optional[str] = None
                delivered = False
                for attempt in range(MAX_RETRIES):
                    ok, err = await self._send_once(payload)
                    if ok:
                        delivered = True
                        break
                    last_err = err
                    # Non-retryable rejections stop immediately.
                    if err and err.startswith("rejected:"):
                        break
                    await asyncio.sleep(min(2 ** attempt, 8))
                if not delivered:
                    self.counters["failures"] += 1
                    self.last_forward_error = last_err or "forward failed"
                    logger.warning(
                        "listener forward failed after %d attempts: %s",
                        MAX_RETRIES, self.last_forward_error,
                    )
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------ binding

    def _tg_connected(self) -> bool:
        try:
            return bool(
                self.tg.status == "connected"
                and self.tg.client is not None
                and self.tg.client.is_connected()
            )
        except Exception:
            return False

    def _register_handlers(self, client: Any) -> None:
        if client is None or not _HAVE_TELETHON:
            return
        if self._bound_client is client:
            return
        self._unregister_handlers()
        self._new_handler = client.add_event_handler(
            self._on_new_message, events.NewMessage
        )
        self._edit_handler = client.add_event_handler(
            self._on_message_edited, events.MessageEdited
        )
        self._delete_handler = client.add_event_handler(
            self._on_message_deleted, events.MessageDeleted
        )
        self._bound_client = client
        logger.info("listener handlers bound to telegram client")

    def _unregister_handlers(self) -> None:
        client = self._bound_client
        self._bound_client = None
        if client is None:
            return
        for handler, ev in (
            (self._new_handler, events.NewMessage if _HAVE_TELETHON else None),
            (self._edit_handler, events.MessageEdited if _HAVE_TELETHON else None),
            (self._delete_handler, events.MessageDeleted if _HAVE_TELETHON else None),
        ):
            if handler is None or ev is None:
                continue
            try:
                client.remove_event_handler(handler, ev)
            except Exception:
                logger.debug("listener handler removal failed", exc_info=True)
        self._new_handler = self._edit_handler = self._delete_handler = None

    async def _watch(self) -> None:
        """Re-bind handlers if tg swaps its client; track connectivity."""
        while True:
            await asyncio.sleep(WATCH_INTERVAL_S)
            try:
                client = getattr(self.tg, "client", None)
                connected = self._tg_connected()
                if client is not None and connected:
                    if self._bound_client is not client:
                        self._register_handlers(client)
                    if self.state in ("RECONNECTING", "DEGRADED"):
                        self.state = "RUNNING" if ingest_configured() else "DEGRADED"
                elif self.state == "RUNNING":
                    self.state = "RECONNECTING"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("listener watcher error: %s", exc)

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> dict[str, Any]:
        async with self._lock:
            if self.state in ("RUNNING", "STARTING", "RECONNECTING"):
                return self.to_dict()
            if not _HAVE_TELETHON:
                self.state = "ERROR"
                self.last_error = "telethon is not installed"
                return self.to_dict()

            self.state = "STARTING"
            self.last_error = None
            try:
                # Reuse the managed Telegram session — never create a
                # competing client. Connect only if not already live.
                if not self._tg_connected():
                    result = await self.tg.connect()
                    if result.get("status") != "connected":
                        self.state = "ERROR"
                        self.last_error = (
                            result.get("lastError")
                            or "Telegram session unavailable — connect first"
                        )
                        return self.to_dict()

                self._queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
                self._worker = asyncio.create_task(self._drain_queue())
                self._watcher = asyncio.create_task(self._watch())
                self._register_handlers(self.tg.client)
                self.started_at = time.time()
                self.stopped_at = None
                if ingest_configured():
                    self.state = "RUNNING"
                else:
                    self.state = "DEGRADED"
                    self.last_error = (
                        "BINARY_INGEST_URL / WORKER_HMAC_SECRET not configured — "
                        "events are received but not forwarded"
                    )
                logger.info("telegram message listener started state=%s", self.state)
                return self.to_dict()
            except Exception as exc:
                self.state = "ERROR"
                self.last_error = str(exc)[:300]
                logger.warning("listener start failed: %s", exc)
                await self._teardown()
                return self.to_dict()

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            await self._teardown()
            self.state = "STOPPED"
            self.stopped_at = time.time()
            logger.info("telegram message listener stopped")
            return self.to_dict()

    async def restart(self) -> dict[str, Any]:
        async with self._lock:
            await self._teardown()
            self.state = "STOPPED"
        return await self.start()

    async def _teardown(self) -> None:
        for task in (self._worker, self._watcher):
            if task is not None:
                task.cancel()
        self._worker = self._watcher = None
        if self._queue is not None:
            try:
                while not self._queue.empty():
                    self._queue.get_nowait()
                    self._queue.task_done()
            except Exception:
                pass
            self._queue = None
        self._unregister_handlers()
        # Allow cancelled tasks to finish.
        await asyncio.sleep(0)


listener = TelegramMessageListener()


async def auto_start_listener_if_enabled() -> None:
    """Start the listener on sidecar boot when TELEGRAM_LISTENER_AUTO_START."""
    if _env("TELEGRAM_LISTENER_AUTO_START", "false").lower() != "true":
        return
    result = await listener.start()
    logger.info("telegram listener auto-start result: %s", result.get("state"))

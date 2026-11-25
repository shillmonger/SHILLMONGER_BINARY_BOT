"""Telegram connection service for the sidecar.

Wraps Telethon (the same library SHILLMONGER_BOT uses) with an independent
session for BINARY_TRADING_BOTS. The session comes from
TELEGRAM_SESSION_STRING (StringSession) — generate a NEW one with
``tools/telegram_login.py``; never reuse SHILLMONGER_BOT's session.

This module only manages the connection and fetches dialog/group metadata.
No message listening, signal parsing or trading happens here.

Env (loaded from BINARY_TRADING_BOTS/.env and sidecar/.env):
  API_ID, API_HASH, PHONE_NUMBER           — same Telegram app credentials
  TELEGRAM_SESSION_STRING                  — authorized StringSession
  TELEGRAM_AUTO_START=true|false           — connect on sidecar startup
  ALLOW_INTERACTIVE_LOGIN=true|false       — permit client.start() prompts
                                             (only useful in a real terminal)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

from .errors import BrokerError, BrokerUnavailableError

logger = logging.getLogger("broker_sidecar.telegram")

try:
    from telethon import TelegramClient, events  # noqa: F401
    from telethon.errors import FloodWaitError, RPCError
    from telethon.sessions import StringSession

    _HAVE_TELETHON = True
except Exception:  # pragma: no cover - depends on env
    TelegramClient = None  # type: ignore
    FloodWaitError = RPCError = Exception  # type: ignore
    StringSession = None  # type: ignore
    _HAVE_TELETHON = False


class TelegramError(BrokerError):
    code = "telegram_error"


class TelegramAuthRequired(TelegramError):
    code = "auth_required"


class TelegramFloodWait(TelegramError):
    code = "flood_wait"

    def __init__(self, wait_seconds: float):
        super().__init__(f"Telegram rate limit — retry in {int(wait_seconds)}s")
        self.wait_seconds = wait_seconds


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def api_id() -> Optional[int]:
    raw = _env("API_ID")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def configured() -> bool:
    return bool(api_id() and _env("API_HASH") and _env("PHONE_NUMBER"))


def _mask_phone(phone: Optional[str]) -> Optional[str]:
    if not phone:
        return None
    digits = "".join(character for character in phone if character.isdigit())
    return f"***{digits[-4:]}" if digits else "***"


class TelegramManager:
    """Owns the single Telethon client for this application."""

    def __init__(self) -> None:
        self.client: Any = None
        self.status = "disconnected"  # disconnected|connecting|connected|auth_required|error
        self.last_error: Optional[str] = None
        self.connected_at: Optional[float] = None
        self.account_phone: Optional[str] = None
        self.account_username: Optional[str] = None
        self.account_name: Optional[str] = None
        self.groups_cache: list[dict[str, Any]] = []
        self.groups_synced_at: Optional[float] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- config

    def _new_client(self) -> Any:
        if not _HAVE_TELETHON:
            raise BrokerUnavailableError(
                "telethon is not installed — run `pip install -r requirements.txt`"
            )
        if not configured():
            raise TelegramAuthRequired(
                "Telegram is not configured — set API_ID, API_HASH, PHONE_NUMBER "
                "and TELEGRAM_SESSION_STRING"
            )
        session_string = _env("TELEGRAM_SESSION_STRING")
        if session_string:
            try:
                session = StringSession(session_string)
            except (TypeError, ValueError) as exc:
                raise TelegramAuthRequired(
                    "TELEGRAM_SESSION_STRING is invalid; regenerate the session with "
                    "`python tools/telegram_login.py`"
                ) from exc
        elif _env("ALLOW_INTERACTIVE_LOGIN", "false").lower() == "true":
            session = StringSession()  # interactive client.start() login
        else:
            raise TelegramAuthRequired(
                "TELEGRAM_SESSION_STRING is empty — generate one with "
                "`python tools/telegram_login.py` and set it in .env"
            )
        return TelegramClient(session, api_id(), _env("API_HASH"))

    # ------------------------------------------------------------ status

    def to_dict(self) -> dict[str, Any]:
        try:
            transport_connected = bool(self.client and self.client.is_connected())
        except Exception:
            transport_connected = False
        return {
            "status": self.status,
            "connected": self.status == "connected",
            "authorized": self.status == "connected",
            "transportConnected": transport_connected,
            "configured": configured(),
            "hasSessionString": bool(_env("TELEGRAM_SESSION_STRING")),
            "phone": _mask_phone(self.account_phone),
            "username": self.account_username,
            "name": self.account_name,
            "lastError": self.last_error,
            "connectedAt": self.connected_at,
            "groupsSyncedAt": self.groups_synced_at,
            "groupCount": len(self.groups_cache),
            "interactiveLoginAllowed": _env("ALLOW_INTERACTIVE_LOGIN", "false").lower() == "true",
        }

    async def get_status(self) -> dict[str, Any]:
        """Refresh transport and authorization state before returning status."""
        async with self._lock:
            client = self.client
            if client is None:
                return self.to_dict()
            try:
                if not client.is_connected():
                    await self._disconnect_locked()
                    self.last_error = "Telegram transport is disconnected"
                    return self.to_dict()
                authorized = await asyncio.wait_for(
                    client.is_user_authorized(), timeout=30
                )
                if authorized:
                    self.status = "connected"
                    self.last_error = None
                else:
                    self.status = "auth_required"
                    self.last_error = (
                        "Telegram transport is connected, but the session is not authorized"
                    )
            except FloodWaitError as exc:
                self.status = "error"
                self.last_error = f"Telegram rate limit — retry in {int(exc.seconds)}s"
            except asyncio.TimeoutError:
                self.status = "error"
                self.last_error = "Telegram authorization check timed out"
            except Exception as exc:
                self.status = "error"
                self.last_error = f"Telegram status check failed: {type(exc).__name__}"
                logger.warning(
                    "telegram status check failed: %s", type(exc).__name__
                )
            return self.to_dict()

    # ----------------------------------------------------------- connect

    async def connect(self) -> dict[str, Any]:
        """Connect (or reconnect) the Telegram client. Idempotent."""
        async with self._lock:
            if self.client is not None:
                try:
                    if (
                        self.status == "connected"
                        and self.client.is_connected()
                        and await self.client.is_user_authorized()
                    ):
                        return self.to_dict()
                except Exception:
                    pass
                await self._disconnect_locked()

            self.status = "connecting"
            self.last_error = None
            client = None
            try:
                client = self._new_client()
                await asyncio.wait_for(client.connect(), timeout=30)

                if not await client.is_user_authorized():
                    await asyncio.wait_for(client.disconnect(), timeout=10)
                    self.status = "auth_required"
                    self.last_error = (
                        "Telegram transport connected, but this session is not authorized; "
                        "generate a new TELEGRAM_SESSION_STRING with tools/telegram_login.py"
                    )
                    return self.to_dict()

                me = await client.get_me()
                self.client = client
                self.status = "connected"
                self.connected_at = time.time()
                self.account_phone = getattr(me, "phone", None)
                self.account_username = getattr(me, "username", None)
                self.account_name = " ".join(
                    p for p in [getattr(me, "first_name", None), getattr(me, "last_name", None)] if p
                ) or None
                self.last_error = None
                logger.info("telegram account authorized")
                return self.to_dict()
            except TelegramAuthRequired as exc:
                await self._close_candidate(client)
                self.status = "auth_required"
                self.last_error = str(exc)
                return self.to_dict()
            except TelegramFloodWait as exc:
                await self._close_candidate(client)
                self.status = "error"
                self.last_error = str(exc)
                return self.to_dict()
            except Exception as exc:
                await self._close_candidate(client)
                if _HAVE_TELETHON and isinstance(exc, FloodWaitError):
                    self.status = "error"
                    self.last_error = f"Telegram rate limit — retry in {int(exc.seconds)}s"
                    return self.to_dict()
                self.status = "error"
                self.last_error = str(exc)[:300]
                logger.warning("telegram connect failed: %s", exc)
                return self.to_dict()

    async def _close_candidate(self, client: Any) -> None:
        if client is None:
            return
        try:
            await asyncio.wait_for(client.disconnect(), timeout=10)
        except Exception:
            logger.debug("telegram failed-client cleanup error", exc_info=True)

    async def disconnect(self) -> dict[str, Any]:
        async with self._lock:
            await self._disconnect_locked()
            return self.to_dict()

    async def _disconnect_locked(self) -> None:
        client, self.client = self.client, None
        self.status = "disconnected"
        self.connected_at = None
        self.groups_cache = []
        self.groups_synced_at = None
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=10)
            except Exception:
                logger.debug("telegram disconnect error (ignored)", exc_info=True)

    # ------------------------------------------------------------ groups

    async def fetch_groups(self) -> dict[str, Any]:
        """Fetch dialogs (groups + supergroups + channels) from Telegram."""
        from telethon.tl.types import Channel, Chat  # type: ignore

        async with self._lock:
            if self.client is None or self.status != "connected":
                raise TelegramAuthRequired("Telegram is not connected")
            try:
                if not self.client.is_connected():
                    self.status = "error"
                    self.last_error = "Telegram transport is disconnected"
                    raise TelegramError(self.last_error)
                if not await self.client.is_user_authorized():
                    self.status = "auth_required"
                    self.last_error = "Telegram session is no longer authorized"
                    raise TelegramAuthRequired(self.last_error)
                dialogs = await asyncio.wait_for(
                    self.client.get_dialogs(), timeout=60
                )
            except TelegramAuthRequired:
                raise
            except TelegramError:
                raise
            except FloodWaitError as exc:
                raise TelegramFloodWait(float(getattr(exc, "seconds", 30))) from exc
            except Exception as exc:
                self.status = "error"
                self.last_error = str(exc)[:300]
                raise TelegramError(f"Failed to fetch dialogs: {exc}") from exc

            groups: list[dict[str, Any]] = []
            for dialog in dialogs:
                entity = dialog.entity
                if not isinstance(entity, (Channel, Chat)):
                    continue
                is_broadcast = bool(getattr(entity, "broadcast", False))
                is_megagroup = bool(getattr(entity, "megagroup", False))
                if isinstance(entity, Chat):
                    gtype = "group"
                elif is_broadcast:
                    gtype = "channel"
                elif is_megagroup:
                    gtype = "supergroup"
                else:
                    gtype = "group"
                groups.append(
                    {
                        "id": str(dialog.id),
                        "name": dialog.name or "",
                        "username": getattr(entity, "username", None),
                        "type": gtype,
                        "participantsCount": getattr(entity, "participants_count", None),
                    }
                )
            groups.sort(key=lambda g: g["name"].lower())
            self.groups_cache = groups
            self.groups_synced_at = time.time()
            self.status = "connected"
            self.last_error = None
            logger.info("telegram groups fetched count=%d", len(groups))
            return {
                "ok": True,
                "groups": groups,
                "syncedAt": self.groups_synced_at,
                "count": len(groups),
            }


tg = TelegramManager()


async def auto_start_if_enabled() -> None:
    """Called from the FastAPI lifespan when TELEGRAM_AUTO_START=true."""
    if _env("TELEGRAM_AUTO_START", "false").lower() != "true":
        return
    if not configured():
        logger.info("telegram auto-start skipped — API_ID/API_HASH/PHONE_NUMBER not set")
        return
    result = await tg.connect()
    logger.info("telegram auto-start result: %s", result.get("status"))

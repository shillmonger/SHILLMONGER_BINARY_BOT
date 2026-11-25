"""IQ Option session wrapper around the community `iqoptionapi` client.

Uses https://github.com/iqoptionapi/iqoptionapi (stable_api.IQ_Option,
v7.1.1) — the de-facto standard but UNMAINTAINED since 2022 and based on
the broker's unofficial web protocol. It can break without notice; use
PRACTICE accounts only.

The library is fully synchronous with internal busy-wait loops, so every
call is executed in a worker thread with an asyncio timeout. A timed-out
call leaves the broker thread running — the session is then closed on
disconnect; this is a known limitation, documented in the README.

Account type is hard-pinned to PRACTICE — REAL cannot be reached through
the sidecar.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

from .errors import (
    BrokerAuthError,
    BrokerError,
    BrokerOtpRequired,
    BrokerTimeoutError,
    BrokerUnavailableError,
)
from .sessions import BrokerSession

logger = logging.getLogger("broker_sidecar.iqoption")

try:
    from iqoptionapi.stable_api import IQ_Option  # type: ignore

    _HAVE_IQ = True
except Exception:  # pragma: no cover - depends on env
    IQ_Option = None  # type: ignore
    _HAVE_IQ = False


def _require_lib() -> None:
    if not _HAVE_IQ:
        raise BrokerUnavailableError(
            "iqoptionapi is not installed — run `pip install -r sidecar/requirements.txt`"
        )


def _host() -> Optional[str]:
    return os.environ.get("IQOPTION_HOST") or None


def _new_client(session: BrokerSession) -> Any:
    """Construct the sync IQ_Option client pinned to a PRACTICE account."""
    _require_lib()
    return IQ_Option(session.email, session.password, active_account_type="PRACTICE")


async def connect(session: BrokerSession, otp: Optional[str] = None) -> None:
    """Authenticate + open the IQ Option websocket for a PRACTICE account.

    First call: ``client.connect()``. If the broker demands 2FA the library
    sends the SMS itself and returns ``(False, "2FA")`` — we surface
    BrokerOtpRequired and keep the client alive so ``complete_otp`` can
    finish the handshake with the user-supplied code.
    """
    _require_lib()
    if otp:
        session._otp_code = otp

    # The library keeps websocket state in module-level globals, so a second
    # simultaneous IQ_Option can interfere. Serialize per-process connects.
    async with _IQ_CONNECT_LOCK:
        client = session.client or await asyncio.to_thread(_new_client, session)
        session.client = client
        started = time.time()
        try:
            code = session._otp_code
            if code:
                ok, reason = await asyncio.wait_for(
                    asyncio.to_thread(client.connect_2fa, code),
                    timeout=_connect_timeout(),
                )
            else:
                ok, reason = await asyncio.wait_for(
                    asyncio.to_thread(client.connect), timeout=_connect_timeout()
                )
        except asyncio.TimeoutError as exc:
            raise BrokerTimeoutError("IQ Option connect timed out") from exc
        except Exception as exc:
            raise BrokerAuthError(f"IQ Option authentication failed: {exc}") from exc

    if ok is not True:
        reason_str = str(reason or "")
        if reason_str == "2FA" or "verify" in reason_str:
            session.meta.requires_otp = True
            raise BrokerOtpRequired()
        raise BrokerAuthError(f"IQ Option authentication failed: {reason_str or 'unknown'}")

    session._otp_code = None
    await _post_connect(session, started)
    logger.info("iqoption session connected user=%s", session.user_id)


async def complete_otp(session: BrokerSession, code: str) -> None:
    """Finish a 2FA handshake on an existing pending session."""
    _require_lib()
    client = session.client
    if client is None:
        raise BrokerAuthError("No pending IQ Option session — connect first")
    session._otp_code = code
    started = time.time()
    try:
        ok, reason = await asyncio.wait_for(
            asyncio.to_thread(client.connect_2fa, code), timeout=_connect_timeout()
        )
    except asyncio.TimeoutError as exc:
        raise BrokerTimeoutError("IQ Option OTP verification timed out") from exc
    except Exception as exc:
        raise BrokerAuthError(f"IQ Option OTP verification failed: {exc}") from exc
    if ok is not True:
        raise BrokerAuthError(f"IQ Option OTP rejected: {reason}")
    session._otp_code = None
    session.meta.requires_otp = False
    await _post_connect(session, started)
    logger.info("iqoption session connected via OTP user=%s", session.user_id)


async def _post_connect(session: BrokerSession, started: float) -> None:
    """Shared post-auth steps: verify, load account info, mark connected."""
    if not await verify_quiet(session):
        raise BrokerAuthError("IQ Option session could not be verified")
    await _refresh_account(session)
    session.meta.connected = True
    session.meta.connected_at = time.time()
    session.meta.last_check = time.time()
    session.meta.latency_ms = round((time.time() - started) * 1000, 1)
    session.meta.last_error = None


async def verify(session: BrokerSession) -> dict[str, Any]:
    """Health check: websocket flag + balance fetch (blocking, threaded)."""
    _require_lib()
    client = session.client
    if client is None:
        raise BrokerAuthError("No active IQ Option session")
    started = time.time()
    try:
        alive = await asyncio.wait_for(
            asyncio.to_thread(client.check_connect), timeout=_health_timeout()
        )
    except asyncio.TimeoutError as exc:
        raise BrokerTimeoutError("IQ Option health check timed out") from exc
    if not alive:
        session.meta.connected = False
        session.meta.last_error = "Websocket disconnected"
        return {"ok": False, "error": "Websocket disconnected"}
    try:
        await _refresh_account(session)
    except Exception as exc:
        session.meta.last_error = str(exc)[:200]
    session.meta.last_check = time.time()
    session.meta.latency_ms = round((time.time() - started) * 1000, 1)
    session.meta.connected = True
    return {"ok": True, "latencyMs": session.meta.latency_ms}


async def verify_quiet(session: BrokerSession) -> bool:
    try:
        result = await verify(session)
        return bool(result.get("ok"))
    except BrokerError:
        raise
    except Exception:
        return False


async def disconnect(session: BrokerSession) -> None:
    client = session.client
    session.client = None
    session.meta.connected = False
    session.meta.requires_otp = False
    session._otp_code = None
    if client is not None:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_close_client, client), timeout=_health_timeout()
            )
        except Exception:
            logger.debug("iqoption close error (ignored)", exc_info=True)
    session.meta.last_error = None
    logger.info("iqoption session disconnected user=%s", session.user_id)


def _close_client(client: Any) -> None:
    try:
        client.api.close()
    except Exception:
        pass


async def get_candles(
    session: BrokerSession, asset: str, interval: int = 60, count: int = 100
) -> list[dict[str, Any]]:
    """Historical candles — best-effort.

    iqoptionapi needs its ACTIVES map (populated by get_all_init, a slow
    multi-second call) before get_candles works. We populate it lazily once
    per session and cache it on the client object.
    """
    _require_lib()
    client = session.client
    if client is None:
        raise BrokerAuthError("No active IQ Option session")

    def _fetch() -> Any:
        import iqoptionapi.constants as OP_code  # type: ignore

        if not getattr(client, "_actives_loaded", False):
            client.get_all_init()  # populates OP_code.ACTIVES (slow, ~seconds)
            client._actives_loaded = True
        endtime = int(time.time())
        return client.get_candles(asset.upper(), interval, count, endtime)

    try:
        candles = await asyncio.wait_for(
            asyncio.to_thread(_fetch), timeout=_connect_timeout()
        )
    except asyncio.TimeoutError as exc:
        raise BrokerTimeoutError("IQ Option candles timed out") from exc
    except Exception as exc:
        raise BrokerUnavailableError(f"IQ Option candles unavailable: {exc}") from exc
    return candles if isinstance(candles, list) else []


async def _refresh_account(session: BrokerSession) -> None:
    """Pull balance/profile/mode into session.meta (never raises)."""
    client = session.client

    def _read() -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            balances = client.get_balances()
            mode = client.get_balance_mode()
            out["mode"] = mode
            currency = None
            for b in balances.get("msg", []):
                # PRACTICE balance id is tracked in global_value.balance_id
                try:
                    import iqoptionapi.global_value as gv  # type: ignore

                    if b.get("id") == getattr(gv, "balance_id", None):
                        out["balance"] = b.get("amount")
                        currency = b.get("currency")
                except Exception:
                    pass
            out["currency"] = currency or client.get_currency()
        except Exception:
            pass
        try:
            profile = client.get_profile_ansyc()
            out["profile"] = profile
        except Exception:
            pass
        return out

    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(_read), timeout=_health_timeout() * 2
        )
    except Exception as exc:
        logger.debug("iqoption account refresh failed: %s", exc)
        return

    if data.get("balance") is not None:
        try:
            session.meta.balance = float(data["balance"])
        except (TypeError, ValueError):
            pass
    if data.get("currency"):
        session.meta.account_currency = str(data["currency"])
    if data.get("mode"):
        session.meta.account_mode = str(data["mode"])
    profile = data.get("profile")
    if isinstance(profile, dict):
        result = profile.get("result") or {}
        for key in ("id", "uid", "user_id"):
            if result.get(key) is not None:
                session.meta.account_id = str(result[key])
                break
    if session.meta.account_mode is None:
        session.meta.account_mode = "PRACTICE"


# iqoptionapi stores websocket state in module globals; serialize connects
# so two users' sessions don't interleave during the auth handshake.
_IQ_CONNECT_LOCK = asyncio.Lock()





def _connect_timeout() -> float:
    return float(os.environ.get("BROKER_CONNECT_TIMEOUT", "45"))


def _health_timeout() -> float:
    return float(os.environ.get("BROKER_HEALTH_TIMEOUT", "15"))

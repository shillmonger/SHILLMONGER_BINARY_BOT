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
    BrokerOrderError,
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


# ---------------------------------------------------------------------------
# Order placement (PRACTICE accounts only — the client is constructed with
# active_account_type="PRACTICE" and there is no code path to REAL).
# ---------------------------------------------------------------------------


def _normalize_active(asset: str, is_otc: bool) -> str:
    """Map an app asset label to an IQ Option active name.

    "EUR/USD" + OTC -> "EURUSD-OTC"; "EUR/USD" -> "EURUSD".
    """
    a = "".join(ch for ch in asset.upper() if ch.isalnum() or ch == "-")
    a = a.replace("-OTC", "")
    if is_otc:
        a += "-OTC"
    return a


# iqoptionapi stores websocket state in module-level globals — serialize
# buys/order lookups across sessions so two users' calls can't interleave.
_ORDER_LOCK = asyncio.Lock()


def _iter_records(node: Any):
    """Yield every dict inside a nested websocket payload."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _iter_records(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_records(v)


def _position_matches(rec: dict[str, Any], order_id: str, active: str, action: str) -> bool:
    rid = rec.get("id") or rec.get("option_id") or rec.get("buy_order_id")
    if rid is not None and str(rid) == str(order_id):
        return True
    rec_active = str(rec.get("active") or rec.get("active_id") or "").upper()
    rec_dir = str(rec.get("direction") or rec.get("action") or "").lower()
    # call/put must match exactly; other vocab (buy/sell) is ignored rather
    # than risking a wrong-direction match.
    dir_ok = rec_dir not in ("call", "put") or rec_dir == action
    return (
        rec_active.replace("-OTC", "") == active.replace("-OTC", "")
        and dir_ok
    )


async def _verify_open_position(
    client: Any, order_id: str, active: str, action: str
) -> Optional[dict[str, Any]]:
    """Look for the order in the broker's open-positions list.

    Returns the matching position record, or None when not found / lookup
    failed. Never raises — verification is best-effort.
    """

    def _fetch() -> Any:
        try:
            ok, msg = client.get_positions("binary-option")
        except TypeError:
            ok, msg = client.get_positions("turbo-option")
        if not ok:
            return None
        return msg

    try:
        msg = await asyncio.wait_for(
            asyncio.to_thread(_fetch), timeout=_health_timeout() * 2
        )
    except Exception as exc:
        logger.debug("iqoption position verification failed: %s", exc)
        return None
    if msg is None:
        return None
    for rec in _iter_records(msg):
        if isinstance(rec, dict) and _position_matches(rec, order_id, active, action):
            return rec
    return None


async def place_order(
    session: BrokerSession,
    asset: str,
    direction: str,
    amount: float,
    duration_minutes: int,
    is_otc: bool = False,
) -> dict[str, Any]:
    """Place a binary/turbo option order on the PRACTICE account.

    Returns a status-classified result — NEVER claims a confirmed position
    without broker-side position data:

      accepted=False, status='REJECTED'  — broker refused the request
      status='CONFIRMED'                 — order acked AND position verified
      status='ACKNOWLEDGED'              — order acked, position unverified
      status='UNCONFIRMED'               — ack ambiguous / verification failed
    """
    _require_lib()
    client = session.client
    if client is None:
        raise BrokerAuthError("No active IQ Option session")

    active = _normalize_active(asset, is_otc)
    action = "call" if str(direction).upper() in ("CALL", "BUY") else "put"
    duration = max(1, min(int(duration_minutes or 1), 60))

    # OP_code.ACTIVES is a static name->id map shipped with the library
    # (covers OTC names like EURUSD-OTC). The library's get_all_init() has an
    # internal infinite reconnect loop — never call it on the order path.
    import iqoptionapi.constants as OP_code  # type: ignore

    if active not in OP_code.ACTIVES:
        raise BrokerOrderError(f"IQ Option does not know active '{active}'")

    started = time.time()
    async with _ORDER_LOCK:
        def _buy() -> Any:
            return client.buy(float(amount), active, action, duration)

        try:
            ok, order_id = await asyncio.wait_for(
                asyncio.to_thread(_buy), timeout=_connect_timeout()
            )
        except asyncio.TimeoutError as exc:
            raise BrokerTimeoutError("IQ Option order placement timed out") from exc
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerOrderError(f"IQ Option order failed: {exc}") from exc
        logger.info(
            "iqoption buy returned user=%s active=%s ok=%s id=%s in %.1fs",
            session.user_id, active, ok, order_id, time.time() - started,
        )

        base = {
            "active": active,
            "action": action,
            "amount": float(amount),
            "durationMinutes": duration,
        }

        # buy() returns (False, <message>) on an explicit broker rejection,
        # but (False, None) when its internal 5s wait expires — and in that
        # case the order may STILL have been placed broker-side. Never
        # report that ambiguous case as REJECTED: verify positions first.
        reject_msg = (
            order_id
            if isinstance(order_id, str) and not order_id.isdigit()
            else None
        )
        if ok is False and reject_msg:
            logger.info(
                "iqoption order rejected user=%s active=%s: %s",
                session.user_id, active, reject_msg,
            )
            return {
                **base,
                "accepted": False,
                "status": "REJECTED",
                "message": f"broker rejected the order: {reject_msg}",
            }

        if order_id is None:
            # Ambiguous ack — look for a matching open position by
            # asset+direction (order_id "" never matches a record id, so
            # _position_matches falls through to the active/action check).
            position = await _verify_open_position(client, "", active, action)
            if position is not None:
                oid = (
                    position.get("id")
                    or position.get("option_id")
                    or position.get("buy_order_id")
                )
                logger.warning(
                    "iqoption ambiguous buy resolved via open position "
                    "user=%s active=%s id=%s",
                    session.user_id, active, oid,
                )
                return {
                    **base,
                    "accepted": True,
                    "orderId": str(oid) if oid is not None else None,
                    "status": "CONFIRMED",
                    "positionId": position.get("id"),
                }
            logger.warning(
                "iqoption order unconfirmed user=%s active=%s: ambiguous ack, "
                "no position found",
                session.user_id, active,
            )
            return {
                **base,
                "accepted": False,
                "status": "UNCONFIRMED",
                "message": "order acknowledgement ambiguous — verify on broker",
            }

        order_id = str(order_id)
        position = await _verify_open_position(client, order_id, active, action)
        status = "CONFIRMED" if position is not None else "ACKNOWLEDGED"
        logger.info(
            "iqoption order %s user=%s active=%s %s amount=%s dur=%sm id=%s",
            status, session.user_id, active, action, amount, duration, order_id,
        )
        return {
            **base,
            "accepted": True,
            "orderId": order_id,
            "status": status,
            "positionId": (position or {}).get("id") if position else None,
        }


def _walk_option_dicts(node: Any):
    """Yield dicts that look like option/position records."""
    if isinstance(node, dict):
        if any(k in node for k in ("id", "option_id", "active_id")) and any(
            k in node for k in ("profit", "win", "result", "value")
        ):
            yield node
        for v in node.values():
            yield from _walk_option_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_option_dicts(v)


async def get_order_result(session: BrokerSession, order_id: str) -> dict[str, Any]:
    """Classify an order using broker-side data only.

    status values:
      OPEN      — found in the broker's open-positions list
      WIN/LOSS/TIE — closed result with profit from option info
      UNKNOWN   — closed record found but result unreadable
      NOT_FOUND — neither open position nor closed record located
    """
    _require_lib()
    client = session.client
    if client is None:
        raise BrokerAuthError("No active IQ Option session")

    # 1) Open positions — is it still running? Match by order/position id.
    async with _ORDER_LOCK:
        position = await _verify_open_position(client, order_id, "*", "*")
    if position is not None:
        return {"status": "OPEN", "position": position}

    # 2) Closed options history.
    def _fetch() -> Any:
        return client.get_optioninfo_v2(5)

    async with _ORDER_LOCK:
        try:
            data = await asyncio.wait_for(
                asyncio.to_thread(_fetch), timeout=_health_timeout() * 2
            )
        except asyncio.TimeoutError as exc:
            raise BrokerTimeoutError("IQ Option order lookup timed out") from exc
        except Exception as exc:
            raise BrokerUnavailableError(f"IQ Option order lookup failed: {exc}") from exc

    target = str(order_id)
    for rec in _walk_option_dicts(data):
        rid = rec.get("id") or rec.get("option_id")
        if rid is None or str(rid) != target:
            continue
        profit = rec.get("profit")
        try:
            profit_f = float(profit) if profit is not None else None
        except (TypeError, ValueError):
            profit_f = None
        status = "UNKNOWN"
        if profit_f is not None:
            status = "WIN" if profit_f > 0 else ("LOSS" if profit_f < 0 else "TIE")
        elif rec.get("win") in ("win", "loose", "equal"):
            status = {"win": "WIN", "loose": "LOSS", "equal": "TIE"}[str(rec["win"])]
        return {
            "status": status,
            "profit": profit_f,
            "raw": {k: rec.get(k) for k in ("id", "active", "direction", "exp", "profit", "value", "win") if k in rec},
        }
    return {"status": "NOT_FOUND"}


# iqoptionapi stores websocket state in module globals; serialize connects
# so two users' sessions don't interleave during the auth handshake.
_IQ_CONNECT_LOCK = asyncio.Lock()





def _connect_timeout() -> float:
    return float(os.environ.get("BROKER_CONNECT_TIMEOUT", "45"))


def _health_timeout() -> float:
    return float(os.environ.get("BROKER_HEALTH_TIMEOUT", "15"))

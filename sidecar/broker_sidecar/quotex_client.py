"""Quotex session wrapper around the community `pyquotex` client.

pyquotex (https://github.com/cleitonleonel/pyquotex) reproduces the broker's
web protocol over WebSocket. It is UNOFFICIAL — the broker can change or
block it at any time, and automated logins carry ToS/ban risk. Use DEMO
(PRACTICE) accounts only.

Account mode is hard-pinned to PRACTICE inside this wrapper — the sidecar
API rejects REAL at the route layer too, so a real-money session cannot be
created through this service.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from typing import Any, Optional

from .errors import (
    BrokerAuthError,
    BrokerOtpRequired,
    BrokerTimeoutError,
    BrokerUnavailableError,
)
from .sessions import BrokerSession

logger = logging.getLogger("broker_sidecar.quotex")

try:
    from pyquotex.stable_api import Quotex  # type: ignore

    _HAVE_PYQUOTEX = True
except Exception:  # pragma: no cover - depends on env
    Quotex = None  # type: ignore
    _HAVE_PYQUOTEX = False


def _require_lib() -> None:
    if not _HAVE_PYQUOTEX:
        raise BrokerUnavailableError(
            "pyquotex is not installed — run `pip install -r sidecar/requirements.txt`"
        )


def _host() -> Optional[str]:
    return os.environ.get("QUOTEX_HOST") or None


def _proxies() -> Optional[dict[str, str]]:
    """Optional egress proxy for Quotex — datacenter IPs are frequently
    Cloudflare-challenged, so a residential proxy may be required."""
    proxy = (os.environ.get("QUOTEX_PROXY") or "").strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _otp_callback(session: BrokerSession):
    """pyquotex invokes this when the broker demands a 2FA code.

    Returns the code the user supplied via the /otp endpoint, or raises
    BrokerOtpRequired so the API can ask the frontend for one.
    """

    def _cb(*_args: Any, **_kwargs: Any) -> str:
        code = session.consume_otp()
        if not code:
            raise BrokerOtpRequired()
        return code

    return _cb


async def connect(session: BrokerSession, otp: Optional[str] = None) -> None:
    """Authenticate + open the Quotex websocket for a PRACTICE account."""
    _require_lib()
    if otp:
        session.set_otp(otp)

    profile_dir = tempfile.mkdtemp(prefix=f"qx-{session.user_id}-")
    client = Quotex(
        session.email,
        session.password,
        host=_host(),
        proxies=_proxies(),
        lang="en",
        user_data_dir=profile_dir,
        on_otp_callback=_otp_callback(session),
    )
    # Hard-pin DEMO — REAL must never be reachable through the sidecar.
    client.set_account_mode("PRACTICE")
    session.client = client

    started = time.time()
    try:
        ok, reason = await asyncio.wait_for(
            client.connect(), timeout=_connect_timeout()
        )
    except asyncio.TimeoutError as exc:
        raise BrokerTimeoutError("Quotex connect timed out") from exc
    except BrokerOtpRequired:
        session.meta.requires_otp = True
        raise
    except Exception as exc:
        raise BrokerAuthError(f"Quotex authentication failed: {exc}") from exc

    if not ok:
        if "otp" in str(reason).lower() or "2fa" in str(reason).lower():
            session.meta.requires_otp = True
            raise BrokerOtpRequired()
        raise BrokerAuthError(f"Quotex authentication failed: {reason}")

    # Verify independently: socket open is not enough — the library reports
    # AuthStatus.AUTHENTICATED only after the broker accepted the session.
    verified = await asyncio.wait_for(
        client.check_connect(), timeout=_health_timeout()
    )
    if not verified:
        raise BrokerAuthError("Quotex session could not be verified")

    await _refresh_account(session)
    session.meta.connected = True
    session.meta.connected_at = time.time()
    session.meta.last_check = time.time()
    session.meta.latency_ms = round((time.time() - started) * 1000, 1)
    session.meta.last_error = None
    logger.info("quotex session connected user=%s", session.user_id)


async def verify(session: BrokerSession) -> dict[str, Any]:
    """Health check: websocket alive + balance fetch."""
    _require_lib()
    client = session.client
    if client is None:
        raise BrokerAuthError("No active Quotex session")
    started = time.time()
    alive = await asyncio.wait_for(client.check_connect(), timeout=_health_timeout())
    if not alive:
        session.meta.connected = False
        session.meta.last_error = "Websocket disconnected"
        return {"ok": False, "error": "Websocket disconnected"}
    try:
        await _refresh_account(session)
    except Exception as exc:  # balance fetch failed but socket is alive
        session.meta.last_error = str(exc)[:200]
    session.meta.last_check = time.time()
    session.meta.latency_ms = round((time.time() - started) * 1000, 1)
    session.meta.connected = True
    return {"ok": True, "latencyMs": session.meta.latency_ms}


async def disconnect(session: BrokerSession) -> None:
    client = session.client
    session.client = None
    if client is not None:
        try:
            await asyncio.wait_for(client.close(), timeout=_health_timeout())
        except Exception:
            logger.debug("quotex close error (ignored)", exc_info=True)
    session.meta.connected = False
    session.meta.requires_otp = False
    session._otp_code = None
    session.meta.last_error = None
    logger.info("quotex session disconnected user=%s", session.user_id)


async def get_candles(
    session: BrokerSession, asset: str, period: int = 60, count: int = 100
) -> list[dict[str, Any]]:
    """Recent candles for an asset. Best-effort — depends on pyquotex API."""
    _require_lib()
    client = session.client
    if client is None:
        raise BrokerAuthError("No active Quotex session")
    import time as _t

    end_from_time = int(_t.time())
    offset = period * max(count, 1)
    try:
        candles = await asyncio.wait_for(
            client.get_candles(asset, end_from_time, offset, period),
            timeout=_health_timeout() * 2,
        )
    except asyncio.TimeoutError as exc:
        raise BrokerTimeoutError("Quotex candles timed out") from exc
    except AttributeError as exc:
        raise BrokerUnavailableError(
            "pyquotex version does not expose get_candles"
        ) from exc
    return candles if isinstance(candles, list) else []


async def _refresh_account(session: BrokerSession) -> None:
    """Pull balance/profile into session.meta (never raises for missing data)."""
    client = session.client
    try:
        balance = await asyncio.wait_for(
            client.get_balance(), timeout=_health_timeout()
        )
        if isinstance(balance, (int, float)):
            session.meta.balance = float(balance)
    except Exception as exc:
        logger.debug("quotex balance fetch failed: %s", exc)

    try:
        profile = await asyncio.wait_for(
            client.get_profile(), timeout=_health_timeout()
        )
        _apply_profile(session, profile)
    except Exception as exc:
        logger.debug("quotex profile fetch failed: %s", exc)

    # Fallbacks if the profile object didn't yield an id/currency.
    session.meta.account_mode = "PRACTICE"
    if session.meta.balance is not None and session.meta.account_currency is None:
        session.meta.account_currency = "USD"


def _apply_profile(session: BrokerSession, profile: Any) -> None:
    """Extract safe account fields from pyquotex's profile object."""
    if profile is None:
        return
    candidates = {
        "account_id": ("id", "user_id", "uid", "account_id"),
        "account_currency": ("currency", "currency_code"),
    }
    for attr, names in candidates.items():
        for name in names:
            value = getattr(profile, name, None)
            if value is None and isinstance(profile, dict):
                value = profile.get(name)
            if value is not None:
                setattr(session.meta, attr, str(value))
                break


def _connect_timeout() -> float:
    return float(os.environ.get("BROKER_CONNECT_TIMEOUT", "45"))


def _health_timeout() -> float:
    return float(os.environ.get("BROKER_HEALTH_TIMEOUT", "15"))

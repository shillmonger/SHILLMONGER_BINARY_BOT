"""Broker sidecar — internal HTTP API for Quotex + IQ Option sessions.

This service wraps the community broker clients (pyquotex, iqoptionapi)
and exposes a small authenticated JSON API consumed ONLY by the Next.js
app's broker adapters (lib/binary-brokers/adapters/sidecar.ts).

Security model:
  * Bind to localhost; never expose publicly.
  * Every /v1/* request must carry `X-Broker-Key: <BROKER_SIDECAR_SECRET>`.
  * Credentials transit only over localhost HTTP and live in memory for the
    session lifetime — never logged, never returned by any endpoint.
  * DEMO/PRACTICE only: REAL/LIVE account modes are rejected with 400.

Run:  python -m broker_sidecar.main   (from BINARY_TRADING_BOTS/sidecar)
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

try:
    from dotenv import load_dotenv

    _env_base = Path(__file__).resolve().parents[1]
    # Real env vars always win (override=False). sidecar/.env is loaded first
    # so its values take precedence over the parent package .env for the same
    # key; the parent .env supplies shared values like API_ID/API_HASH.
    load_dotenv(_env_base / ".env")
    load_dotenv(_env_base.parent / ".env")
except ImportError:  # python-dotenv optional — env vars still work
    pass

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from . import (
    agent_tunnel,
    iqoption_client,
    message_listener,
    quotex_client,
    telegram_service,
)
from .errors import (
    BrokerAuthError,
    BrokerError,
    BrokerOtpRequired,
    BrokerTimeoutError,
    BrokerUnavailableError,
)
from .sessions import BrokerSession, store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("broker_sidecar")

BROKERS = {
    "quotex": quotex_client,
    "iqoption": iqoption_client,
}

DEMO_MODES = {"DEMO", "PRACTICE"}
REAL_MODES = {"REAL", "LIVE"}


def _secret() -> str:
    return os.environ.get("BROKER_SIDECAR_SECRET", "")


async def require_key(x_broker_key: Optional[str] = Header(default=None)) -> None:
    secret = _secret()
    if not secret:
        # Fail closed: no secret configured -> nothing is allowed.
        raise HTTPException(status_code=503, detail="sidecar secret not configured")
    if x_broker_key is None or not secrets.compare_digest(
        x_broker_key.encode("utf-8"), secret.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="invalid broker key")


# ---------------------------------------------------------------- models


class ConnectBody(BaseModel):
    userId: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)
    accountMode: str = "DEMO"
    otp: Optional[str] = Field(default=None, max_length=32)


class OtpBody(BaseModel):
    userId: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=4, max_length=16)


class UserBody(BaseModel):
    userId: str = Field(min_length=1, max_length=64)


class ConnectResponse(BaseModel):
    ok: bool
    connected: bool = False
    requiresOtp: bool = False
    account: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    code: Optional[str] = None
    latencyMs: Optional[float] = None


# ---------------------------------------------------------------- helpers


def _broker(name: str):
    mod = BROKERS.get(name)
    if mod is None:
        raise HTTPException(status_code=404, detail=f"unknown broker '{name}'")
    return mod


def _check_mode(mode: str) -> None:
    m = (mode or "DEMO").upper()
    if m in REAL_MODES:
        raise HTTPException(
            status_code=400,
            detail="REAL/LIVE account mode is not permitted — DEMO/PRACTICE only",
        )
    if m not in DEMO_MODES:
        raise HTTPException(status_code=400, detail=f"unsupported accountMode '{mode}'")


def _get_or_create_session(body: ConnectBody, broker: str) -> BrokerSession:
    existing = store.get(broker, body.userId)
    if existing and existing.meta.connected:
        # Reconnecting an already-live session: drop it first so the broker
        # gets a fresh handshake rather than a duplicate login.
        return existing
    session = BrokerSession(broker, body.userId, body.email.strip().lower(), body.password)
    store.put(session)
    return session


def _err(exc: BrokerError) -> ConnectResponse:
    return ConnectResponse(
        ok=False,
        connected=False,
        requiresOtp=isinstance(exc, BrokerOtpRequired),
        error=str(exc),
        code=exc.code,
    )


async def _run_connect(broker: str, session: BrokerSession, otp: Optional[str]) -> ConnectResponse:
    mod = _broker(broker)
    try:
        await mod.connect(session, otp=otp)
    except BrokerOtpRequired as exc:
        return _err(exc)
    except BrokerError as exc:
        return _err(exc)
    except Exception as exc:  # library internals raise arbitrary types
        logger.exception("%s connect crashed user=%s", broker, session.user_id)
        return ConnectResponse(ok=False, error=str(exc)[:300], code="error")

    return ConnectResponse(
        ok=True,
        connected=True,
        account=session.meta.to_dict()["account"],
        latencyMs=session.meta.latency_ms,
    )


# ---------------------------------------------------------------- app


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("broker sidecar starting (secret %s)", "set" if _secret() else "MISSING")
    await telegram_service.auto_start_if_enabled()
    await message_listener.auto_start_listener_if_enabled()
    yield
    # Graceful shutdown: stop the listener, then disconnect telegram +
    # every live broker session.
    try:
        await message_listener.listener.stop()
    except Exception:
        logger.debug("listener shutdown failed", exc_info=True)
    try:
        await telegram_service.tg.disconnect()
    except Exception:
        logger.debug("telegram shutdown disconnect failed", exc_info=True)
    # Graceful shutdown: disconnect every live session.
    for key in store.list_keys():
        broker, _, user_id = key.partition(":")
        session = store.drop(broker, user_id)
        if session:
            try:
                await BROKERS[broker].disconnect(session)
            except Exception:
                logger.debug("shutdown disconnect failed for %s", key, exc_info=True)


app = FastAPI(title="broker-sidecar", version="1.0.0", lifespan=lifespan)
app.include_router(agent_tunnel.router)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness + which broker libraries are importable. No secrets."""
    libs = {}
    for name, mod in BROKERS.items():
        libs[name] = {
            "available": getattr(mod, "_HAVE_PYQUOTEX", getattr(mod, "_HAVE_IQ", False)),
            "sessions": sum(1 for k in store.list_keys() if k.startswith(f"{name}:")),
        }
    return {"ok": True, "time": time.time(), "brokers": libs}


# Register fixed Telegram paths before /v1/{broker}/... routes. Otherwise
# Starlette matches /v1/telegram/status as broker="telegram", action="status".
@app.get("/v1/telegram/status", dependencies=[Depends(require_key)])
async def tg_status() -> dict[str, Any]:
    return await telegram_service.tg.get_status()


@app.post("/v1/telegram/connect", dependencies=[Depends(require_key)])
async def tg_connect() -> dict[str, Any]:
    return await telegram_service.tg.connect()


@app.post("/v1/telegram/disconnect", dependencies=[Depends(require_key)])
async def tg_disconnect() -> dict[str, Any]:
    return await telegram_service.tg.disconnect()


@app.get("/v1/telegram/groups", dependencies=[Depends(require_key)])
async def tg_groups() -> dict[str, Any]:
    try:
        return await telegram_service.tg.fetch_groups()
    except telegram_service.TelegramFloodWait as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": "flood_wait", "waitSeconds": exc.wait_seconds, "error": str(exc)},
        ) from exc
    except telegram_service.TelegramError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ------------------------------------------------- message listener (Phase 3)
# Ingestion only — the listener forwards raw Telegram events to the Next.js
# backend. No signal parsing and no trade execution happens here.


@app.get("/v1/telegram/listener/status", dependencies=[Depends(require_key)])
async def listener_status() -> dict[str, Any]:
    return message_listener.listener.to_dict()


@app.post("/v1/telegram/listener/start", dependencies=[Depends(require_key)])
async def listener_start() -> dict[str, Any]:
    return await message_listener.listener.start()


@app.post("/v1/telegram/listener/stop", dependencies=[Depends(require_key)])
async def listener_stop() -> dict[str, Any]:
    return await message_listener.listener.stop()


@app.post("/v1/telegram/listener/restart", dependencies=[Depends(require_key)])
async def listener_restart() -> dict[str, Any]:
    return await message_listener.listener.restart()


# ------------------------------------------------------- Windows agent tunnel
# The agent dials OUT to /v1/agent/tunnel (WebSocket). These HTTP routes are
# for the Next.js dashboard/adapter only and require X-Broker-Key like every
# other /v1/* endpoint. They expose status and two fixed verbs (PING, RESTART)
# — never arbitrary commands.


@app.get("/v1/agent/status", dependencies=[Depends(require_key)])
async def agent_status() -> dict[str, Any]:
    return agent_tunnel.manager.status()


@app.post("/v1/agent/ping", dependencies=[Depends(require_key)])
async def agent_ping() -> dict[str, Any]:
    session = agent_tunnel.manager.session
    if session is None:
        raise HTTPException(status_code=503, detail="agent tunnel offline")
    try:
        result = await session.request("PING", timeout=10)
    except Exception as exc:
        raise HTTPException(status_code=504, detail=f"agent ping failed: {exc}") from exc
    return {"ok": True, "result": result.get("result", result)}


@app.post("/v1/agent/restart", dependencies=[Depends(require_key)])
async def agent_restart() -> dict[str, Any]:
    """Ask the agent to gracefully restart itself (fixed RESTART verb)."""
    session = agent_tunnel.manager.session
    if session is None:
        raise HTTPException(status_code=503, detail="agent tunnel offline")
    try:
        result = await session.request("RESTART", timeout=10)
    except Exception as exc:
        raise HTTPException(status_code=504, detail=f"agent restart failed: {exc}") from exc
    return {"ok": True, "result": result.get("result", result)}


@app.post("/v1/{broker}/connect", response_model=ConnectResponse, dependencies=[Depends(require_key)])
async def connect(broker: str, body: ConnectBody) -> ConnectResponse:
    mod = _broker(broker)
    _check_mode(body.accountMode)
    session = _get_or_create_session(body, broker)
    async with session.lock:
        return await _run_connect(broker, session, body.otp)


@app.post("/v1/{broker}/otp", response_model=ConnectResponse, dependencies=[Depends(require_key)])
async def otp(broker: str, body: OtpBody) -> ConnectResponse:
    """Complete a 2FA handshake on a pending session."""
    session = store.get(broker, body.userId)
    if session is None:
        raise HTTPException(status_code=404, detail="no pending session — connect first")
    async with session.lock:
        try:
            # Both clients accept a re-connect with the OTP: quotex consumes
            # it via on_otp_callback; iqoption calls connect_2fa.
            session._otp_code = body.code
            return await _run_connect(broker, session, body.code)
        finally:
            session._otp_code = None


@app.post("/v1/{broker}/disconnect", dependencies=[Depends(require_key)])
async def disconnect(broker: str, body: UserBody) -> dict[str, Any]:
    mod = _broker(broker)
    session = store.drop(broker, body.userId)
    if session is None:
        return {"ok": True, "connected": False, "message": "no active session"}
    async with session.lock:
        try:
            await mod.disconnect(session)
        except Exception as exc:
            logger.warning("%s disconnect error: %s", broker, exc)
    return {"ok": True, "connected": False}


@app.get("/v1/{broker}/status", dependencies=[Depends(require_key)])
async def status(broker: str, userId: str = Query(...)) -> dict[str, Any]:
    _broker(broker)
    session = store.get(broker, userId)
    if session is None:
        return {
            "connected": False,
            "account": None,
            "requiresOtp": False,
            "lastError": None,
        }
    return session.meta.to_dict() | {"connected": session.meta.connected}


@app.post("/v1/{broker}/verify", dependencies=[Depends(require_key)])
async def verify(broker: str, body: UserBody) -> dict[str, Any]:
    mod = _broker(broker)
    session = store.get(broker, body.userId)
    if session is None or session.client is None:
        return {"ok": False, "error": "no active session", "connected": False}
    async with session.lock:
        try:
            result = await mod.verify(session)
        except BrokerError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code, "connected": False}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300], "connected": False}
    return {
        "ok": bool(result.get("ok")),
        "latencyMs": result.get("latencyMs"),
        "account": session.meta.to_dict()["account"],
        "connected": session.meta.connected,
        "lastError": session.meta.last_error,
    }


@app.get("/v1/{broker}/candles", dependencies=[Depends(require_key)])
async def candles(
    broker: str,
    userId: str = Query(...),
    asset: str = Query(default="EURUSD"),
    period: int = Query(default=60, ge=1, le=86400),
    count: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    mod = _broker(broker)
    session = store.get(broker, userId)
    if session is None or session.client is None:
        raise HTTPException(status_code=404, detail="no active session")
    async with session.lock:
        try:
            if broker == "quotex":
                data = await mod.get_candles(session, asset, period=period, count=count)
            else:
                data = await mod.get_candles(session, asset, interval=period, count=count)
        except BrokerError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "asset": asset, "period": period, "candles": data}


def main() -> None:
    import uvicorn

    # Platforms like Altare/Render inject PORT and route traffic to it; honor
    # BROKER_SIDECAR_* overrides first, then PORT, then the local default.
    host = os.environ.get("BROKER_SIDECAR_HOST") or (
        "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    )
    port = int(os.environ.get("BROKER_SIDECAR_PORT") or os.environ.get("PORT") or "8790")
    uvicorn.run("broker_sidecar.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()

"""Loopback-only health/diagnostics HTTP endpoints.

Binds 127.0.0.1 (enforced again here, independent of config validation).
Endpoints:
  GET /health       liveness — 200 while the process serves
  GET /ready        readiness — 200 only in CONNECTED state
  GET /status       full state snapshot (token required if configured)
  GET /diagnostics  local checks summary (token required if configured)
"""

from __future__ import annotations

import hmac
import logging
import time
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException

from .config import AgentConfig
from .diagnostics import diagnostics_summary, run_diagnostics
from .state import AgentState, StateMachine

logger = logging.getLogger("quotex_agent.health")

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def create_app(
    config: AgentConfig,
    state: StateMachine,
    *,
    started_at: Optional[float] = None,
    heartbeat=None,
    diagnostics_extra=None,
) -> FastAPI:
    if config.health_host not in _LOOPBACK:
        raise ValueError(
            f"refusing to bind health server on non-loopback host "
            f"{config.health_host!r}"
        )
    started = started_at if started_at is not None else time.time()
    app = FastAPI(title="SHILLMONGER Windows Agent", docs_url=None,
                  redoc_url=None, openapi_url=None)

    def _authorized(x_agent_token: Optional[str] = Header(default=None)) -> None:
        if not config.local_token:
            return
        if x_agent_token is None or not hmac.compare_digest(
            x_agent_token.encode(), config.local_token.encode()
        ):
            raise HTTPException(status_code=401, detail="invalid agent token")

    @app.get("/health")
    async def health():
        return {
            "ok": True,
            "service": "quotex-agent",
            "agentId": config.agent_id,
            "version": config.version,
            "state": state.state.value,
            "uptimeSeconds": round(time.time() - started, 1),
        }

    @app.get("/ready")
    async def ready():
        if state.state is not AgentState.CONNECTED:
            raise HTTPException(status_code=503,
                                detail=f"not ready: {state.state.value}")
        return {"ok": True, "state": state.state.value}

    @app.get("/status", dependencies=[Depends(_authorized)])
    async def status():
        snap = state.snapshot()
        return {
            "service": "quotex-agent",
            "agentId": config.agent_id,
            "version": config.version,
            "uptimeSeconds": round(time.time() - started, 1),
            "state": snap,
            "heartbeat": heartbeat.status() if heartbeat else None,
            "tunnelEnabled": config.tunnel_enabled,
        }

    @app.get("/diagnostics", dependencies=[Depends(_authorized)])
    async def diagnostics():
        checks = run_diagnostics(config, include_port=False,
                                 extra=diagnostics_extra)
        return diagnostics_summary(checks)

    return app

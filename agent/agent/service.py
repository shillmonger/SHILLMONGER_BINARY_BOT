"""Agent service orchestrator — startup, shutdown, state, heartbeat,
health server. Phase B scope: infrastructure only, no broker clients,
no tunnel. The tunnel connector plugs in here in Phase C.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Optional

import uvicorn

from .config import AgentConfig
from .diagnostics import diagnostics_summary, run_diagnostics
from .health_server import create_app
from .heartbeat import Heartbeat
from .state import AgentState, StateMachine
from .tunnel import TunnelClient

logger = logging.getLogger("quotex_agent.service")


class AgentService:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.state = StateMachine()
        self.started_at = time.time()
        self.heartbeat = Heartbeat(config.heartbeat_seconds)
        self._server: Optional[uvicorn.Server] = None
        self._server_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._diagnostics_cache: Optional[dict] = None
        self._diagnostics_at = 0.0
        self.tunnel: Optional[TunnelClient] = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start health server + heartbeat, then mark CONNECTED."""
        if not self.state.transition(AgentState.STARTING, "service start"):
            logger.warning("start ignored — state=%s", self.state.state.value)
            return
        try:
            self.state_dir_prepare()
            await self._start_health_server()
            self._start_tunnel_if_enabled()
            await self.heartbeat.start()
            checks = run_diagnostics(self.config)
            self._diagnostics_cache = diagnostics_summary(checks)
            self._diagnostics_at = time.time()
            failed = [c["name"] for c in self._diagnostics_cache["checks"] if not c["ok"]]
            if failed:
                logger.warning("diagnostics degraded: %s", ",".join(failed))
                # Non-fatal: health endpoints still serve diagnostics.
            self.state.transition(
                AgentState.DISCONNECTED if self.config.tunnel_enabled
                else AgentState.CONNECTED,
                "awaiting tunnel" if self.config.tunnel_enabled
                else "core loops healthy",
            )
            logger.info("agent started id=%s bind=%s",
                        self.config.agent_id, self.config.health_bind)
        except Exception as exc:
            self.state.fail(f"startup failed: {exc}")
            logger.error("startup failed: %s", exc)
            raise

    def state_dir_prepare(self) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)

    def _start_tunnel_if_enabled(self) -> None:
        if not self.config.tunnel_enabled:
            return
        self.tunnel = TunnelClient(
            self.config,
            self.state,
            on_restart=self.request_stop,
        )
        # Heartbeats ride the existing Heartbeat loop when the tunnel is up.
        self.heartbeat._on_beat = self._beat_to_tunnel
        self.tunnel.start()

    async def _beat_to_tunnel(self) -> None:
        if self.tunnel is not None and self.tunnel.connected:
            await self.tunnel.send_event("HEARTBEAT", {
                "agentId": self.config.agent_id,
                "version": self.config.version,
                "state": self.state.state.value,
            })

    async def _start_health_server(self) -> None:
        app = create_app(
            self.config,
            self.state,
            started_at=self.started_at,
            heartbeat=self.heartbeat,
        )
        config = uvicorn.Config(
            app,
            host=self.config.health_host,
            port=self.config.health_port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(
            self._server.serve(), name="health-server"
        )
        # Wait until the socket is actually accepting.
        for _ in range(100):
            if self._server.started:
                return
            if self._server_task.done():
                raise RuntimeError("health server exited during startup")
            await asyncio.sleep(0.05)
        raise RuntimeError("health server did not start in time")

    async def stop(self) -> None:
        """Graceful shutdown: heartbeat -> health server -> STOPPED."""
        logger.info("shutdown requested state=%s", self.state.state.value)
        try:
            await self.heartbeat.stop()
        except Exception:
            logger.debug("heartbeat stop error (ignored)", exc_info=True)
        if self.tunnel is not None:
            try:
                await self.tunnel.stop()
            except Exception:
                logger.debug("tunnel stop error (ignored)", exc_info=True)
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            try:
                await asyncio.wait_for(self._server_task, timeout=10)
            except (asyncio.TimeoutError, Exception):
                self._server_task.cancel()
        self.state.transition(AgentState.STOPPED, "shutdown")
        logger.info("agent stopped")

    def request_stop(self) -> None:
        self._stop_event.set()

    async def wait(self) -> None:
        await self._stop_event.wait()

    # -------------------------------------------------------------- status

    def status_snapshot(self) -> dict:
        snap = self.state.snapshot()
        checks = run_diagnostics(self.config)
        return {
            "service": "quotex-agent",
            "agentId": self.config.agent_id,
            "version": self.config.version,
            "uptimeSeconds": round(time.time() - self.started_at, 1),
            "state": snap,
            "heartbeat": self.heartbeat.status(),
            "diagnostics": diagnostics_summary(checks),
            "tunnelEnabled": self.config.tunnel_enabled,
            "bind": self.config.health_bind,
        }


async def run_service(config: AgentConfig) -> int:
    """Entry coroutine: install signal handlers, start, wait, stop."""
    service = AgentService(config)
    loop = asyncio.get_running_loop()

    def _on_signal(*_args):
        logger.info("termination signal received")
        service.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, RuntimeError):
            # Windows: add_signal_handler may be unavailable for some signals;
            # fall back to signal.signal below.
            try:
                signal.signal(sig, lambda *_a: service.request_stop())
            except (ValueError, OSError):
                pass

    try:
        await service.start()
        await service.wait()
    finally:
        await service.stop()
    return 0

"""Outbound WSS/WS tunnel client — the agent's only network surface.

Dials OUT to the VPS sidecar (/v1/agent/tunnel), performs the HMAC AUTH
handshake, then exchanges signed envelopes. Reconnects forever with
capped exponential backoff; auth rejection is fatal (bad secret needs an
operator, not retries).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from . import auth as authmod
from .config import AgentConfig
from .protocol import (
    MAX_FRAME_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    TIMESTAMP_SKEW_SECONDS,
    AuthPayload,
    Envelope,
    ErrorCode,
    ProtocolError,
    Verb,
    build_envelope,
    error_payload,
    new_correlation_id,
    parse_envelope,
    verb_allowed,
    verify_frame,
)
from .retry import backoff_delay
from .state import AgentState, StateMachine

logger = logging.getLogger("quotex_agent.tunnel")

CONNECT_TIMEOUT = 15.0
AUTH_TIMEOUT = 10.0
# Inbound rate limit: token bucket, 2 msg/s sustained, burst 20.
RATE_BURST = 20
RATE_PER_SECOND = 2.0
# A session that stayed up this long resets the backoff attempt counter.
STABLE_SESSION_SECONDS = 60.0


class FatalAuthError(Exception):
    """AUTH rejected — retrying cannot fix a wrong shared secret."""


class _RateLimiter:
    def __init__(self, burst: int, per_second: float) -> None:
        self._tokens = float(burst)
        self._burst = float(burst)
        self._rate = per_second
        self._last = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        self._tokens = min(self._burst,
                           self._tokens + (now - self._last) * self._rate)
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


def ws_url(config: AgentConfig) -> str:
    base = config.vps_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return f"{base}/v1/agent/tunnel"


class TunnelClient:
    """Managed tunnel session with reconnect loop."""

    def __init__(
        self,
        config: AgentConfig,
        state: StateMachine,
        *,
        on_connected: Optional[Callable[[], None]] = None,
        on_disconnected: Optional[Callable[[str], None]] = None,
        on_restart: Optional[Callable[[], None]] = None,
    ) -> None:
        self.config = config
        self.state = state
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._on_restart = on_restart
        self._ws: Any = None
        self._session_token: Optional[str] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._limiter = _RateLimiter(RATE_BURST, RATE_PER_SECOND)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._connected_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.frames_in = 0
        self.frames_out = 0
        self.reconnects = 0

    # ------------------------------------------------------------ lifecycle

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._session_token is not None

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="tunnel")

    async def stop(self) -> None:
        self._stop.set()
        await self._close_ws(code=1000, reason="shutdown")
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ProtocolError(ErrorCode.UNAVAILABLE,
                                                "tunnel closed"))
        self._pending.clear()

    async def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            started = time.time()
            try:
                await self._session()
            except FatalAuthError as exc:
                self.last_error = str(exc)
                self.state.fail(str(exc))
                logger.error("tunnel auth rejected — stopping retries: %s", exc)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)[:300]
                logger.warning("tunnel session ended: %s", self.last_error)
            finally:
                await self._close_ws()
                self._session_token = None
                self._connected_at = None
            if self._stop.is_set():
                break
            if time.time() - started >= STABLE_SESSION_SECONDS:
                attempt = 0
            else:
                attempt += 1
                self.reconnects += 1
            if self.state.state is AgentState.CONNECTED:
                self._notify_disconnected(self.last_error or "tunnel dropped")
            delay = backoff_delay(
                attempt,
                base_delay=self.config.reconnect_base_delay,
                max_delay=self.config.reconnect_max_delay,
            )
            logger.info("tunnel reconnect in %.1fs (attempt %d)", delay, attempt)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------- session

    async def _session(self) -> None:
        url = ws_url(self.config)
        async with websockets.connect(
            url,
            open_timeout=CONNECT_TIMEOUT,
            ping_interval=20,
            ping_timeout=20,
            max_size=MAX_FRAME_BYTES + 1024,
        ) as ws:
            self._ws = ws
            try:
                await self._authenticate()
                self._connected_at = time.time()
                self.last_error = None
                self._notify_connected()
                await self._recv_loop(ws)
            finally:
                self._ws = None

    async def _authenticate(self) -> None:
        auth_msg, ts, nonce = authmod.build_auth_message(self.config.agent_id)
        signature = authmod.sign(self.config.shared_secret, auth_msg)
        payload = AuthPayload(
            agentId=self.config.agent_id,
            timestamp=int(ts),
            nonce=nonce,
            signature=signature,
        ).model_dump()
        frame_id = new_correlation_id()
        env = build_envelope("AUTH", payload, secret=self.config.shared_secret,
                             frame_id=frame_id)
        # AUTH is signed with the shared secret via build_envelope; the
        # payload.signature additionally binds agentId.ts.nonce.
        await self._send_raw(env)
        raw = await asyncio.wait_for(ws_recv(self._ws), timeout=AUTH_TIMEOUT)
        env_in = parse_envelope(raw)
        if env_in.type != "AUTH_OK":
            raise FatalAuthError(
                f"unexpected auth response type {env_in.type}")
        token = env_in.payload.get("sessionToken")
        if not isinstance(token, str) or len(token) < 32:
            raise FatalAuthError("AUTH_OK missing sessionToken")
        # Verify AUTH_OK signature with the shared secret.
        if not verify_frame(self.config.shared_secret, env_in.ts, env_in.id,
                            env_in.type, env_in.payload, env_in.sig):
            raise FatalAuthError("AUTH_OK signature invalid")
        self._session_token = token
        logger.info("tunnel authenticated agent=%s", self.config.agent_id)

    async def _recv_loop(self, ws: Any) -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
                await self._send_error(new_correlation_id(),
                                       ErrorCode.MALFORMED, "frame too large")
                continue
            try:
                env = parse_envelope(raw)
            except ProtocolError as exc:
                await self._send_error(new_correlation_id(), exc.code,
                                       exc.message)
                continue
            if env.type == "AUTH_OK":
                continue
            try:
                await self._handle_frame(env)
            except ProtocolError as exc:
                await self._send_error(env.id, exc.code, exc.message)
            except Exception as exc:  # noqa: BLE001 - isolate frame errors
                logger.warning("frame handling error: %s", exc)
                await self._send_error(env.id, ErrorCode.INTERNAL,
                                       "internal error")

    # ------------------------------------------------------------- frames

    async def _handle_frame(self, env: Envelope) -> None:
        if not self._limiter.allow():
            raise ProtocolError(ErrorCode.RATE_LIMITED, "rate limit exceeded")
        if not verb_allowed("sidecar_to_agent", env.type):
            raise ProtocolError(ErrorCode.UNKNOWN_TYPE, f"verb {env.type} not allowed")
        if not verify_frame(
            self._session_token or "", env.ts, env.id, env.type,
            env.payload, env.sig,
        ):
            raise ProtocolError(ErrorCode.UNAUTHORIZED, "bad frame signature")
        self.frames_in += 1
        # Frames carrying result/error are RESPONSES to our requests.
        if "result" in env.payload or "error" in env.payload:
            self.resolve_pending(env.id, env.payload)
            return
        verb = Verb(env.type)
        if verb is Verb.PING:
            await self._respond(env, {"result": {"pong": True}})
        elif verb in (Verb.STATUS, Verb.HEALTH):
            await self._respond(env, self._local_status(verb))
        elif verb is Verb.RESTART:
            await self._respond(env, {"result": {"restarting": True}})
            if self._on_restart is not None:
                self._on_restart()

    def _local_status(self, verb: Verb) -> dict:
        if verb is Verb.HEALTH:
            return {"result": {"ok": True, "state": self.state.state.value,
                               "agentId": self.config.agent_id,
                               "version": self.config.version}}
        return {"result": {
            "agentId": self.config.agent_id,
            "version": self.config.version,
            "state": self.state.state.value,
            "uptimeSeconds": (
                round(time.time() - self._connected_at, 1)
                if self._connected_at else None),
            "tunnel": {
                "connected": self.connected,
                "reconnects": self.reconnects,
                "framesIn": self.frames_in,
                "framesOut": self.frames_out,
                "lastError": self.last_error,
            },
        }}

    async def _respond(self, req: Envelope, payload: dict) -> None:
        body = build_envelope(req.type, payload,
                              secret=self._session_token or "",
                              frame_id=req.id)
        await self._send_raw(body)

    async def _send_error(self, frame_id: str, code: ErrorCode,
                          message: str) -> None:
        try:
            body = build_envelope(
                "ERROR", error_payload(code, message, frame_id),
                secret=self._session_token or "",
                frame_id=frame_id,
            )
            await self._send_raw(body)
        except Exception:
            pass

    async def send_event(self, verb: str,
                         payload: Optional[dict] = None) -> bool:
        """Fire-and-forget agent->sidecar event (HEARTBEAT etc.)."""
        if not self.connected:
            return False
        try:
            body = build_envelope(verb, payload or {},
                                  secret=self._session_token or "")
            await self._send_raw(body)
            self.frames_out += 1
            return True
        except Exception as exc:
            logger.debug("send_event %s failed: %s", verb, exc)
            return False

    async def request(self, verb: str,
                      payload: Optional[dict] = None,
                      timeout: float = REQUEST_TIMEOUT_SECONDS
                      ) -> dict:
        """Send a request to the sidecar and await the correlated response."""
        if not self.connected:
            raise ProtocolError(ErrorCode.UNAVAILABLE, "tunnel not connected")
        if not verb_allowed("agent_to_sidecar", verb):
            raise ProtocolError(ErrorCode.UNKNOWN_TYPE, f"verb {verb} not allowed")
        frame_id = new_correlation_id()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[frame_id] = fut
        try:
            body = build_envelope(verb, payload or {},
                                  secret=self._session_token or "",
                                  frame_id=frame_id)
            await self._send_raw(body)
            self.frames_out += 1
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ProtocolError(ErrorCode.TIMEOUT,
                                f"{verb} timed out") from exc
        finally:
            self._pending.pop(frame_id, None)

    def resolve_pending(self, frame_id: str, payload: dict) -> None:
        fut = self._pending.get(frame_id)
        if fut is not None and not fut.done():
            fut.set_result(payload)

    async def _send_raw(self, env: dict) -> None:
        ws = self._ws
        if ws is None:
            raise ProtocolError(ErrorCode.UNAVAILABLE, "no socket")
        async with self._send_lock:
            await ws.send(json.dumps(env, separators=(",", ":")))

    async def _close_ws(self, code: int = 1000, reason: str = "") -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close(code=code, reason=reason)
            except Exception:
                pass

    # ------------------------------------------------------------- events

    def _notify_connected(self) -> None:
        if self.state.state in (AgentState.DISCONNECTED, AgentState.CONNECTED,
                                AgentState.ERROR):
            self.state.transition(AgentState.CONNECTED, "tunnel established")
        if self._on_connected:
            self._on_connected()

    def _notify_disconnected(self, reason: str) -> None:
        if self.state.state is AgentState.CONNECTED:
            self.state.transition(AgentState.DISCONNECTED, reason)
        self.last_error = reason
        if self._on_disconnected:
            self._on_disconnected(reason)


async def ws_recv(ws: Any) -> str:
    return await ws.recv()

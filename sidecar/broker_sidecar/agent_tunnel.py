"""Windows-agent tunnel endpoint — authenticated outbound WSS/WS channel.

The Windows agent dials IN to ``/v1/agent/tunnel`` (it is the client; this
service never initiates outbound connections to the agent). Auth is a
one-time HMAC handshake with ``AGENT_SHARED_SECRET`` followed by
per-session-token-signed envelopes — the same scheme the agent implements
in ``agent/protocol.py``. Keep the two implementations in lockstep.

Security properties:
  - fail-closed: no AGENT_SHARED_SECRET -> every tunnel attempt rejected
  - timestamp + nonce replay protection on AUTH; per-message HMAC after
  - fixed verb allowlist per direction; payloads schema-checked
  - per-connection rate limit; 64 KiB frame cap; stale-heartbeat tracking
  - no shell, no file paths, no arbitrary commands — verbs are a fixed enum
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("broker_sidecar.agent_tunnel")

router = APIRouter()

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024
AUTH_TIMEOUT = 10.0
AUTH_MAX_AGE = 60
FRAME_SKEW = 300
NONCE_CAP = 4096
RATE_BURST = 30
RATE_PER_SECOND = 2.0
DEFAULT_STALE_SECONDS = 45.0
DEFAULT_OFFLINE_SECONDS = 90.0
SESSION_TOKEN_LEN = 64

AGENT_TO_SIDECAR = {"HEARTBEAT", "STATUS", "HEALTH", "PONG"}
SIDECAR_TO_AGENT = {"STATUS", "HEALTH", "RESTART", "PING"}

CLOSE_UNAUTHORIZED = 4401
CLOSE_POLICY = 4403
CLOSE_TOO_BIG = 1009


def _env(name: str, default: str = "") -> str:
    import os
    return (os.environ.get(name) or default).strip()


def _shared_secret() -> str:
    return _env("AGENT_SHARED_SECRET")


def _stale_seconds() -> float:
    try:
        return max(5.0, float(_env("AGENT_STALE_SECONDS", "45")))
    except ValueError:
        return DEFAULT_STALE_SECONDS


def _request_timeout() -> float:
    try:
        return max(1.0, float(_env("AGENT_REQUEST_TIMEOUT", "15")))
    except ValueError:
        return 15.0


# ------------------------------------------------------------------ crypto

def _hmac(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def _safe_eq(a: str, b: str) -> bool:
    x, y = a.encode(), b.encode()
    return len(x) == len(y) and hmac.compare_digest(x, y)


def _canonical(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True,
                      ensure_ascii=False)


def _frame_sig(secret: str, ts: int, fid: str, verb: str, payload: Any) -> str:
    return _hmac(secret, f"{ts}.{fid}.{verb}.{_canonical(payload)}")


def _verify_frame(secret: str, ts: int, fid: str, verb: str,
                  payload: Any, sig: str) -> bool:
    return _safe_eq(_frame_sig(secret, ts, fid, verb, payload), sig or "")


class NonceCache:
    def __init__(self, max_age: int = AUTH_MAX_AGE, cap: int = NONCE_CAP):
        self._max_age = max_age
        self._cap = cap
        self._seen: dict[str, float] = {}

    def seen(self, nonce: str, now: Optional[float] = None) -> bool:
        now_ts = now if now is not None else time.time()
        cutoff = now_ts - self._max_age
        for k in [k for k, ts in self._seen.items() if ts < cutoff]:
            self._seen.pop(k, None)
        if nonce in self._seen:
            return True
        if len(self._seen) >= self._cap:
            for k in sorted(self._seen, key=self._seen.get)[: self._cap // 4]:
                self._seen.pop(k, None)
        self._seen[nonce] = now_ts
        return False


class RateLimiter:
    def __init__(self, burst: int = RATE_BURST, per_second: float = RATE_PER_SECOND):
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


# ----------------------------------------------------------------- session

class AgentSession:
    """One authenticated agent tunnel."""

    def __init__(self, ws: WebSocket, agent_id: str):
        self.ws = ws
        self.agent_id = agent_id
        self.session_token = secrets.token_hex(SESSION_TOKEN_LEN // 2)
        self.connected_at = time.time()
        self.last_heartbeat_at: Optional[float] = None
        self.last_frame_at = self.connected_at
        self.version: Optional[str] = None
        self.state_reported: Optional[str] = None
        self.last_error: Optional[str] = None
        self.counters = {"frames_in": 0, "frames_out": 0, "heartbeats": 0,
                         "rejected": 0, "requests": 0}
        self.limiter = RateLimiter()
        self.pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._closed = False

    # ---- inbound helpers -------------------------------------------------

    def heartbeat_age(self) -> Optional[float]:
        if self.last_heartbeat_at is None:
            return None
        return time.time() - self.last_heartbeat_at

    def online(self) -> bool:
        age = self.heartbeat_age()
        return age is not None and age <= _stale_seconds() * 2

    # ---- outbound ---------------------------------------------------------

    async def send(self, verb: str, payload: Optional[dict] = None,
                   frame_id: Optional[str] = None) -> str:
        fid = frame_id or uuid.uuid4().hex
        ts = int(time.time())
        body = dict(payload or {})
        env = {
            "v": PROTOCOL_VERSION, "id": fid, "type": verb, "ts": ts,
            "payload": body,
            "sig": _frame_sig(self.session_token, ts, fid, verb, body),
        }
        raw = json.dumps(env, separators=(",", ":"), ensure_ascii=False)
        async with self._send_lock:
            await self.ws.send_text(raw)
        self.counters["frames_out"] += 1
        return fid

    async def request(self, verb: str, payload: Optional[dict] = None,
                      timeout: Optional[float] = None) -> dict:
        """Send a request to the agent and await the correlated response."""
        if self._closed:
            raise RuntimeError("agent session closed")
        if verb not in SIDECAR_TO_AGENT:
            raise ValueError(f"verb {verb} not allowed toward agent")
        timeout = timeout if timeout is not None else _request_timeout()
        fid = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending[fid] = fut
        self.counters["requests"] += 1
        try:
            await self.send(verb, payload, frame_id=fid)
            result = await asyncio.wait_for(fut, timeout=timeout)
            return result
        finally:
            self.pending.pop(fid, None)

    def resolve(self, frame_id: str, payload: dict) -> None:
        fut = self.pending.get(frame_id)
        if fut is not None and not fut.done():
            fut.set_result(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("agent session closed"))
        self.pending.clear()
        try:
            await self.ws.close(code=code, reason=reason)
        except Exception:
            pass


# ----------------------------------------------------------------- manager

class AgentTunnelManager:
    """Registry of the (single) active agent session + status reporting."""

    def __init__(self) -> None:
        self.session: Optional[AgentSession] = None
        self._lock = asyncio.Lock()
        self._nonces = NonceCache()
        self.connects = 0
        self.rejects = 0

    # ------------------------------------------------------------- endpoint

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        session: Optional[AgentSession] = None
        try:
            session = await self._authenticate(ws)
            if session is None:
                return
            async with self._lock:
                old = self.session
                self.session = session
            self.connects += 1
            if old is not None:
                await old.close(code=4000, reason="superseded")
            logger.info("agent tunnel established id=%s", session.agent_id)
            await self._recv_loop(session)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.warning("agent tunnel error: %s", type(exc).__name__)
        finally:
            if session is not None:
                await session.close()
                async with self._lock:
                    if self.session is session:
                        self.session = None
                logger.info("agent tunnel closed id=%s", session.agent_id)

    # -------------------------------------------------------------- auth

    async def _authenticate(self, ws: WebSocket) -> Optional[AgentSession]:
        secret = _shared_secret()
        if not secret:
            self.rejects += 1
            await ws.close(code=CLOSE_UNAUTHORIZED, reason="not configured")
            return None
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=AUTH_TIMEOUT)
        except (asyncio.TimeoutError, WebSocketDisconnect):
            await ws.close(code=CLOSE_POLICY, reason="auth timeout")
            return None
        try:
            env = json.loads(raw)
        except (ValueError, TypeError):
            await ws.close(code=CLOSE_POLICY, reason="bad auth frame")
            return None
        if not isinstance(env, dict) or env.get("type") != "AUTH":
            await ws.close(code=CLOSE_POLICY, reason="expected AUTH")
            return None
        payload = env.get("payload")
        if not isinstance(payload, dict):
            await ws.close(code=CLOSE_POLICY, reason="bad auth payload")
            return None
        agent_id = str(payload.get("agentId") or "")
        try:
            ts = int(payload.get("timestamp"))
            nonce = str(payload.get("nonce") or "")
            signature = str(payload.get("signature") or "")
        except (TypeError, ValueError):
            await ws.close(code=CLOSE_UNAUTHORIZED, reason="bad auth fields")
            return None
        if not agent_id or len(agent_id) > 64 or len(nonce) < 8 or len(nonce) > 128:
            await ws.close(code=CLOSE_UNAUTHORIZED, reason="bad auth fields")
            return None
        now = int(time.time())
        if abs(now - ts) > AUTH_MAX_AGE:
            self.rejects += 1
            await ws.close(code=CLOSE_UNAUTHORIZED, reason="stale auth")
            return None
        message = f"{agent_id}.{ts}.{nonce}"
        if not _safe_eq(_hmac(secret, message), signature):
            self.rejects += 1
            logger.warning("agent auth rejected id=%s", agent_id)
            await ws.close(code=CLOSE_UNAUTHORIZED, reason="bad signature")
            return None
        if self._nonces.seen(nonce):
            self.rejects += 1
            await ws.close(code=CLOSE_UNAUTHORIZED, reason="nonce replay")
            return None

        session = AgentSession(ws, agent_id)
        # AUTH_OK is signed with the SHARED secret (agent has no session
        # token yet); subsequent frames use the session token.
        ts_ok = int(time.time())
        fid = uuid.uuid4().hex
        ok_payload = {"sessionToken": session.session_token,
                      "heartbeatInterval": 15}
        env_out = {
            "v": PROTOCOL_VERSION, "id": fid, "type": "AUTH_OK", "ts": ts_ok,
            "payload": ok_payload,
            "sig": _frame_sig(secret, ts_ok, fid, "AUTH_OK", ok_payload),
        }
        await ws.send_text(json.dumps(env_out, separators=(",", ":")))
        return session

    # ------------------------------------------------------------- receive

    async def _recv_loop(self, session: AgentSession) -> None:
        async for raw in session.ws.iter_text():
            session.last_frame_at = time.time()
            if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
                await self._send_error(session, "frame too large")
                continue
            env = self._parse(raw)
            if env is None:
                continue
            session.counters["frames_in"] += 1
            if not session.limiter.allow():
                await self._send_error(session, "rate limited", env.get("id"))
                continue
            await self._dispatch(session, env)

    def _parse(self, raw: str) -> Optional[dict]:
        try:
            env = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(env, dict):
            return None
        if env.get("v") != PROTOCOL_VERSION:
            return None
        if not isinstance(env.get("id"), str) or len(env["id"]) > 64:
            return None
        if not isinstance(env.get("type"), str):
            return None
        try:
            ts = int(env.get("ts"))
        except (TypeError, ValueError):
            return None
        if abs(int(time.time()) - ts) > FRAME_SKEW:
            return None
        if not isinstance(env.get("payload"), dict):
            env["payload"] = {}
        if not isinstance(env.get("sig"), str):
            return None
        return env

    async def _dispatch(self, session: AgentSession, env: dict) -> None:
        verb = env["type"]
        payload = env["payload"]
        sig = env["sig"]
        # Responses to OUR requests resolve pending futures.
        if "result" in payload or "error" in payload:
            if not _verify_frame(session.session_token, int(env["ts"]),
                                 env["id"], verb, payload, sig):
                await self._send_error(session, "bad signature", env["id"])
                return
            session.resolve(env["id"], payload)
            return
        if verb not in AGENT_TO_SIDECAR:
            await self._send_error(session, f"verb {verb} not allowed",
                                   env["id"])
            return
        if not _verify_frame(session.session_token, int(env["ts"]),
                             env["id"], verb, payload, sig):
            await self._send_error(session, "bad signature", env["id"])
            return
        if verb == "HEARTBEAT":
            session.last_heartbeat_at = time.time()
            session.counters["heartbeats"] += 1
            v = payload.get("version")
            if isinstance(v, str):
                session.version = v
            s = payload.get("state")
            if isinstance(s, str):
                session.state_reported = s
            return
        # STATUS / HEALTH / PONG from agent are informational pushes or
        # responses already handled above; log and count only.
        logger.debug("agent frame %s id=%s", verb, env["id"])

    async def _send_error(self, session: AgentSession, message: str,
                          frame_id: Optional[str] = None) -> None:
        try:
            await session.send("ERROR", {
                "error": {"code": "MALFORMED", "message": message[:200]},
                "correlationId": frame_id,
            }, frame_id=frame_id or uuid.uuid4().hex)
        except Exception:
            pass

    # -------------------------------------------------------------- status

    def status(self) -> dict:
        session = self.session
        now = time.time()
        if session is None:
            return {
                "online": False, "state": "OFFLINE", "agentId": None,
                "lastHeartbeatAt": None, "heartbeatAgeSeconds": None,
                "connectedAt": None, "version": None, "reportedState": None,
                "lastError": None, "uptimeSeconds": None,
                "counters": None,
            }
        age = session.heartbeat_age()
        stale = age is not None and age > _stale_seconds()
        offline = age is None or age > DEFAULT_OFFLINE_SECONDS
        state = "OFFLINE" if offline else ("STALE" if stale else "ONLINE")
        return {
            "online": state == "ONLINE",
            "state": state,
            "agentId": session.agent_id,
            "lastHeartbeatAt": session.last_heartbeat_at,
            "heartbeatAgeSeconds": round(age, 1) if age is not None else None,
            "connectedAt": session.connected_at,
            "uptimeSeconds": round(now - session.connected_at, 1),
            "version": session.version,
            "reportedState": session.state_reported,
            "lastError": session.last_error,
            "counters": dict(session.counters),
            "manager": {"connects": self.connects, "rejects": self.rejects},
        }


# Module singleton — same pattern as telegram_service.tg.
manager = AgentTunnelManager()


@router.websocket("/v1/agent/tunnel")
async def agent_tunnel_endpoint(ws: WebSocket) -> None:
    await manager.handle(ws)

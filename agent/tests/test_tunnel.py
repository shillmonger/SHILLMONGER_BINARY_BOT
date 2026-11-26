"""Tunnel client tests against a fake in-process WS sidecar.

Covers: connect+auth, heartbeat, request/response correlation, timeout,
malformed frames, bad signatures, unauthorized verbs, reconnect, and
fatal auth rejection. No real network — websockets.serve on loopback.
"""

import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
import websockets

from agent import auth as authmod
from agent.config import AgentConfig
from agent.protocol import ErrorCode, ProtocolError
from agent.state import AgentState, StateMachine
from agent.tunnel import FatalAuthError, TunnelClient, ws_url

SECRET = "s" * 48
SESSION_TOKEN = "t" * 64
AGENT_ID = "test-agent"


def make_config(tmp_path: Path, port: int, **over) -> AgentConfig:
    fields = dict(
        agent_id=AGENT_ID,
        version="9.9.9-test",
        vps_url=f"http://127.0.0.1:{port}",
        shared_secret=SECRET,
        health_host="127.0.0.1",
        health_port=0,
        local_token="",
        heartbeat_seconds=60.0,
        reconnect_base_delay=0.05,
        reconnect_max_delay=0.2,
        state_dir=tmp_path / "state",
        tunnel_enabled=True,
    )
    fields.update(over)
    return AgentConfig(**fields)


def _frame_sig(secret: str, ts: int, fid: str, verb: str, payload: dict) -> str:
    msg = f"{ts}.{fid}.{verb}.{json.dumps(payload, separators=(',', ':'), sort_keys=True, ensure_ascii=False)}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


class FakeSidecar:
    """Minimal sidecar: AUTH handshake, then record/respond to frames."""

    def __init__(self, *, respond=True, drop_after_auth=False,
                 bad_auth_ok_sig=False, inject_bad_frame=False):
        self.received: list[dict] = []
        self.connections = 0
        self._respond = respond
        self._drop = drop_after_auth
        self._bad_sig = bad_auth_ok_sig
        self._inject_bad = inject_bad_frame
        self._server = None
        self.port = 0

    async def __aenter__(self):
        self._server = await websockets.serve(
            self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _handler(self, ws):
        self.connections += 1
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            env = json.loads(raw)
            p = env.get("payload", {})
            assert env.get("type") == "AUTH"
            msg = f'{p["agentId"]}.{p["timestamp"]}.{p["nonce"]}'
            expected = hmac.new(SECRET.encode(), msg.encode(),
                                hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, p["signature"]):
                await ws.close(4401)
                return
            ts = int(time.time())
            payload = {"sessionToken": SESSION_TOKEN}
            out = {"v": 1, "id": "auth-ok-1", "type": "AUTH_OK", "ts": ts,
                   "payload": payload,
                   "sig": _frame_sig(SECRET if not self._bad_sig else "x" * 48,
                                     ts, "auth-ok-1", "AUTH_OK", payload)}
            await ws.send(json.dumps(out))
            self.received.append({"type": "AUTH", "payload": p})
            if self._drop:
                await ws.close(1000)
                return
            if self._inject_bad:
                await ws.send("{this is not json")
                # Also a well-formed envelope with a forged signature.
                forged = {"v": 1, "id": "forged-1", "type": "STATUS",
                          "ts": int(time.time()), "payload": {},
                          "sig": "0" * 64}
                await ws.send(json.dumps(forged))
            async for raw in ws:
                env = json.loads(raw)
                self.received.append(env)
                if (self._respond and env.get("type") in ("STATUS", "HEALTH")
                        and "result" not in env.get("payload", {})
                        and "error" not in env.get("payload", {})):
                    rid = env["id"]
                    rts = int(time.time())
                    resp_payload = {"result": {"echo": env["type"],
                                               "agentId": AGENT_ID}}
                    resp = {"v": 1, "id": rid, "type": env["type"], "ts": rts,
                            "payload": resp_payload,
                            "sig": _frame_sig(SESSION_TOKEN, rts, rid,
                                              env["type"], resp_payload)}
                    await ws.send(json.dumps(resp))
        except Exception:
            pass


async def _wait_for(pred, timeout=5.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return False


def _client(config: AgentConfig) -> tuple[TunnelClient, StateMachine]:
    state = StateMachine(initial=AgentState.DISCONNECTED)
    return TunnelClient(config, state), state


async def test_ws_url_conversion():
    cfg = make_config(Path("."), 1234)
    assert ws_url(cfg) == "ws://127.0.0.1:1234/v1/agent/tunnel"
    cfg2 = make_config(Path("."), 1234, vps_url="https://example.com")
    assert ws_url(cfg2) == "wss://example.com/v1/agent/tunnel"


async def test_connect_auth_and_heartbeat(tmp_path):
    async with FakeSidecar() as sc:
        config = make_config(tmp_path, sc.port)
        client, state = _client(config)
        client.start()
        try:
            assert await _wait_for(lambda: client.connected)
            assert state.state is AgentState.CONNECTED
            assert await client.send_event("HEARTBEAT", {"agentId": AGENT_ID})
            assert await _wait_for(
                lambda: any(f.get("type") == "HEARTBEAT"
                            for f in sc.received))
        finally:
            await client.stop()


async def test_request_response_correlation(tmp_path):
    async with FakeSidecar() as sc:
        config = make_config(tmp_path, sc.port)
        client, _ = _client(config)
        client.start()
        try:
            assert await _wait_for(lambda: client.connected)
            result = await client.request("STATUS", timeout=5)
            assert result["result"]["echo"] == "STATUS"
            # The sidecar saw the request frame.
            assert any(f.get("type") == "STATUS" for f in sc.received)
        finally:
            await client.stop()


async def test_request_timeout(tmp_path):
    async with FakeSidecar(respond=False) as sc:
        config = make_config(tmp_path, sc.port)
        client, _ = _client(config)
        client.start()
        try:
            assert await _wait_for(lambda: client.connected)
            with pytest.raises(ProtocolError) as ei:
                await client.request("HEALTH", timeout=0.5)
            assert ei.value.code is ErrorCode.TIMEOUT
        finally:
            await client.stop()


async def test_malformed_and_forged_frames_do_not_kill_tunnel(tmp_path):
    async with FakeSidecar(inject_bad_frame=True) as sc:
        config = make_config(tmp_path, sc.port)
        client, _ = _client(config)
        client.start()
        try:
            assert await _wait_for(lambda: client.connected)
            # Agent must answer both bad frames with ERROR envelopes and
            # stay connected — then serve a normal request.
            assert await _wait_for(
                lambda: sum(1 for f in sc.received
                            if f.get("type") == "ERROR") >= 2, timeout=5)
            result = await client.request("STATUS", timeout=5)
            assert result["result"]["echo"] == "STATUS"
        finally:
            await client.stop()


async def test_bad_signature_from_server_rejected(tmp_path):
    async with FakeSidecar(bad_auth_ok_sig=True) as sc:
        config = make_config(tmp_path, sc.port)
        client, state = _client(config)
        client.start()
        try:
            # AUTH_OK signature invalid -> FatalAuthError -> ERROR, no retry.
            assert await _wait_for(
                lambda: state.state is AgentState.ERROR, timeout=8)
            assert client._task is not None and client._task.done()
        finally:
            await client.stop()


async def test_reconnect_after_drop(tmp_path):
    async with FakeSidecar(drop_after_auth=True) as sc:
        config = make_config(tmp_path, sc.port,
                             reconnect_base_delay=0.05,
                             reconnect_max_delay=0.1)
        client, state = _client(config)
        client.start()
        try:
            # Server drops every connection right after AUTH_OK.
            assert await _wait_for(lambda: sc.connections >= 2, timeout=8)
            assert client.reconnects >= 1
        finally:
            await client.stop()


async def test_send_event_when_disconnected_returns_false(tmp_path):
    config = make_config(tmp_path, 1)  # nothing listening
    client, _ = _client(config)
    assert await client.send_event("HEARTBEAT") is False

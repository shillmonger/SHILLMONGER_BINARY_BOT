"""Agent tunnel tests — WS auth, envelopes, correlation, rate/verb policy.

All in-process via FastAPI TestClient; no real network or broker.
"""

import hashlib
import hmac
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("BROKER_SIDECAR_SECRET", "test-secret")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from broker_sidecar import agent_tunnel  # noqa: E402
from broker_sidecar.main import app  # noqa: E402

SECRET = "a" * 48
KEY = {"X-Broker-Key": "test-secret"}
AGENT = "agent-test-1"


def _hmac(secret: str, msg: str) -> str:
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def _frame_sig(token: str, ts: int, fid: str, verb: str, payload: dict) -> str:
    canon = json.dumps(payload, separators=(",", ":"), sort_keys=True,
                       ensure_ascii=False)
    return _hmac(token, f"{ts}.{fid}.{verb}.{canon}")


def auth_frame(agent_id: str = AGENT, secret: str = SECRET,
               ts: int | None = None, nonce: str = "nonce-abcdefgh",
               bad_sig: bool = False) -> dict:
    ts = ts if ts is not None else int(time.time())
    msg = f"{agent_id}.{ts}.{nonce}"
    sig = _hmac(secret, msg)
    if bad_sig:
        sig = "0" * 64
    return {"v": 1, "id": "auth-1", "type": "AUTH", "ts": ts,
            "payload": {"agentId": agent_id, "timestamp": ts,
                        "nonce": nonce, "signature": sig}}


def data_frame(token: str, verb: str, payload: dict | None = None,
               fid: str | None = None, bad_sig: bool = False) -> dict:
    ts = int(time.time())
    fid = fid or f"f-{int(time.time() * 1000)}"
    body = payload or {}
    sig = _frame_sig(token, ts, fid, verb, body)
    if bad_sig:
        sig = "0" * 64
    return {"v": 1, "id": fid, "type": verb, "ts": ts,
            "payload": body, "sig": sig}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENT_SHARED_SECRET", SECRET)
    monkeypatch.setenv("BROKER_SIDECAR_SECRET", "test-secret")
    manager = agent_tunnel.manager
    manager.session = None
    manager.connects = 0
    manager.rejects = 0
    manager._nonces = agent_tunnel.NonceCache()
    with TestClient(app) as c:
        yield c


def _authed_ws(client, nonce: str | None = None):
    ws = client.websocket_connect("/v1/agent/tunnel")
    ws.__enter__()
    ws.send_text(json.dumps(auth_frame(nonce=nonce or "nonce-abcdefgh")))
    resp = json.loads(ws.receive_text())
    assert resp["type"] == "AUTH_OK"
    return ws, resp["payload"]["sessionToken"]


# --------------------------------------------------------------- status API


def test_status_offline_and_requires_key(client):
    r = client.get("/v1/agent/status")
    assert r.status_code == 401
    r = client.get("/v1/agent/status", headers=KEY)
    assert r.status_code == 200
    body = r.json()
    assert body["online"] is False
    assert body["state"] == "OFFLINE"


def test_status_online_after_heartbeat(client):
    ws, token = _authed_ws(client)
    try:
        ws.send_text(json.dumps(data_frame(
            token, "HEARTBEAT",
            {"agentId": AGENT, "version": "1.2.3", "state": "CONNECTED"})))
        time.sleep(0.3)
        body = client.get("/v1/agent/status", headers=KEY).json()
        assert body["online"] is True
        assert body["state"] == "ONLINE"
        assert body["agentId"] == AGENT
        assert body["version"] == "1.2.3"
        assert body["counters"]["heartbeats"] == 1
    finally:
        ws.__exit__(None, None, None)


def test_status_stale_after_old_heartbeat(client):
    ws, token = _authed_ws(client)
    try:
        ws.send_text(json.dumps(data_frame(token, "HEARTBEAT", {})))
        time.sleep(0.2)
        session = agent_tunnel.manager.session
        session.last_heartbeat_at = time.time() - 60  # > 45s stale, < 90s offline
        body = client.get("/v1/agent/status", headers=KEY).json()
        assert body["state"] == "STALE"
        assert body["online"] is False
    finally:
        ws.__exit__(None, None, None)


# ------------------------------------------------------------------ auth


def test_rejects_bad_auth_signature(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/agent/tunnel") as ws:
            ws.send_text(json.dumps(auth_frame(bad_sig=True)))
            ws.receive_text()


def test_rejects_stale_auth_timestamp(client):
    old = auth_frame(ts=int(time.time()) - 3600)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/agent/tunnel") as ws:
            ws.send_text(json.dumps(old))
            ws.receive_text()


def test_rejects_nonce_replay(client):
    nonce = "replay-me-123"
    ws1, _ = _authed_ws(client, nonce=nonce)
    ws1.__exit__(None, None, None)
    time.sleep(0.2)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/agent/tunnel") as ws:
            ws.send_text(json.dumps(auth_frame(nonce=nonce)))
            ws.receive_text()


def test_rejects_when_secret_unconfigured(client, monkeypatch):
    monkeypatch.delenv("AGENT_SHARED_SECRET")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/agent/tunnel") as ws:
            ws.send_text(json.dumps(auth_frame()))
            ws.receive_text()


# ------------------------------------------------------------- data frames


def test_heartbeat_then_status_request_roundtrip(client):
    ws, token = _authed_ws(client)
    try:
        ws.send_text(json.dumps(data_frame(token, "HEARTBEAT",
                                           {"version": "2.0"})))
        time.sleep(0.2)
        session = agent_tunnel.manager.session
        # Sidecar -> agent STATUS request with correlation id.
        box = {}
        def do_req():
            box["r"] = client.portal.call(
                session.request, "STATUS", None, 5)
        t = threading.Thread(target=do_req)
        t.start()
        frame = json.loads(ws.receive_text())
        assert frame["type"] == "STATUS"
        assert frame["id"]
        # Agent responds with result + same correlation id.
        rts = int(time.time())
        resp_payload = {"result": {"state": "CONNECTED", "pong": True}}
        resp = {"v": 1, "id": frame["id"], "type": "STATUS", "ts": rts,
                "payload": resp_payload,
                "sig": _frame_sig(token, rts, frame["id"], "STATUS",
                                  resp_payload)}
        ws.send_text(json.dumps(resp))
        t.join(timeout=5)
        assert box["r"]["result"]["pong"] is True
    finally:
        ws.__exit__(None, None, None)


def test_ping_verb_roundtrip(client):
    ws, token = _authed_ws(client)
    try:
        ws.send_text(json.dumps(data_frame(token, "HEARTBEAT", {})))
        time.sleep(0.2)
        session = agent_tunnel.manager.session
        box = {}
        def do_req():
            box["r"] = client.portal.call(session.request, "PING", None, 5)
        t = threading.Thread(target=do_req)
        t.start()
        frame = json.loads(ws.receive_text())
        assert frame["type"] == "PING"
        rts = int(time.time())
        resp_payload = {"result": {"pong": True}}
        resp = {"v": 1, "id": frame["id"], "type": "PING", "ts": rts,
                "payload": resp_payload,
                "sig": _frame_sig(token, rts, frame["id"], "PING",
                                  resp_payload)}
        ws.send_text(json.dumps(resp))
        t.join(timeout=5)
        assert box["r"]["result"]["pong"] is True
    finally:
        ws.__exit__(None, None, None)


def test_request_timeout_when_agent_silent(client):
    ws, token = _authed_ws(client)
    try:
        ws.send_text(json.dumps(data_frame(token, "HEARTBEAT", {})))
        time.sleep(0.2)
        session = agent_tunnel.manager.session
        with pytest.raises(Exception):
            client.portal.call(session.request, "STATUS", None, 0.4)
    finally:
        ws.__exit__(None, None, None)


def test_bad_frame_signature_gets_error_not_disconnect(client):
    ws, token = _authed_ws(client)
    try:
        ws.send_text(json.dumps(data_frame(
            token, "STATUS", {"probe": 1}, bad_sig=True)))
        frame = json.loads(ws.receive_text())
        assert frame["type"] == "ERROR"
        # Connection survives — a valid heartbeat still registers.
        ws.send_text(json.dumps(data_frame(token, "HEARTBEAT", {})))
        time.sleep(0.3)
        assert client.get("/v1/agent/status", headers=KEY
                          ).json()["counters"]["heartbeats"] == 1
    finally:
        ws.__exit__(None, None, None)


def test_disallowed_verb_rejected(client):
    ws, token = _authed_ws(client)
    try:
        ws.send_text(json.dumps(data_frame(token, "SHELL", {"cmd": "id"})))
        frame = json.loads(ws.receive_text())
        assert frame["type"] == "ERROR"
        assert "not allowed" in frame["payload"]["error"]["message"]
    finally:
        ws.__exit__(None, None, None)


def test_malformed_json_dropped_silently(client):
    ws, token = _authed_ws(client)
    try:
        ws.send_text("{garbage")
        # Connection stays usable.
        ws.send_text(json.dumps(data_frame(token, "HEARTBEAT", {})))
        time.sleep(0.3)
        body = client.get("/v1/agent/status", headers=KEY).json()
        assert body["counters"]["heartbeats"] == 1
    finally:
        ws.__exit__(None, None, None)


def test_graceful_disconnect_clears_session(client):
    ws, token = _authed_ws(client)
    ws.send_text(json.dumps(data_frame(token, "HEARTBEAT", {})))
    time.sleep(0.2)
    ws.__exit__(None, None, None)
    time.sleep(0.5)
    body = client.get("/v1/agent/status", headers=KEY).json()
    assert body["state"] == "OFFLINE"

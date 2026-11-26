import asyncio

import httpx
import pytest

from agent.config import load_config
from agent.health_server import create_app
from agent.state import AgentState, StateMachine
from agent.heartbeat import Heartbeat


def _cfg(**over):
    env = {"AGENT_ID": "health-test", "AGENT_LOCAL_HEALTH_PORT": "18793"}
    env.update(over)
    return load_config(env_file=None, environ=env)


def _client(app, token=None):
    headers = {"X-Agent-Token": token} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers=headers,
    )


async def test_health_liveness():
    cfg = _cfg()
    app = create_app(cfg, StateMachine())
    async with _client(app) as c:
        r = await c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["service"] == "quotex-agent"
    assert body["agentId"] == "health-test"


async def test_ready_503_until_connected():
    sm = StateMachine()
    cfg = _cfg()
    app = create_app(cfg, sm)
    async with _client(app) as c:
        r = await c.get("/ready")
        assert r.status_code == 503
        sm.transition(AgentState.STARTING)
        r = await c.get("/ready")
        assert r.status_code == 503
        sm.transition(AgentState.CONNECTED)
        r = await c.get("/ready")
        assert r.status_code == 200


async def test_status_snapshot():
    sm = StateMachine()
    sm.transition(AgentState.STARTING)
    sm.transition(AgentState.CONNECTED)
    hb = Heartbeat(999)
    hb.beat_now()
    cfg = _cfg(AGENT_VERSION="1.2.3")
    app = create_app(cfg, sm, heartbeat=hb)
    async with _client(app) as c:
        r = await c.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "1.2.3"
    assert body["state"]["state"] == "CONNECTED"
    assert body["heartbeat"]["beats"] == 1


async def test_local_token_required_when_configured():
    cfg = _cfg(AGENT_LOCAL_TOKEN="tok-123")
    app = create_app(cfg, StateMachine())
    async with _client(app) as c:
        assert (await c.get("/status")).status_code == 401
        assert (await c.get("/diagnostics")).status_code == 401
    async with _client(app, token="tok-123") as c:
        assert (await c.get("/status")).status_code == 200
        assert (await c.get("/diagnostics")).status_code == 200


async def test_wrong_token_rejected():
    cfg = _cfg(AGENT_LOCAL_TOKEN="tok-123")
    app = create_app(cfg, StateMachine())
    async with _client(app, token="wrong") as c:
        assert (await c.get("/status")).status_code == 401


async def test_non_loopback_refused():
    # config validation normally prevents this; create_app must refuse too
    # — build the config object bypassing load_config validation.
    from agent.config import AgentConfig
    bad = AgentConfig(
        agent_id="x", version="0", vps_url="", shared_secret="",
        health_host="0.0.0.0", health_port=1, local_token="",
        heartbeat_seconds=15, reconnect_base_delay=1,
        reconnect_max_delay=60, state_dir=None, tunnel_enabled=False,
    )
    with pytest.raises(ValueError, match="loopback"):
        create_app(bad, StateMachine())

import asyncio

from agent.config import load_config
from agent.service import AgentService
from agent.state import AgentState


def _cfg(tmp_path, **over):
    env = {
        "AGENT_ID": "svc-test",
        "AGENT_LOCAL_HEALTH_PORT": "18794",
        "AGENT_STATE_DIR": str(tmp_path / "state"),
        "AGENT_HEARTBEAT_SECONDS": "1",
    }
    env.update(over)
    return load_config(env_file=None, environ=env)


async def test_service_start_stop_lifecycle(tmp_path):
    svc = AgentService(_cfg(tmp_path))
    await svc.start()
    assert svc.state.state is AgentState.CONNECTED
    snap = svc.status_snapshot()
    assert snap["agentId"] == "svc-test"
    assert snap["heartbeat"]["running"] is True
    assert snap["diagnostics"]["ok"] is True
    await svc.stop()
    assert svc.state.state is AgentState.STOPPED


async def test_request_stop_ends_wait(tmp_path):
    svc = AgentService(_cfg(tmp_path))
    await svc.start()
    svc.request_stop()
    await asyncio.wait_for(svc.wait(), timeout=2)
    await svc.stop()


async def test_double_start_is_safe(tmp_path):
    svc = AgentService(_cfg(tmp_path))
    await svc.start()
    await svc.start()  # no-op
    assert svc.state.state is AgentState.CONNECTED
    await svc.stop()


async def test_heartbeat_records_beats(tmp_path):
    svc = AgentService(_cfg(tmp_path))
    await svc.start()
    await asyncio.sleep(1.3)  # ~1 beat at 1s interval
    status = svc.heartbeat.status()
    assert status["beats"] >= 1
    assert status["lastBeatAt"] is not None
    await svc.stop()

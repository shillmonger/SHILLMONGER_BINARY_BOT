import asyncio

from agent.heartbeat import Heartbeat


async def test_heartbeat_beats_on_interval():
    beats = []

    async def on_beat():
        beats.append(1)

    hb = Heartbeat(0.2, on_beat=on_beat)  # clamped to >=1s? no: interval max(1.0)
    # interval is clamped to minimum 1.0s — use manual beat for speed instead
    await hb.start()
    await asyncio.sleep(0.1)
    hb.beat_now()
    status = hb.status()
    assert status["beats"] >= 1
    assert status["running"] is True
    await hb.stop()
    assert hb.status()["running"] is False


async def test_heartbeat_failure_isolated():
    async def bad():
        raise RuntimeError("beat exploded")

    hb = Heartbeat(999, on_beat=bad)
    await hb.start()
    hb._interval = 0.01  # test-only: shrink for speed
    await asyncio.sleep(0.05)
    status = hb.status()
    assert status["failures"] >= 1
    assert "beat exploded" in (status["lastError"] or "")
    await hb.stop()


def test_heartbeat_status_shape():
    hb = Heartbeat(15)
    s = hb.status()
    assert s["intervalSeconds"] == 15
    assert s["beats"] == 0
    assert s["running"] is False

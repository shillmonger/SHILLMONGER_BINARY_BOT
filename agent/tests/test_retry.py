import pytest

from agent.retry import backoff_delay, retry_async


class Boom(Exception):
    pass


async def test_success_first_try():
    calls = []

    async def ok():
        calls.append(1)
        return "done"

    assert await retry_async(ok, attempts=3) == "done"
    assert len(calls) == 1


async def test_retries_then_success():
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise Boom("nope")
        return "ok"

    result = await retry_async(
        flaky, attempts=5, base_delay=0.001, max_delay=0.01, jitter=0.0,
        sleep=lambda _s: __import__("asyncio").sleep(0),
    )
    assert result == "ok"
    assert len(calls) == 3


async def test_exhausts_attempts_and_raises_last():
    calls = []

    async def always():
        calls.append(1)
        raise Boom("fail")

    with pytest.raises(Boom):
        await retry_async(
            always, attempts=3, base_delay=0.001, max_delay=0.01, jitter=0.0,
            sleep=lambda _s: __import__("asyncio").sleep(0),
        )
    assert len(calls) == 3


async def test_non_matching_exception_not_retried():
    calls = []

    async def other():
        calls.append(1)
        raise ValueError("fatal")

    with pytest.raises(ValueError):
        await retry_async(
            other, attempts=3, base_delay=0.001, retry_on=(Boom,),
            sleep=lambda _s: __import__("asyncio").sleep(0),
        )
    assert len(calls) == 1


def test_backoff_delay_bounds():
    d = backoff_delay(0, base_delay=1.0, max_delay=30.0, jitter=0.0)
    assert d == 1.0
    d = backoff_delay(10, base_delay=1.0, max_delay=30.0, jitter=0.0)
    assert d == 30.0  # capped


def test_backoff_jitter_range():
    import random
    rng = random.Random(42)
    for _ in range(50):
        d = backoff_delay(2, base_delay=1.0, max_delay=100.0, jitter=0.2, rng=rng)
        assert 4.0 * 0.8 <= d <= 4.0 * 1.2

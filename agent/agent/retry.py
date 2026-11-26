"""Bounded retry with exponential backoff and jitter.

Used for infrastructure failures only (tunnel reconnects, health probes).
Never used to hammer broker endpoints.
"""

from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


async def retry_async(
    factory: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: float = 0.2,
    retry_on: tuple = (Exception,),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call ``factory`` up to ``attempts`` times.

    Delay before attempt N (1-based, after the first failure) is
    ``min(max_delay, base_delay * 2**(n-1))`` plus uniform jitter of
    +/- ``jitter`` fraction. Only ``retry_on`` exceptions are retried;
    the final failure is re-raised unchanged.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return await factory()
        except retry_on as exc:  # noqa: BLE001 - intentional bounded retry
            last_exc = exc
            if i == attempts - 1:
                break
            delay = min(max_delay, base_delay * (2 ** i))
            delay *= 1.0 + random.uniform(-jitter, jitter)
            await sleep(max(0.0, delay))
    assert last_exc is not None
    raise last_exc


def backoff_delay(
    attempt: int,
    *,
    base_delay: float,
    max_delay: float,
    jitter: float = 0.2,
    rng: random.Random | None = None,
) -> float:
    """Pure delay calculator — exposed for tests and the tunnel client."""
    r = rng or random
    delay = min(max_delay, base_delay * (2 ** max(0, attempt)))
    return max(0.0, delay * (1.0 + r.uniform(-jitter, jitter)))

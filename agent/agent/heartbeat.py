"""Service heartbeat.

Phase B records beats locally (count, timestamps, interval drift). Phase C
transmits them over the tunnel; the sidecar uses freshness for stale
detection. The loop is failure-isolated: a beat error never kills the
service — it is logged and counted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("quotex_agent.heartbeat")


class Heartbeat:
    def __init__(
        self,
        interval_seconds: float,
        *,
        on_beat: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self._interval = max(1.0, interval_seconds)
        self._on_beat = on_beat
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._beats = 0
        self._failures = 0
        self._last_beat_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._started_at: Optional[float] = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._started_at = time.time()
        self._task = asyncio.create_task(self._loop(), name="heartbeat")
        logger.info("heartbeat started interval=%.1fs", self._interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        logger.info("heartbeat stopped beats=%d failures=%d",
                    self._beats, self._failures)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return  # stop requested during the interval
            except asyncio.TimeoutError:
                pass
            await self._beat()

    async def _beat(self) -> None:
        self._last_beat_at = time.time()
        try:
            if self._on_beat is not None:
                await self._on_beat()
            self._beats += 1
            self._last_error = None
        except Exception as exc:  # noqa: BLE001 - heartbeat must never crash
            self._failures += 1
            self._last_error = str(exc)[:200]
            logger.warning("heartbeat beat failed: %s", self._last_error)

    def beat_now(self) -> None:
        """Synchronous single beat for tests/shutdown paths."""
        self._last_beat_at = time.time()
        self._beats += 1

    def status(self) -> dict:
        now = time.time()
        return {
            "intervalSeconds": self._interval,
            "beats": self._beats,
            "failures": self._failures,
            "lastBeatAt": self._last_beat_at,
            "ageSeconds": (
                round(now - self._last_beat_at, 1)
                if self._last_beat_at else None
            ),
            "lastError": self._last_error,
            "running": self._task is not None and not self._task.done(),
        }

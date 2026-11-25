"""Quotex health check: verifies the session and that it is a DEMO account."""

from __future__ import annotations

from ..common.adapters import BrokerAdapter
from ..common.base_connector import run_health_check
from ..common.config import BrokerSettings
from ..common.models import HealthResult


async def check_quotex_health(adapter: BrokerAdapter, settings: BrokerSettings) -> HealthResult:
    """Never raises. Timeouts, dead sessions and non-DEMO accounts are reported in
    the returned HealthResult."""
    return await run_health_check(adapter, timeout=settings.health_timeout)

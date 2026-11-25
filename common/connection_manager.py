"""Coordinates independent broker connectors and renders the terminal dashboard."""

from __future__ import annotations

import asyncio
import logging
import textwrap
from typing import Dict, List, Sequence

from .base_connector import BaseConnector
from .logger import close_logger
from .models import BrokerStatus, ConnectionState

WIDTH = 50


class ConnectionManager:
    """Starts, queries and stops connectors. A failure in one never reaches another:
    every call into a connector is individually guarded."""

    def __init__(self, connectors: Sequence[BaseConnector], logger: logging.Logger) -> None:
        self._connectors: Dict[str, BaseConnector] = {}
        for connector in connectors:
            if connector.KEY in self._connectors:
                raise ValueError(f"Duplicate connector key: {connector.KEY}")
            self._connectors[connector.KEY] = connector
        self.logger = logger
        self._phase = "INITIALIZED"
        self._last_states: Dict[str, ConnectionState] = {}

    # ----------------------------------------------------------------- queries
    @property
    def phase(self) -> str:
        return self._phase

    @property
    def connectors(self) -> Dict[str, BaseConnector]:
        return dict(self._connectors)

    def get_state(self, key: str) -> ConnectionState:
        return self._connectors[key].state

    def get_status(self, key: str) -> BrokerStatus:
        return self._connectors[key].status()

    def statuses(self) -> List[BrokerStatus]:
        return [c.status() for c in self._connectors.values()]

    @property
    def monitoring_active(self) -> bool:
        return self._phase == "RUNNING" and any(c.is_monitoring for c in self._connectors.values())

    # --------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._phase == "RUNNING":
            return
        self._phase = "STARTING"
        self.logger.info("Manager starting with brokers: %s", ", ".join(self._connectors))
        await asyncio.gather(*(self._guard(c, "start") for c in self._connectors.values()))
        self._phase = "RUNNING"
        self.logger.info("Manager running")

    async def stop(self) -> None:
        if self._phase == "STOPPED":
            return
        self._phase = "STOPPING"
        self.logger.info("Shutdown requested")
        await asyncio.gather(*(self._guard(c, "stop") for c in self._connectors.values()))
        self._phase = "STOPPED"
        self.logger.info("Manager stopped")
        for connector in self._connectors.values():
            connector.close_logging()
        close_logger(self.logger)

    async def _guard(self, connector: BaseConnector, action: str) -> None:
        try:
            await getattr(connector, action)()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = connector.describe_error(exc)
            self.logger.error("%s: %s failed: %s", connector.LABEL, action, message)
            connector.mark_error(f"{action} failed: {message}")

    def observe(self) -> None:
        """Log state transitions to manager.log (called on each dashboard refresh)."""
        for key, connector in self._connectors.items():
            state = connector.state
            if self._last_states.get(key) != state:
                self.logger.info("%s: %s", connector.LABEL, state.value)
                self._last_states[key] = state

    # --------------------------------------------------------------- dashboard
    def render_dashboard(self) -> str:
        statuses = self.statuses()
        bar = "=" * WIDTH
        rule = "-" * WIDTH
        lines = [bar, "BROKER CONNECTION MANAGER".center(WIDTH).rstrip(), bar, ""]
        if any(s.simulated for s in statuses):
            lines += ["*** SIMULATION MODE: no real broker session ***", ""]
        for status in statuses:
            lines += self._render_status(status)
            lines += ["", rule, ""]
        lines += [
            f"Manager Status: {self._phase}",
            f"Monitoring: {self._monitoring_text()}",
            "",
            bar,
        ]
        return "\n".join(lines)

    def _monitoring_text(self) -> str:
        if self._phase in ("STOPPING", "STOPPED", "INITIALIZED"):
            return "STOPPED" if self._phase != "INITIALIZED" else "NOT STARTED"
        if self.monitoring_active:
            return "ACTIVE"
        return "IDLE (no live broker session to monitor)"

    @staticmethod
    def _render_status(status: BrokerStatus) -> List[str]:
        state = status.state.value
        if status.simulated and status.state.is_up:
            state += " (SIMULATED)"
        mode = status.account_mode or f"{status.configured_mode} (configured)"
        checked = status.last_check.strftime("%H:%M:%S") if status.last_check else "never"
        latency = f"{status.latency_ms:.0f} ms" if status.latency_ms is not None else "UNKNOWN"
        lines = [
            status.broker,
            f"Status: {state}",
            f"Account Mode: {mode}",
            f"Last Connection Check: {checked}",
            f"Latency: {latency}",
        ]
        if status.detail:
            wrapped = textwrap.wrap(status.detail, width=WIDTH - 8) or [""]
            lines.append(f"Detail: {wrapped[0]}")
            lines += [" " * 8 + part for part in wrapped[1:]]
        return lines

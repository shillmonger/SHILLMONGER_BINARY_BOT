"""Connection status state machine.

States (per the approved design):
  STOPPED -> STARTING -> CONNECTED <-> DISCONNECTED -> ERROR | BLOCKED

Phase B note: CONNECTED means the agent's core loops are healthy. Broker
and tunnel connectivity states are layered on in later phases; BLOCKED is
reserved for broker security-challenge detection (never bypassed).
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Optional


class AgentState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"
    BLOCKED = "BLOCKED"


_TRANSITIONS = {
    AgentState.STOPPED: {AgentState.STARTING},
    AgentState.STARTING: {AgentState.CONNECTED, AgentState.DISCONNECTED,
                          AgentState.ERROR, AgentState.STOPPED},
    AgentState.CONNECTED: {
        AgentState.DISCONNECTED,
        AgentState.ERROR,
        AgentState.BLOCKED,
        AgentState.STOPPED,
    },
    AgentState.DISCONNECTED: {AgentState.CONNECTED, AgentState.ERROR, AgentState.STOPPED},
    AgentState.ERROR: {AgentState.STARTING, AgentState.STOPPED},
    AgentState.BLOCKED: {AgentState.STARTING, AgentState.STOPPED},
}

_HEALTHY = {AgentState.CONNECTED, AgentState.STARTING}


class StateMachine:
    """Thread-safe state machine with last-error bookkeeping."""

    def __init__(self, initial: AgentState = AgentState.STOPPED) -> None:
        self._state = initial
        self._last_error: Optional[str] = None
        self._last_transition_at = time.time()
        self._transition_count = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> AgentState:
        with self._lock:
            return self._state

    @property
    def last_error(self) -> Optional[str]:
        with self._lock:
            return self._last_error

    def transition(self, target: AgentState, reason: str = "") -> bool:
        """Attempt a transition. Returns False (no-op) when illegal."""
        with self._lock:
            if target not in _TRANSITIONS.get(self._state, set()):
                return False
            self._state = target
            self._last_transition_at = time.time()
            self._transition_count += 1
            if target in (AgentState.ERROR, AgentState.BLOCKED):
                self._last_error = reason or f"entered {target.value}"
            elif target is AgentState.CONNECTED:
                self._last_error = None
            return True

    def fail(self, error: str) -> None:
        self.transition(AgentState.ERROR, reason=error)

    def block(self, reason: str) -> None:
        """Mark BLOCKED — reserved for broker security challenges."""
        self.transition(AgentState.BLOCKED, reason=reason)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state.value,
                "healthy": self._state in _HEALTHY,
                "lastError": self._last_error,
                "lastTransitionAt": self._last_transition_at,
                "transitionCount": self._transition_count,
            }

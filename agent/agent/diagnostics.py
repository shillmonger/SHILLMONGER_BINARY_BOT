"""Local health diagnostics.

Checks only LOCAL infrastructure: config sanity, interpreter version,
dependencies importable, state dir writable, health bind is loopback,
health port free. It NEVER contacts broker endpoints or probes
third-party security challenges — diagnostics must not become a
side-channel for broker interaction.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from .config import AgentConfig

MIN_PYTHON = (3, 10)
REQUIRED_MODULES = ("fastapi", "uvicorn", "dotenv")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


def _check_config(config: AgentConfig) -> Check:
    return Check("config", True, f"agent_id={config.agent_id} v{config.version}")


def _check_python(_config: AgentConfig) -> Check:
    ok = sys.version_info[:2] >= MIN_PYTHON
    return Check(
        "python_version", ok,
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )


def _check_dependencies(_config: AgentConfig) -> Check:
    missing = [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]
    return Check("dependencies", not missing,
                 "ok" if not missing else f"missing: {', '.join(missing)}")


def _check_state_dir(config: AgentConfig) -> Check:
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        probe = config.state_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return Check("state_dir", True, str(config.state_dir))
    except OSError as exc:
        return Check("state_dir", False, f"{config.state_dir}: {exc}")


def _check_health_bind(config: AgentConfig) -> Check:
    if config.health_host not in ("127.0.0.1", "::1", "localhost"):
        return Check("health_bind", False,
                     f"non-loopback bind refused: {config.health_host}")
    return Check("health_bind", True, config.health_bind)


def _check_port_free(config: AgentConfig) -> Check:
    """Only meaningful BEFORE the health server binds; callers may skip it
    once the server is running (the port is then legitimately in use)."""
    family = socket.AF_INET6 if ":" in config.health_host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.bind((config.health_host, config.health_port))
        return Check("health_port", True, str(config.health_port))
    except OSError as exc:
        return Check("health_port", False, f"port {config.health_port}: {exc}")


CHECKS: List[Callable[[AgentConfig], Check]] = [
    _check_config,
    _check_python,
    _check_dependencies,
    _check_state_dir,
    _check_health_bind,
]


def run_diagnostics(
    config: AgentConfig,
    *,
    include_port: bool = False,
    extra: Optional[List[Callable[[AgentConfig], Check]]] = None,
) -> List[Check]:
    checks = [c(config) for c in CHECKS]
    if include_port:
        checks.append(_check_port_free(config))
    if extra:
        checks.extend(c(config) for c in extra)
    return checks


def diagnostics_summary(checks: List[Check]) -> dict:
    return {
        "ok": all(c.ok for c in checks),
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
        "checkedAt": time.time(),
    }

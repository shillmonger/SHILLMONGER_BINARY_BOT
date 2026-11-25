"""Environment-based configuration. Nothing is hardcoded; secrets are masked.

Real environment variables take precedence over values in the .env file.
The process environment is never modified, so nothing is shared between brokers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

PACKAGE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PACKAGE_DIR / ".env"
DEFAULT_LOG_DIR = PACKAGE_DIR / "logs"

#: Account modes accepted in this development stage. Everything else is refused.
DEMO_MODES = frozenset({"DEMO", "PRACTICE"})

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class SecretStr:
    """Holds a secret and refuses to print it. Use ``reveal()`` deliberately."""

    __slots__ = ("_value",)

    def __init__(self, value: str = "") -> None:
        self._value = value or ""

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "SecretStr('***')" if self._value else "SecretStr('')"

    def __str__(self) -> str:
        return "***" if self._value else ""


def parse_env_file(path: Path) -> Dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` file. Missing/unreadable file -> empty dict."""
    values: Dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            idx = value.find(" #")
            if idx != -1:
                value = value[:idx].rstrip()
        values[key] = value
    return values


def build_environment(
    env_file: Optional[Path] = DEFAULT_ENV_FILE,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Merge the .env file with the real environment (environment wins)."""
    merged: Dict[str, str] = parse_env_file(env_file) if env_file else {}
    merged.update(os.environ if environ is None else environ)
    return merged


def _get_bool(env: Mapping[str, str], key: str, default: bool, errors: List[str]) -> bool:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    errors.append(f"{key} must be true or false")
    return default


def _get_number(env, key, default, errors, *, minimum, cast=float):
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = cast(raw.strip())
    except ValueError:
        errors.append(f"{key} must be a number")
        return default
    if value < minimum:
        errors.append(f"{key} must be >= {minimum}")
        return default
    return value


@dataclass(frozen=True)
class BrokerSettings:
    """Settings for ONE broker. Each broker reads its own prefixed variables."""

    prefix: str
    enabled: bool
    account_mode: str
    email: SecretStr
    password: SecretStr
    adapter: str
    connect_timeout: float
    health_interval: float
    health_timeout: float
    degraded_latency_ms: float
    failures_before_disconnect: int
    max_reconnect_attempts: int  # 0 = unlimited
    reconnect_base_delay: float
    reconnect_max_delay: float
    errors: Tuple[str, ...] = ()

    @property
    def config_error(self) -> Optional[str]:
        return "; ".join(self.errors) if self.errors else None


def read_broker_settings(env: Mapping[str, str], prefix: str) -> BrokerSettings:
    """Read ``<PREFIX>_*`` variables. Invalid values never raise; they are reported
    through ``config_error`` so only that broker is blocked."""
    errors: List[str] = []
    mode = (env.get(f"{prefix}_ACCOUNT_MODE") or "DEMO").strip().upper()
    if mode not in DEMO_MODES:
        errors.append(
            f"{prefix}_ACCOUNT_MODE={mode[:20]!r} is not permitted: "
            "only DEMO/PRACTICE is allowed in this version"
        )
        mode = "DEMO"
    else:
        mode = "DEMO"

    def num(name, default, minimum, cast=float):
        return _get_number(env, f"{prefix}_{name}", default, errors, minimum=minimum, cast=cast)

    return BrokerSettings(
        prefix=prefix,
        enabled=_get_bool(env, f"{prefix}_ENABLED", True, errors),
        account_mode=mode,
        email=SecretStr(env.get(f"{prefix}_EMAIL", "")),
        password=SecretStr(env.get(f"{prefix}_PASSWORD", "")),
        adapter=(env.get(f"{prefix}_ADAPTER") or "").strip(),
        connect_timeout=num("CONNECT_TIMEOUT", 20.0, 0.01),
        health_interval=num("HEALTH_INTERVAL", 15.0, 0.01),
        health_timeout=num("HEALTH_TIMEOUT", 10.0, 0.01),
        degraded_latency_ms=num("DEGRADED_LATENCY_MS", 1500.0, 0.0),
        failures_before_disconnect=num("FAILURES_BEFORE_DISCONNECT", 2, 1, int),
        max_reconnect_attempts=num("MAX_RECONNECT_ATTEMPTS", 5, 0, int),
        reconnect_base_delay=num("RECONNECT_BASE_DELAY", 2.0, 0.0),
        reconnect_max_delay=num("RECONNECT_MAX_DELAY", 60.0, 0.0),
        errors=tuple(errors),
    )


@dataclass(frozen=True)
class ManagerSettings:
    log_dir: Path
    log_level: str
    refresh_interval: float
    errors: Tuple[str, ...] = ()


def read_manager_settings(env: Mapping[str, str]) -> ManagerSettings:
    errors: List[str] = []
    log_dir = (env.get("BROKER_LOG_DIR") or "").strip()
    level = (env.get("BROKER_LOG_LEVEL") or "INFO").strip().upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        errors.append("BROKER_LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")
        level = "INFO"
    refresh = _get_number(env, "BROKER_DASHBOARD_REFRESH", 1.0, errors, minimum=0.05)
    return ManagerSettings(
        log_dir=Path(log_dir) if log_dir else DEFAULT_LOG_DIR,
        log_level=level,
        refresh_interval=refresh,
        errors=tuple(errors),
    )

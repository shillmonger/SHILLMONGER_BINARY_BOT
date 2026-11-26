"""Environment-based configuration with strict validation.

Real environment variables take precedence over .env values. Validation
fails fast with ConfigError listing every problem — a misconfigured agent
must never start half-initialized.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional

AGENT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = AGENT_DIR / ".env"
DEFAULT_STATE_DIR = AGENT_DIR / "state"

AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
MIN_SECRET_LEN = 32

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """Raised when agent configuration is missing or invalid."""


def parse_env_file(path: Path) -> Dict[str, str]:
    """Minimal KEY=VALUE parser (same semantics as the sidecar's)."""
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


def _merged_env(env_file: Optional[Path], environ: Optional[Mapping[str, str]]) -> Dict[str, str]:
    merged = parse_env_file(env_file) if env_file else {}
    merged.update(os.environ if environ is None else environ)
    return merged


def _get(env: Mapping[str, str], key: str, default: str = "") -> str:
    return (env.get(key) or default).strip()


def _get_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = _get(env, key)
    if not raw:
        return default
    low = raw.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise ConfigError(f"{key} must be true or false (got {raw!r})")


def _get_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = _get(env, key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number (got {raw!r})") from exc


def _get_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = _get(env, key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer (got {raw!r})") from exc


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str
    version: str
    vps_url: str
    shared_secret: str
    health_host: str
    health_port: int
    local_token: str
    heartbeat_seconds: float
    reconnect_base_delay: float
    reconnect_max_delay: float
    state_dir: Path
    tunnel_enabled: bool

    @property
    def health_bind(self) -> str:
        return f"{self.health_host}:{self.health_port}"


def load_config(
    env_file: Optional[Path] = DEFAULT_ENV_FILE,
    environ: Optional[Mapping[str, str]] = None,
) -> AgentConfig:
    env = _merged_env(env_file, environ)
    errors: List[str] = []

    agent_id = _get(env, "AGENT_ID", "win-agent")
    if not AGENT_ID_RE.match(agent_id):
        errors.append(
            "AGENT_ID must be a lowercase slug (a-z, 0-9, hyphens; max 63 chars)"
        )

    version = _get(env, "AGENT_VERSION", "0.1.0") or "0.1.0"

    tunnel_enabled = _get_bool(env, "AGENT_TUNNEL_ENABLED", False)
    vps_url = _get(env, "AGENT_VPS_URL")
    shared_secret = _get(env, "AGENT_SHARED_SECRET")
    if tunnel_enabled:
        if not (vps_url.startswith("ws://") or vps_url.startswith("wss://")
                or vps_url.startswith("http://") or vps_url.startswith("https://")):
            errors.append(
                "AGENT_VPS_URL must be an http(s):// or ws(s):// URL "
                "when AGENT_TUNNEL_ENABLED=true"
            )
        if len(shared_secret) < MIN_SECRET_LEN:
            errors.append(
                f"AGENT_SHARED_SECRET must be at least {MIN_SECRET_LEN} "
                "characters when AGENT_TUNNEL_ENABLED=true"
            )
    elif vps_url and not (
        vps_url.startswith("ws://") or vps_url.startswith("wss://")
        or vps_url.startswith("http://") or vps_url.startswith("https://")
    ):
        errors.append("AGENT_VPS_URL must be an http(s):// or ws(s):// URL")

    health_host = (_get(env, "AGENT_LOCAL_HEALTH_HOST", "127.0.0.1") or "127.0.0.1").lower()
    if health_host not in LOOPBACK_HOSTS:
        errors.append(
            f"AGENT_LOCAL_HEALTH_HOST must be loopback only "
            f"(one of {sorted(LOOPBACK_HOSTS)}); refusing {health_host!r}"
        )

    health_port = _get_int(env, "AGENT_LOCAL_HEALTH_PORT", 8791)
    if not (1 <= health_port <= 65535):
        errors.append("AGENT_LOCAL_HEALTH_PORT must be between 1 and 65535")

    heartbeat = _get_float(env, "AGENT_HEARTBEAT_SECONDS", 15.0)
    if heartbeat < 1.0:
        errors.append("AGENT_HEARTBEAT_SECONDS must be >= 1")

    base_delay = _get_float(env, "AGENT_RECONNECT_BASE_DELAY", 1.0)
    max_delay = _get_float(env, "AGENT_RECONNECT_MAX_DELAY", 60.0)
    if base_delay < 0 or max_delay < 0:
        errors.append("Reconnect delays must be >= 0")
    elif base_delay > max_delay:
        errors.append("AGENT_RECONNECT_BASE_DELAY must be <= AGENT_RECONNECT_MAX_DELAY")

    state_dir_raw = _get(env, "AGENT_STATE_DIR")
    state_dir = Path(state_dir_raw) if state_dir_raw else DEFAULT_STATE_DIR
    if not state_dir.is_absolute():
        state_dir = (AGENT_DIR / state_dir).resolve()

    if errors:
        raise ConfigError("Invalid agent configuration: " + "; ".join(errors))

    return AgentConfig(
        agent_id=agent_id,
        version=version,
        vps_url=vps_url,
        shared_secret=shared_secret,
        health_host=health_host,
        health_port=health_port,
        local_token=_get(env, "AGENT_LOCAL_TOKEN"),
        heartbeat_seconds=heartbeat,
        reconnect_base_delay=base_delay,
        reconnect_max_delay=max_delay,
        state_dir=state_dir,
        tunnel_enabled=tunnel_enabled,
    )

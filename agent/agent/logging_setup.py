"""Structured logging with secret redaction.

Emits single-line JSON records. A redaction filter scrubs known secret
patterns from every message and every extra field before output.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any

_SECRET_ENV_KEYS = (
    "AGENT_SHARED_SECRET", "AGENT_LOCAL_TOKEN", "BROKER_SIDECAR_SECRET",
    "WORKER_HMAC_SECRET", "API_HASH", "TELEGRAM_SESSION_STRING",
)

_PATTERNS = [
    re.compile(r"(?i)(api_hash|session_string|shared_secret|local_token|"
               r"broker_sidecar_secret|worker_hmac_secret|password|passwd|"
               r"token|secret|api_key|apikey)\s*[:=]\s*\S+"),
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"),
    re.compile(r"\b\d{8,10}:[a-zA-Z0-9_\-]{30,}\b"),  # telegram bot tokens
]


def redact(text: str) -> str:
    out = text
    for pat in _PATTERNS:
        out = pat.sub(lambda m: _mask(m.group(0)), out)
    for key in _SECRET_ENV_KEYS:
        import os
        val = os.environ.get(key)
        if val and len(val) >= 8:
            out = out.replace(val, "***")
    return out


def _mask(match: str) -> str:
    # Keep the key name and separator spacing, mask the value:
    # "password=abc" -> "password=***", "api_key: abc" -> "api_key: ***"
    for sep in ("=", ":"):
        idx = match.find(sep)
        if idx != -1:
            space = " " if match[idx + 1:idx + 2] == " " else ""
            return match[: idx + 1] + space + "***"
    return "***"


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.msg))
        if record.args:
            record.args = tuple(
                redact(str(a)) if isinstance(a, str) else a for a in record.args
            )
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.created * 1000) % 1000:03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("agent_id", "state", "event", "correlation_id",
                    "error", "detail"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = redact(str(val)) if isinstance(val, str) else val
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", agent_id: str = "agent") -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger = logging.getLogger("quotex_agent")
    logger.info("logging initialized", extra={"agent_id": agent_id})
    return logger

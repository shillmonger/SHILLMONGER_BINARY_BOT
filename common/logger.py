"""Logging with automatic secret redaction.

Loggers are created as stand-alone ``logging.Logger`` objects (not registered in the
global logging hierarchy) so each broker has fully separate handlers and files.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable, Optional, Set

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

_KEY_VALUE = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|cookie|ssid|session[_-]?id|"
    r"authorization|api[_-]?key)\b(\s*[=:]\s*)([^\s,;&'\"]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


class Redactor:
    """Masks known secret values and common ``key=value`` credential patterns."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: Set[str] = set()
        for secret in secrets:
            self.add(secret)

    def add(self, secret: Optional[str]) -> None:
        if secret:
            self._secrets.add(secret)

    def __call__(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "***")
        text = _BEARER.sub("Bearer ***", text)  # first, so the token itself is masked
        return _KEY_VALUE.sub(r"\1\2***", text)


class RedactingFormatter(logging.Formatter):
    """Redacts the complete formatted record, including tracebacks."""

    def __init__(self, fmt: str, redactor: Redactor) -> None:
        super().__init__(fmt)
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redactor(super().format(record))


def build_logger(
    name: str,
    log_file: Path,
    *,
    level: str = "INFO",
    redactor: Optional[Redactor] = None,
) -> logging.Logger:
    """Create an isolated rotating-file logger. Console output is deliberately off
    because the terminal is used for the status dashboard."""
    logger = logging.Logger(name, level=getattr(logging, level.upper(), logging.INFO))
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8", delay=True
    )
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, redactor or Redactor()))
    logger.addHandler(handler)
    return logger


def close_logger(logger: logging.Logger) -> None:
    """Flush and close file handlers (required on Windows before deleting files)."""
    for handler in list(logger.handlers):
        try:
            handler.flush()
            handler.close()
        except Exception:  # pragma: no cover - best effort
            pass
        logger.removeHandler(handler)

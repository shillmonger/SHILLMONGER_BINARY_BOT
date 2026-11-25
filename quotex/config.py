"""Quotex configuration. Reads ONLY variables starting with ``QUOTEX_``."""

from __future__ import annotations

from typing import Mapping

from ..common.config import BrokerSettings, read_broker_settings

ENV_PREFIX = "QUOTEX"
BROKER_LABEL = "QUOTEX"
LOG_FILE_NAME = "quotex.log"


def load_quotex_settings(env: Mapping[str, str]) -> BrokerSettings:
    return read_broker_settings(env, ENV_PREFIX)

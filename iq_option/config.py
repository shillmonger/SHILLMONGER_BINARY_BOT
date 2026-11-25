"""IQ Option configuration. Reads ONLY variables starting with ``IQ_OPTION_``."""

from __future__ import annotations

from typing import Mapping

from ..common.config import BrokerSettings, read_broker_settings

ENV_PREFIX = "IQ_OPTION"
BROKER_LABEL = "IQ OPTION"
LOG_FILE_NAME = "iq_option.log"


def load_iq_option_settings(env: Mapping[str, str]) -> BrokerSettings:
    return read_broker_settings(env, ENV_PREFIX)

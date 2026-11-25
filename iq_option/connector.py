"""IQ Option connector: own settings, own log file, own state, own task."""

from __future__ import annotations

from typing import Mapping

from ..common.adapters import BrokerAdapter
from ..common.base_connector import BaseConnector
from ..common.config import BrokerSettings
from ..common.models import HealthResult
from .config import BROKER_LABEL, ENV_PREFIX, LOG_FILE_NAME, load_iq_option_settings
from .health_check import check_iq_option_health


class IQOptionConnector(BaseConnector):
    LABEL = BROKER_LABEL
    KEY = "iq_option"
    LOG_FILE = LOG_FILE_NAME
    UNSUPPORTED_REASON = (
        "No officially supported public trading API is known for IQ Option, so no "
        f"connection was attempted. Set {ENV_PREFIX}_ADAPTER to an authorized "
        "adapter (see README)."
    )

    @classmethod
    def read_settings(cls, env: Mapping[str, str]) -> BrokerSettings:
        return load_iq_option_settings(env)

    async def check_health(self, adapter: BrokerAdapter) -> HealthResult:
        return await check_iq_option_health(adapter, self.settings)

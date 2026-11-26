"""Entrypoint: ``python -m agent`` from the agent/ directory."""

from __future__ import annotations

import asyncio
import sys

from .config import ConfigError, load_config
from .logging_setup import setup_logging
from .service import run_service


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        # Logging isn't configured yet — emit a plain stderr line.
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    logger = setup_logging("INFO", agent_id=config.agent_id)
    try:
        return asyncio.run(run_service(config))
    except KeyboardInterrupt:
        logger.info("interrupted")
        return 0


if __name__ == "__main__":
    sys.exit(main())

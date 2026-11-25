#!/usr/bin/env python3
"""Launcher for panel deployments (Pterodactyl-style eggs that run a single
PY_FILE from the repo root). Equivalent to `python -m broker_sidecar.main`
executed inside sidecar/."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sidecar"))

from broker_sidecar.main import main  # noqa: E402

if __name__ == "__main__":
    main()

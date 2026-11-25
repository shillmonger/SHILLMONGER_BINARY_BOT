"""Makes ``import BINARY_TRADING_BOTS`` work when tests run from any directory."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent  # folder that contains BINARY_TRADING_BOTS/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

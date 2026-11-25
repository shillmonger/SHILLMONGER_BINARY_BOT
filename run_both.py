#!/usr/bin/env python3
"""RUN_BOTH.py - start and monitor the IQ Option and Quotex connection services.

Run from the project root:      python BINARY_TRADING_BOTS/RUN_BOTH.py
or from inside the folder:      python RUN_BOTH.py

Stage 1 only: DEMO connection + health monitoring. No trading, no Telegram, no signals.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, TextIO

_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PACKAGE_DIR.parent


def _bootstrap_imports(remove_script_dir: bool) -> None:
    """Make ``import BINARY_TRADING_BOTS`` work from any working directory.

    When run as a script, Python puts this folder first on sys.path; that would let
    its generic sub-package names (``common``, ``tests`` ...) shadow modules of the
    host project, so it is removed.
    """
    if remove_script_dir:
        target = os.path.normcase(str(_PACKAGE_DIR))
        sys.path[:] = [
            p for p in sys.path if os.path.normcase(os.path.abspath(p or os.getcwd())) != target
        ]
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))


_bootstrap_imports(remove_script_dir=(__name__ == "__main__"))

from BINARY_TRADING_BOTS.common.adapters import SimulatedAdapter  # noqa: E402
from BINARY_TRADING_BOTS.common.base_connector import AdapterFactory  # noqa: E402
from BINARY_TRADING_BOTS.common.config import (  # noqa: E402
    DEFAULT_ENV_FILE,
    build_environment,
    read_manager_settings,
)
from BINARY_TRADING_BOTS.common.connection_manager import ConnectionManager  # noqa: E402
from BINARY_TRADING_BOTS.common.logger import build_logger  # noqa: E402
from BINARY_TRADING_BOTS.iq_option.connector import IQOptionConnector  # noqa: E402
from BINARY_TRADING_BOTS.quotex.connector import QuotexConnector  # noqa: E402


def simulated_factories() -> Dict[str, AdapterFactory]:
    """Fake adapters for ``--simulate``. Quotex 'drops' every 20 s so the
    reconnection cycle is visible. Nothing here touches a network."""
    return {
        "iq_option": lambda s, log: SimulatedAdapter(s, log, latency_ms=42.0),
        "quotex": lambda s, log: SimulatedAdapter(s, log, latency_ms=65.0, drop_after=20.0),
    }


def build_manager(
    env: Mapping[str, str],
    *,
    simulate: bool = False,
    adapter_factories: Optional[Mapping[str, AdapterFactory]] = None,
) -> ConnectionManager:
    """Load configuration and create (but do not start) both connectors."""
    manager_settings = read_manager_settings(env)
    manager_logger = build_logger(
        "MANAGER",
        manager_settings.log_dir / "manager.log",
        level=manager_settings.log_level,
    )
    for problem in manager_settings.errors:
        manager_logger.warning("Configuration problem (default used): %s", problem)
    factories = dict(simulated_factories() if simulate else {})
    factories.update(adapter_factories or {})
    connectors = []
    for cls in (IQOptionConnector, QuotexConnector):
        connectors.append(
            cls.from_env(
                env,
                log_dir=manager_settings.log_dir,
                log_level=manager_settings.log_level,
                adapter_factory=factories.get(cls.KEY),
            )
        )
    return ConnectionManager(connectors, manager_logger)


class _DashboardPrinter:
    def __init__(self, out: TextIO, clear: bool) -> None:
        self.out = out
        self.ansi = clear and bool(getattr(out, "isatty", lambda: False)())
        self._last = ""
        if self.ansi and os.name == "nt":
            os.system("")  # enables ANSI escape handling in Windows terminals

    def show(self, text: str, force: bool = False) -> None:
        if self.ansi:
            self.out.write("\033[2J\033[H" + text + "\n")
        elif force or text != self._last:
            self.out.write(text + "\n\n")
        self.out.flush()
        self._last = text

    def note(self, text: str) -> None:
        self.out.write(text + "\n")
        self.out.flush()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event):
    """CTRL+C / SIGTERM request a graceful stop; a second CTRL+C aborts immediately.
    Uses signal.signal (not loop.add_signal_handler) so it also works on Windows."""
    previous = {}

    def handler(signum, frame):  # noqa: ARG001
        if stop_event.is_set():
            raise KeyboardInterrupt
        loop.call_soon_threadsafe(stop_event.set)

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):  # not in the main thread
            continue

    def restore() -> None:
        for sig, old in previous.items():
            try:
                signal.signal(sig, old if old is not None else signal.SIG_DFL)
            except (ValueError, OSError):
                pass

    return restore


async def run_manager(
    manager: ConnectionManager,
    *,
    refresh: float = 1.0,
    duration: Optional[float] = None,
    stop_event: Optional[asyncio.Event] = None,
    out: Optional[TextIO] = None,
    clear: bool = True,
    install_signals: bool = True,
) -> None:
    """Start the manager, refresh the dashboard, and shut down gracefully."""
    loop = asyncio.get_running_loop()
    stop_event = stop_event or asyncio.Event()
    restore = _install_signal_handlers(loop, stop_event) if install_signals else (lambda: None)
    printer = _DashboardPrinter(out or sys.stdout, clear)
    started = loop.time()
    try:
        await manager.start()
        while not stop_event.is_set():
            manager.observe()
            printer.show(manager.render_dashboard())
            if duration is not None and loop.time() - started >= duration:
                break
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=refresh)
            except asyncio.TimeoutError:
                pass
    finally:
        restore()
        printer.note("\nShutting down brokers gracefully...")
        await manager.stop()
        manager.observe()
        printer.show(manager.render_dashboard(), force=True)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start and monitor IQ Option + Quotex DEMO connections (no trading)."
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use SIMULATED adapters (no real broker session) to preview the dashboard",
    )
    parser.add_argument("--duration", type=float, default=None, help="stop automatically after N seconds")
    parser.add_argument("--refresh", type=float, default=None, help="dashboard refresh interval in seconds")
    parser.add_argument("--env-file", default=None, help="path to a .env file (default: BINARY_TRADING_BOTS/.env)")
    parser.add_argument("--no-clear", action="store_true", help="append output instead of redrawing the screen")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None, env: Optional[Mapping[str, str]] = None) -> int:
    args = parse_args(argv)
    env_file = Path(args.env_file) if args.env_file else DEFAULT_ENV_FILE
    environment = build_environment(env_file, env)
    manager = build_manager(environment, simulate=args.simulate)
    refresh = args.refresh or read_manager_settings(environment).refresh_interval
    try:
        asyncio.run(
            run_manager(manager, refresh=refresh, duration=args.duration, clear=not args.no_clear)
        )
    except KeyboardInterrupt:
        print("\nInterrupted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

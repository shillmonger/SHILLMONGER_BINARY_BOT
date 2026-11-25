"""run_both.py and ConnectionManager tests - SIMULATED only.

These tests never contact a broker, need no credentials and cannot trade.
"""

import _bootstrap  # noqa: F401
import asyncio
import contextlib
import importlib.util
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from _support import CrashingAdapter, fast_env, sim_factory, wait_until
from BINARY_TRADING_BOTS.common.models import ConnectionState as S

_SCRIPT = Path(__file__).resolve().parent.parent / "run_both.py"
_spec = importlib.util.spec_from_file_location("RUN_BOTH_under_test", _SCRIPT)
RUN_BOTH = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RUN_BOTH)


def make_env(tmp, **extra):
    env = {"BROKER_LOG_DIR": tmp, "BROKER_DASHBOARD_REFRESH": "0.05"}
    env.update(fast_env("IQ_OPTION"))
    env.update(fast_env("QUOTEX"))
    env.update(extra)
    return env


class TestRunBoth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="run_both_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def manager(self, **kwargs):
        m = RUN_BOTH.build_manager(make_env(self.tmp), **kwargs)

        async def shutdown():
            await m.stop()

        self.addAsyncCleanup(shutdown)
        return m

    async def run_for(self, manager, duration=0.4, **kwargs):
        out = io.StringIO()
        await RUN_BOTH.run_manager(
            manager, refresh=0.05, duration=duration, out=out, clear=False, install_signals=False, **kwargs
        )
        return out.getvalue()

    # ------------------------------------------------------------ initialization
    async def test_initializes_both_brokers_independently(self):
        m = self.manager()
        self.assertEqual(list(m.connectors), ["iq_option", "quotex"])
        self.assertEqual(m.get_state("iq_option"), S.DISCONNECTED)
        self.assertEqual(m.get_state("quotex"), S.DISCONNECTED)
        self.assertIsNot(m.connectors["iq_option"].logger, m.connectors["quotex"].logger)
        self.assertIsNot(m.connectors["iq_option"].settings, m.connectors["quotex"].settings)

    # ------------------------------------------------------- honest status output
    async def test_default_run_never_claims_connected(self):
        m = self.manager()
        output = await self.run_for(m)
        self.assertIn("BROKER CONNECTION MANAGER", output)
        self.assertIn("Status: UNSUPPORTED", output)
        self.assertNotIn("Status: CONNECTED", output)
        self.assertIn("Account Mode: DEMO", output)
        self.assertIn("Latency: UNKNOWN", output)

    async def test_simulated_run_is_labelled_as_simulation(self):
        m = self.manager(simulate=True)
        output = await self.run_for(m, duration=0.5)
        self.assertIn("SIMULATION MODE", output)
        self.assertIn("Status: CONNECTED (SIMULATED)", output)
        self.assertIn("Monitoring: ACTIVE", output)
        self.assertIn("Manager Status: RUNNING", output)
        self.assertIn("Manager Status: STOPPED", output)  # final dashboard after shutdown

    # ------------------------------------------------------- independent failures
    async def test_one_broker_failing_does_not_affect_the_other(self):
        created = []
        m = self.manager(
            adapter_factories={
                "iq_option": lambda s, l: CrashingAdapter(s, l),
                "quotex": sim_factory(created),
            }
        )
        await m.start()
        self.assertTrue(await wait_until(lambda: m.get_state("iq_option") is S.ERROR))
        self.assertTrue(await wait_until(lambda: m.get_state("quotex") is S.CONNECTED))
        await asyncio.sleep(0.2)
        self.assertEqual(m.get_state("quotex"), S.CONNECTED)  # still healthy
        self.assertTrue(m.connectors["quotex"].is_monitoring)

    async def test_factory_that_raises_is_contained_per_broker(self):
        def explode(settings, logger):
            raise RuntimeError("factory exploded")

        created = []
        m = self.manager(adapter_factories={"iq_option": explode, "quotex": sim_factory(created)})
        await m.start()
        self.assertEqual(m.get_state("iq_option"), S.ERROR)
        self.assertTrue(await wait_until(lambda: m.get_state("quotex") is S.CONNECTED))

    async def test_start_failure_in_one_connector_is_contained(self):
        created = []
        m = self.manager(adapter_factories={"quotex": sim_factory(created)})

        async def broken_start():
            raise RuntimeError("start exploded")

        m.connectors["iq_option"].start = broken_start
        await m.start()
        self.assertEqual(m.get_state("iq_option"), S.ERROR)
        self.assertTrue(await wait_until(lambda: m.get_state("quotex") is S.CONNECTED))

    # --------------------------------------------------------- graceful shutdown
    async def test_graceful_shutdown_on_stop_event(self):
        created = []
        m = self.manager(adapter_factories={"iq_option": sim_factory(created), "quotex": sim_factory(created)})
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.3, stop.set)  # stands in for CTRL+C
        await self.run_for(m, duration=None, stop_event=stop)
        self.assertEqual(m.phase, "STOPPED")
        for connector in m.connectors.values():
            self.assertFalse(connector.is_monitoring)
            self.assertEqual(connector.state, S.DISCONNECTED)
        self.assertTrue(all(a.disconnect_calls >= 1 for a in created))
        manager_log = (Path(self.tmp) / "manager.log").read_text(encoding="utf-8")
        self.assertIn("Manager stopped", manager_log)


class TestRunBothCli(unittest.TestCase):
    def test_main_runs_simulated_and_creates_all_three_logs(self):
        tmp = tempfile.mkdtemp(prefix="run_both_cli_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = RUN_BOTH.main(
                ["--simulate", "--duration", "0.5", "--no-clear", "--env-file", str(Path(tmp) / "none.env")],
                env={"BROKER_LOG_DIR": tmp, "BROKER_DASHBOARD_REFRESH": "0.1"},
            )
        self.assertEqual(code, 0)
        self.assertIn("BROKER CONNECTION MANAGER", buffer.getvalue())
        for name in ("iq_option.log", "quotex.log", "manager.log"):
            self.assertTrue((Path(tmp) / name).exists(), name)


if __name__ == "__main__":
    unittest.main()

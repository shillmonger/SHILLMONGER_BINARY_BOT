from agent.config import load_config
from agent.diagnostics import diagnostics_summary, run_diagnostics


def _cfg(tmp_path, **over):
    env = {
        "AGENT_ID": "diag-test",
        "AGENT_LOCAL_HEALTH_PORT": "18792",
        "AGENT_STATE_DIR": str(tmp_path / "state"),
    }
    env.update(over)
    return load_config(env_file=None, environ=env)


def test_all_checks_pass_with_valid_config(tmp_path):
    cfg = _cfg(tmp_path)
    checks = run_diagnostics(cfg, include_port=True)
    names = {c.name for c in checks}
    assert {"config", "python_version", "dependencies",
            "state_dir", "health_bind", "health_port"} <= names
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    assert diagnostics_summary(checks)["ok"] is True


def test_state_dir_created_and_writable(tmp_path):
    cfg = _cfg(tmp_path)
    checks = {c.name: c for c in run_diagnostics(cfg)}
    assert checks["state_dir"].ok
    assert (tmp_path / "state").is_dir()


def test_summary_shape(tmp_path):
    cfg = _cfg(tmp_path)
    s = diagnostics_summary(run_diagnostics(cfg))
    assert s["ok"] is True
    assert isinstance(s["checks"], list) and s["checks"]
    assert "checkedAt" in s


def test_no_network_calls_by_design():
    # Diagnostics must never probe broker endpoints — this test exists so a
    # future change that adds network checks is a deliberate, reviewed act.
    import inspect
    import agent.diagnostics as d
    src = inspect.getsource(d)
    for banned in ("requests", "httpx", "socket.create_connection",
                   "websocket", "quotex.com"):
        assert banned not in src, f"diagnostics must not use {banned}"

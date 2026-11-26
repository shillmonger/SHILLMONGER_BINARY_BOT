import pytest

from agent.config import ConfigError, load_config


def env(**over):
    base = {
        "AGENT_ID": "test-agent",
        "AGENT_VERSION": "9.9.9",
        "AGENT_LOCAL_HEALTH_PORT": "18791",
    }
    base.update(over)
    return base


def test_minimal_valid_config():
    cfg = load_config(env_file=None, environ=env())
    assert cfg.agent_id == "test-agent"
    assert cfg.version == "9.9.9"
    assert cfg.health_host == "127.0.0.1"
    assert cfg.health_port == 18791
    assert cfg.tunnel_enabled is False
    assert cfg.heartbeat_seconds == 15.0


def test_missing_agent_id_uses_default():
    e = env()
    del e["AGENT_ID"]
    cfg = load_config(env_file=None, environ=e)
    assert cfg.agent_id == "win-agent"


def test_bad_agent_id_rejected():
    with pytest.raises(ConfigError, match="AGENT_ID"):
        load_config(env_file=None, environ=env(AGENT_ID="Bad_ID!"))


def test_non_loopback_health_host_rejected():
    with pytest.raises(ConfigError, match="loopback"):
        load_config(env_file=None, environ=env(AGENT_LOCAL_HEALTH_HOST="0.0.0.0"))


def test_localhost_alias_accepted():
    cfg = load_config(env_file=None, environ=env(AGENT_LOCAL_HEALTH_HOST="localhost"))
    assert cfg.health_host == "localhost"


def test_bad_port_rejected():
    with pytest.raises(ConfigError, match="HEALTH_PORT"):
        load_config(env_file=None, environ=env(AGENT_LOCAL_HEALTH_PORT="70000"))


def test_tunnel_enabled_requires_url_and_secret():
    with pytest.raises(ConfigError):
        load_config(env_file=None, environ=env(AGENT_TUNNEL_ENABLED="true"))


def test_tunnel_enabled_valid():
    cfg = load_config(
        env_file=None,
        environ=env(
            AGENT_TUNNEL_ENABLED="true",
            AGENT_VPS_URL="http://vps.example.com:25590",
            AGENT_SHARED_SECRET="x" * 40,
        ),
    )
    assert cfg.tunnel_enabled is True
    assert cfg.vps_url == "http://vps.example.com:25590"


def test_short_secret_rejected_when_tunnel_enabled():
    with pytest.raises(ConfigError, match="SHARED_SECRET"):
        load_config(
            env_file=None,
            environ=env(
                AGENT_TUNNEL_ENABLED="true",
                AGENT_VPS_URL="http://vps.example.com:25590",
                AGENT_SHARED_SECRET="short",
            ),
        )


def test_bad_vps_url_rejected():
    with pytest.raises(ConfigError, match="AGENT_VPS_URL"):
        load_config(env_file=None, environ=env(AGENT_VPS_URL="notaurl"))


def test_ws_url_accepted():
    cfg = load_config(env_file=None, environ=env(AGENT_VPS_URL="wss://vps.example.com"))
    assert cfg.vps_url == "wss://vps.example.com"


def test_reconnect_base_must_not_exceed_max():
    with pytest.raises(ConfigError, match="BASE_DELAY"):
        load_config(
            env_file=None,
            environ=env(AGENT_RECONNECT_BASE_DELAY="60", AGENT_RECONNECT_MAX_DELAY="1"),
        )


def test_heartbeat_minimum():
    with pytest.raises(ConfigError, match="HEARTBEAT"):
        load_config(env_file=None, environ=env(AGENT_HEARTBEAT_SECONDS="0.1"))


def test_env_file_not_required():
    cfg = load_config(env_file=None, environ=env())
    assert cfg.agent_id == "test-agent"

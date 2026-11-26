import logging

from agent.logging_setup import RedactionFilter, redact


def test_redact_password_assignment():
    assert redact("password=hunter2") == "password=***"
    assert redact("api_key: abc123xyz") == "api_key: ***"


def test_redact_bearer_token():
    out = redact("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out


def test_redact_env_secret_value():
    import os
    os.environ["AGENT_SHARED_SECRET"] = "supersecretvalue123"
    try:
        out = redact("token is supersecretvalue123 ok")
        assert "supersecretvalue123" not in out
    finally:
        del os.environ["AGENT_SHARED_SECRET"]


def test_redact_telegram_bot_token_shape():
    out = redact("bot 123456789:AAEhBOweik6ad9r_QXMENQj1nTcZ2X1v-abc hit")
    assert "AAEhBOweik6ad9r" not in out


def test_filter_scrubs_log_record():
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="connect failed password=secret123", args=(), exc_info=None,
    )
    assert RedactionFilter().filter(record)
    assert "secret123" not in record.getMessage()
    assert "password=***" in record.getMessage()

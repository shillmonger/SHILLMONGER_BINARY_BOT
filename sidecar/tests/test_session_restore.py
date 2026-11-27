"""Session auto-restore tests — no network, no real credentials."""

import hashlib
import hmac
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("BROKER_SIDECAR_SECRET", "test-secret")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from broker_sidecar import session_restore  # noqa: E402


def test_enabled_default_on(monkeypatch):
    monkeypatch.delenv("BROKER_SESSION_RESTORE", raising=False)
    assert session_restore.enabled() is True


@pytest.mark.parametrize("v", ["off", "0", "false", "no", "OFF"])
def test_enabled_off_values(monkeypatch, v):
    monkeypatch.setenv("BROKER_SESSION_RESTORE", v)
    assert session_restore.enabled() is False


def test_restore_url_explicit_wins(monkeypatch):
    monkeypatch.setenv("SESSION_RESTORE_URL", "http://app.local/custom")
    monkeypatch.setenv("BINARY_INGEST_URL", "http://other/ingest")
    assert session_restore.restore_url() == "http://app.local/custom"


def test_restore_url_derived_from_ingest(monkeypatch):
    monkeypatch.delenv("SESSION_RESTORE_URL", raising=False)
    monkeypatch.setenv(
        "BINARY_INGEST_URL",
        "http://localhost:3000/api/internal/binary-telegram/ingest",
    )
    assert session_restore.restore_url() == (
        "http://localhost:3000/api/internal/binary-brokers/restore-sessions"
    )


def test_restore_url_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("SESSION_RESTORE_URL", raising=False)
    monkeypatch.delenv("BINARY_INGEST_URL", raising=False)
    assert session_restore.restore_url() is None


def test_sign_matches_worker_scheme():
    raw, ts, secret = "", 1700000000, "s3cret"
    expected = hmac.new(
        secret.encode(), f"{ts}.{raw}".encode(), hashlib.sha256
    ).hexdigest()
    assert session_restore._sign(raw, ts, secret) == expected


def test_fetch_returns_empty_when_unconfigured(monkeypatch):
    monkeypatch.delenv("SESSION_RESTORE_URL", raising=False)
    monkeypatch.delenv("BINARY_INGEST_URL", raising=False)
    monkeypatch.delenv("WORKER_HMAC_SECRET", raising=False)
    assert session_restore.fetch_restore_sessions() == []


def test_fetch_parses_sessions(monkeypatch):
    monkeypatch.setenv("SESSION_RESTORE_URL", "http://app.local/restore")
    monkeypatch.setenv("WORKER_HMAC_SECRET", "s3cret")

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "ok": True,
                "sessions": [
                    {"broker": "iqoption", "userId": "u1", "email": "a@b.c", "password": "x"}
                ],
            }

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return FakeResp()

    monkeypatch.setattr("requests.get", fake_get)
    sessions = session_restore.fetch_restore_sessions()
    assert captured["url"] == "http://app.local/restore"
    assert "X-Signature" in captured["headers"]
    assert "X-Timestamp" in captured["headers"]
    assert sessions == [
        {"broker": "iqoption", "userId": "u1", "email": "a@b.c", "password": "x"}
    ]

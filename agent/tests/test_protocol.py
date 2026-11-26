"""Protocol envelope tests — signing, validation, replay/freshness."""

import json
import time

import pytest

from agent import protocol as p


def test_envelope_roundtrip():
    env = p.build_envelope("HEARTBEAT", {"agentId": "a1"}, secret="s" * 40)
    parsed = p.parse_envelope(json.dumps(env))
    assert parsed.type == "HEARTBEAT"
    assert parsed.payload == {"agentId": "a1"}
    assert parsed.v == 1


def test_signature_verifies_and_detects_tamper():
    secret = "s" * 40
    env = p.build_envelope("STATUS", {"x": 1}, secret=secret)
    assert p.verify_frame(secret, env["ts"], env["id"], env["type"],
                          env["payload"], env["sig"])
    # Tampered payload must fail.
    bad = dict(env["payload"], x=2)
    assert not p.verify_frame(secret, env["ts"], env["id"], env["type"],
                              bad, env["sig"])
    # Wrong secret must fail.
    assert not p.verify_frame("t" * 40, env["ts"], env["id"], env["type"],
                              env["payload"], env["sig"])


def test_stale_timestamp_rejected():
    env = p.build_envelope("PING", {}, secret="s" * 40,
                           ts=int(time.time()) - 3600)
    with pytest.raises(p.ProtocolError):
        p.parse_envelope(json.dumps(env))


def test_malformed_json_rejected():
    with pytest.raises(p.ProtocolError):
        p.parse_envelope("{not json")


def test_oversized_frame_rejected():
    env = p.build_envelope("PING", {"blob": "x" * (p.MAX_FRAME_BYTES + 10)},
                           secret="s" * 40)
    with pytest.raises(p.ProtocolError):
        p.parse_envelope(json.dumps(env))


def test_bad_version_rejected():
    env = p.build_envelope("PING", {}, secret="s" * 40)
    env["v"] = 2
    with pytest.raises(p.ProtocolError):
        p.parse_envelope(json.dumps(env))


def test_extra_envelope_field_rejected():
    env = p.build_envelope("PING", {}, secret="s" * 40)
    env["extra"] = "field"
    with pytest.raises(p.ProtocolError):
        p.parse_envelope(json.dumps(env))


def test_verb_allowlist():
    assert p.verb_allowed("sidecar_to_agent", "STATUS")
    assert p.verb_allowed("sidecar_to_agent", "RESTART")
    assert not p.verb_allowed("sidecar_to_agent", "SHELL")
    assert not p.verb_allowed("sidecar_to_agent", "HEARTBEAT")
    assert p.verb_allowed("agent_to_sidecar", "HEARTBEAT")
    assert p.verb_allowed("agent_to_sidecar", "PONG")
    assert not p.verb_allowed("agent_to_sidecar", "RESTART")
    assert not p.verb_allowed("agent_to_sidecar", "DELETE_ALL")


def test_correlation_ids_unique():
    ids = {p.new_correlation_id() for _ in range(100)}
    assert len(ids) == 100


def test_error_payload_shape():
    body = p.error_payload(p.ErrorCode.TIMEOUT, "too slow", "corr-1")
    assert body["error"]["code"] == "TIMEOUT"
    assert body["correlationId"] == "corr-1"

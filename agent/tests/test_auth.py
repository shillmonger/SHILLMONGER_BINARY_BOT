import time

from agent.auth import (
    NonceCache,
    build_auth_message,
    canonical_json,
    new_nonce,
    sign,
    sign_message,
    verify_auth_message,
    verify_message,
    verify_signature,
)

SECRET = "s" * 40
AGENT = "win-home-01"


def test_sign_verify_roundtrip():
    msg = "hello.world"
    sig = sign(SECRET, msg)
    assert verify_signature(SECRET, msg, sig)
    assert not verify_signature("other" + "s" * 33, msg, sig)
    assert not verify_signature(SECRET, msg + "x", sig)


def test_auth_message_roundtrip():
    msg, ts, nonce = build_auth_message(AGENT)
    sig = sign(SECRET, msg)
    assert verify_auth_message(SECRET, AGENT, msg, sig)
    assert ts.isdigit() and len(nonce) == 32


def test_auth_rejects_wrong_agent():
    msg, _, _ = build_auth_message(AGENT)
    sig = sign(SECRET, msg)
    assert not verify_auth_message(SECRET, "other-agent", msg, sig)


def test_auth_rejects_stale_timestamp():
    old = int(time.time()) - 120
    msg, _, _ = build_auth_message(AGENT, timestamp=old)
    sig = sign(SECRET, msg)
    assert not verify_auth_message(SECRET, AGENT, msg, sig)


def test_auth_rejects_future_timestamp():
    future = int(time.time()) + 120
    msg, _, _ = build_auth_message(AGENT, timestamp=future)
    sig = sign(SECRET, msg)
    assert not verify_auth_message(SECRET, AGENT, msg, sig)


def test_auth_rejects_bad_signature():
    msg, _, _ = build_auth_message(AGENT)
    assert not verify_auth_message(SECRET, AGENT, msg, "0" * 64)


def test_auth_rejects_malformed_message():
    assert not verify_auth_message(SECRET, AGENT, "not-a-message", "x" * 64)
    assert not verify_auth_message(SECRET, AGENT, "a.b", "x" * 64)


def test_message_signing_roundtrip():
    payload = {"type": "PING", "v": 1}
    ts = int(time.time())
    msg, sig = sign_message(SECRET, ts, "corr-1", payload)
    assert verify_message(SECRET, msg, sig)
    # tampered payload breaks signature
    msg2, _ = sign_message(SECRET, ts, "corr-1", {"type": "PING", "v": 2})
    assert not verify_message(SECRET, msg2, sig)


def test_canonical_json_stable():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert canonical_json({"a": 1}) == canonical_json(dict(a=1))


def test_nonce_cache_detects_replay():
    cache = NonceCache(max_age_seconds=60)
    n = new_nonce()
    assert cache.seen(n) is False   # first sighting
    assert cache.seen(n) is True    # replay
    other = new_nonce()
    assert cache.seen(other) is False


def test_nonce_cache_evicts_stale():
    cache = NonceCache(max_age_seconds=10)
    cache.seen("fixed-nonce", now=1000.0)
    # 11s later the entry has aged out, so the nonce reads as new again
    assert cache.seen("fixed-nonce", now=1011.0) is False

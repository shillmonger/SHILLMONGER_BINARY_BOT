"""Tunnel wire protocol — shared envelope, verbs, signing, validation.

Envelope (JSON text frame, both directions):

  {"v":1,"id":"<correlation>","type":"<VERB>","ts":<unix>,"payload":{...},"sig":"<hmac>"}

Frame signature (post-AUTH): HMAC_SHA256(key, "{ts}.{id}.{type}.{canonical(payload)}")
  key = per-session token issued in AUTH_OK.

AUTH handshake (first frame, signed with AGENT_SHARED_SECRET):
  type=AUTH, payload={agentId, timestamp, nonce, signature}
  signature = HMAC_SHA256(secret, "{agentId}.{timestamp}.{nonce}")

Sidecar -> agent verbs:  STATUS, HEALTH, RESTART, PING
Agent -> sidecar verbs:  HEARTBEAT, STATUS, HEALTH, PONG
Responses reuse the request type+id with payload.result or payload.error.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024
TIMESTAMP_SKEW_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 15.0


class Verb(str, Enum):
    # sidecar -> agent
    STATUS = "STATUS"
    HEALTH = "HEALTH"
    RESTART = "RESTART"
    PING = "PING"
    # agent -> sidecar
    HEARTBEAT = "HEARTBEAT"
    PONG = "PONG"


AGENT_TO_SIDECAR = {Verb.HEARTBEAT, Verb.STATUS, Verb.HEALTH, Verb.PONG}
SIDECAR_TO_AGENT = {Verb.STATUS, Verb.HEALTH, Verb.RESTART, Verb.PING}


class ErrorCode(str, Enum):
    UNAUTHORIZED = "UNAUTHORIZED"
    MALFORMED = "MALFORMED"
    UNKNOWN_TYPE = "UNKNOWN_TYPE"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    INTERNAL = "INTERNAL"
    BAD_STATE = "BAD_STATE"


class ProtocolError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    v: int = PROTOCOL_VERSION
    id: str = Field(min_length=1, max_length=64)
    type: str = Field(min_length=1, max_length=32)
    ts: int
    payload: dict[str, Any] = Field(default_factory=dict)
    sig: str = Field(min_length=64, max_length=64)

    @field_validator("v")
    @classmethod
    def _v(cls, v: int) -> int:
        if v != PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol version {v}")
        return v


class AuthPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agentId: str = Field(min_length=1, max_length=64)
    timestamp: int
    nonce: str = Field(min_length=8, max_length=128)
    signature: str = Field(min_length=64, max_length=64)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True,
                      ensure_ascii=False)


def sign_frame(secret: str, ts: int, frame_id: str, verb: str,
               payload: dict) -> str:
    message = f"{ts}.{frame_id}.{verb}.{canonical_json(payload)}"
    return hmac.new(secret.encode(), message.encode(),
                    hashlib.sha256).hexdigest()


def verify_frame(secret: str, ts: int, frame_id: str, verb: str,
                  payload: dict, signature: str) -> bool:
    expected = sign_frame(secret, ts, frame_id, verb, payload)
    a, b = expected.encode(), (signature or "").encode()
    return len(a) == len(b) and hmac.compare_digest(a, b)


def build_envelope(verb: str, payload: Optional[dict] = None,
                   *, secret: str, frame_id: Optional[str] = None,
                   ts: Optional[int] = None) -> dict:
    """Build a signed outbound envelope."""
    ts_val = int(ts if ts is not None else time.time())
    fid = frame_id or new_correlation_id()
    body = dict(payload or {})
    return {
        "v": PROTOCOL_VERSION,
        "id": fid,
        "type": verb,
        "ts": ts_val,
        "payload": body,
        "sig": sign_frame(secret, ts_val, fid, verb, body),
    }


def parse_envelope(raw: str) -> Envelope:
    if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ProtocolError(ErrorCode.MALFORMED, "frame too large")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(ErrorCode.MALFORMED, "invalid JSON") from exc
    try:
        env = Envelope.model_validate(data)
    except Exception as exc:
        raise ProtocolError(ErrorCode.MALFORMED, f"bad envelope: {exc}") from exc
    now = int(time.time())
    if abs(now - env.ts) > TIMESTAMP_SKEW_SECONDS:
        raise ProtocolError(ErrorCode.MALFORMED, "stale timestamp")
    return env


def verb_allowed(direction: str, verb: str) -> bool:
    try:
        v = Verb(verb)
    except ValueError:
        return False
    allowed = AGENT_TO_SIDECAR if direction == "agent_to_sidecar" else SIDECAR_TO_AGENT
    return v in allowed


def error_payload(code: ErrorCode, message: str,
                  correlation_id: Optional[str] = None) -> dict:
    return {
        "error": {"code": code.value, "message": message[:300]},
        "correlationId": correlation_id,
    }

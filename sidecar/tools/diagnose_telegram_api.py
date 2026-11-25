"""Safely diagnose local sidecar auth and Telegram status/groups endpoints.

Run from ``sidecar`` with the API on localhost:
    python tools/diagnose_telegram_api.py

The configured broker key is used only in the local request header and never
printed. Group names, usernames, IDs, and account identity are not displayed.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Optional

try:
    from dotenv import load_dotenv
except ImportError as exc:  # pragma: no cover - sidecar requirements provide it
    raise SystemExit("python-dotenv is required; install sidecar requirements") from exc


def load_sidecar_environment() -> None:
    sidecar_dir = Path(__file__).resolve().parents[1]
    load_dotenv(sidecar_dir / ".env")
    load_dotenv(sidecar_dir.parent / ".env")


def request_json(
    base_url: str, path: str, secret: Optional[str] = None
) -> tuple[int, dict[str, Any]]:
    headers = {"X-Broker-Key": secret} if secret is not None else {}
    request = urllib.request.Request(f"{base_url}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {}
        return exc.code, body


def main() -> int:
    load_sidecar_environment()
    secret = os.environ.get("BROKER_SIDECAR_SECRET", "")
    base_url = (
        f"http://{os.environ.get('BROKER_SIDECAR_HOST', '127.0.0.1')}:"
        f"{os.environ.get('BROKER_SIDECAR_PORT', '8790')}"
    )
    print(f"Configured broker key present: {'yes' if secret else 'no'}")

    try:
        health_status, health = request_json(base_url, "/health")
    except (OSError, ValueError) as exc:
        print(f"Local sidecar reachable: no ({type(exc).__name__})")
        return 1
    print(f"GET /health: HTTP {health_status}, ok={health.get('ok')}")

    missing_status, _ = request_json(base_url, "/v1/telegram/status")
    invalid_status, _ = request_json(
        base_url, "/v1/telegram/status", "diagnostic-invalid-key"
    )
    print(f"GET Telegram status without key: HTTP {missing_status}")
    print(f"GET Telegram status with invalid key: HTTP {invalid_status}")

    if not secret:
        print("Authenticated checks skipped: BROKER_SIDECAR_SECRET is empty.")
        return 1

    status_code, status = request_json(base_url, "/v1/telegram/status", secret)
    print(f"GET /v1/telegram/status with configured key: HTTP {status_code}")
    if status_code != 200:
        detail = status.get("detail")
        if isinstance(detail, str):
            print(f"  detail: {detail}")
        return 1

    safe_status = {
        key: status.get(key)
        for key in (
            "status",
            "connected",
            "authorized",
            "transportConnected",
            "configured",
            "hasSessionString",
            "groupCount",
        )
    }
    print(f"  state: {json.dumps(safe_status, sort_keys=True)}")

    if status.get("authorized") is not True:
        print("GET /v1/telegram/groups: not called; Telegram is not authorized.")
        return 1

    groups_code, groups = request_json(base_url, "/v1/telegram/groups", secret)
    print(f"GET /v1/telegram/groups with configured key: HTTP {groups_code}")
    if groups_code != 200:
        detail = groups.get("detail")
        if isinstance(detail, (str, dict)):
            print(f"  detail: {json.dumps(detail, sort_keys=True)}")
        return 1

    entries = groups.get("groups", [])
    type_counts = Counter(
        entry.get("type", "unknown")
        for entry in entries
        if isinstance(entry, dict)
    )
    print(
        "  result: "
        + json.dumps(
            {
                "ok": groups.get("ok"),
                "count": groups.get("count"),
                "types": dict(type_counts),
                "namesAndIdsWithheld": True,
            },
            sort_keys=True,
        )
    )
    refreshed_code, refreshed = request_json(
        base_url, "/v1/telegram/status", secret
    )
    if refreshed_code == 200:
        print(
            "  refreshed status: "
            + json.dumps(
                {
                    "authorized": refreshed.get("authorized"),
                    "groupCount": refreshed.get("groupCount"),
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Project and Telegram Login Report

**Prepared:** 2026-11-25 (updated after implementing QR login)  
**Workspace:** `BINARY_TRADING_BOTS`  
**Purpose:** Summarize the project, its configuration, the Telegram login flow, and the unresolved code-delivery problem.

## Executive summary

This workspace contains two related but distinct systems:

1. A standalone Python connection/health-monitoring package for Quotex and IQ Option, launched by `run_both.py`.
2. A FastAPI sidecar that wraps community broker clients and also has a separate Telethon integration for connecting one Telegram account and listing its groups.

The project does **not** implement signal reading, message monitoring, or trade execution. The standalone manager has no real broker adapter configured, so its normal broker status is `UNSUPPORTED`; its `--simulate` mode is fake and does not contact a broker.

The Telegram verification code problem is not caused by an empty session string. The session is created only after a successful login. The user's run of the original `sidecar/tools/telegram_login.py` received a Telegram `SentCode` response with `SentCodeTypeApp`, a `phone_code_hash`, and an active connection. This shows that Telegram accepted the code request and selected in-app delivery; it does **not** prove that the user saw the code. No SMS/call fallback was indicated (`Next type: None`). The user reports waiting two hours, confirms the phone number is correct, and provided a screenshot showing an active Telegram Desktop session.

The helper has now been updated to use Telegram QR login instead of requesting a verification code. It renders a temporary QR locally, refreshes expired QR tokens, handles 2FA without echoing the password, and saves the authorized session to `sidecar/.env` without printing it. The user must scan and approve it from their own phone; no live QR authorization has been performed in this workspace, so successful authentication remains unverified.

## What the project does

### Standalone broker monitor

`run_both.py` starts independent IQ Option and Quotex connector lifecycles. For each connector it can load a configured adapter, verify the session and DEMO/PRACTICE account mode, monitor health and latency, detect dropped connections, retry with exponential backoff, log status, and shut down gracefully. The dashboard reports states such as `CONNECTING`, `CONNECTED`, `DEGRADED`, `RECONNECTING`, `UNSUPPORTED`, and `ERROR`.

The default adapter is deliberately an unsupported placeholder. `--simulate` uses in-memory fakes and is clearly labelled as simulated. The standalone manager does not use the FastAPI sidecar.

### FastAPI broker sidecar

`sidecar/broker_sidecar/main.py` exposes an authenticated localhost API for broker connection, OTP completion, status, verification, disconnection, and candle retrieval. Broker sessions are held in memory and isolated by broker and application user. REAL/LIVE accounts are rejected; the broker wrappers pin account mode to PRACTICE/DEMO.

The sidecar wraps `pyquotex` and `iqoptionapi`, which the project documents as unofficial community clients. This has compatibility and broker-terms risks. The API does not expose a trade-execution endpoint.

### Telegram integration

The sidecar's Telegram manager uses Telethon to authorize one Telegram user and fetch that account's groups, supergroups, and channels. It does not listen for messages, parse signals, or trade. The sidecar exposes Telegram status, connect, disconnect, and groups endpoints.

`sidecar/tools/telegram_login.py` is a separate, one-time local login helper. It now renders a QR login token in the terminal, waits for approval, handles an optional Telegram 2FA password, and stores the resulting `StringSession` in `sidecar/.env`.

## Configuration observed

Secret values are intentionally withheld from this report. These are presence/state checks, not a validation that credentials are correct or accepted by Telegram.

### Root `.env`

- `API_ID`, `API_HASH`, and `PHONE_NUMBER`: present (values withheld).
- No `TELEGRAM_SESSION_STRING` entry in this file.
- IQ Option and Quotex are enabled and set to DEMO.
- Both `IQ_OPTION_ADAPTER` and `QUOTEX_ADAPTER` are empty. Therefore, the standalone `run_both.py` has no real adapter configured and will report the brokers as `UNSUPPORTED` unless simulated.
- Broker email and password entries are empty.
- Root monitoring configuration is present: log level `INFO`, dashboard refresh `1` second; per-broker connection timeout `20` seconds, health interval `15` seconds, health timeout `10` seconds, degraded-latency threshold `1500` ms, two failed checks before disconnect, five reconnect attempts, and backoff from `2` to `60` seconds.

### `sidecar/.env`

- `BROKER_SIDECAR_SECRET`: set (value withheld).
- Sidecar host `127.0.0.1`, port `8790`.
- Sidecar connection/health timeouts: `45`/`15` seconds.
- `TELEGRAM_SESSION_STRING`: empty.
- `TELEGRAM_AUTO_START`: `true`.
- `ALLOW_INTERACTIVE_LOGIN`: `false`.
- Telegram API credentials and phone are not overridden in this file; the sidecar and login helper load them from the parent `.env`.

With auto-start enabled and no session string, the sidecar can attempt to start Telegram but cannot authorize the account; it should report an auth-required state. `ALLOW_INTERACTIVE_LOGIN=false` affects the sidecar's interactive login behavior; it does not disable the separate `telegram_login.py` helper.

The inspected environment has one active set of `API_ID`/`API_HASH`/`PHONE_NUMBER` keys. The Telegram service is implemented as one global manager with one session string, so it supports one account at a time—not two accounts simultaneously. API app credentials do not select the Telegram user; the configured phone number does.

### Other configuration files

- `sidecar/.env.example` documents the sidecar secret, localhost binding, Telegram variables, and session-string setup.
- The root `README.md` refers to a root `.env.example`, but no root `.env.example` was present in the workspace inventory.
- `.gitignore` excludes `.env` files, Python caches, pytest cache, virtual environments, and generated log files.

## Telegram login: findings and current problem

### What the helper does

The helper loads `sidecar/.env` first and the root `.env` second, then creates a fresh Telethon `StringSession` and starts QR login using the configured API credentials. It renders the QR only in the terminal, refreshes it when the token expires, and prompts securely for a 2FA password if required. After approval, it writes the new session string directly to `sidecar/.env` and does not print it. It refuses to overwrite a non-empty existing session string.

### Diagnostics supplied by the user

- Delivery type: `SentCodeTypeApp`.
- Next delivery type: `None`.
- Timeout: `None`.
- `phone_code_hash`: present.
- Client connected: `True`.
- Telethon: `1.45.0`.
- Python: `3.14.5`.
- The helper remained at its input prompt because it had not received a code to enter.

**Interpretation:** Telegram returned a valid send-code response and selected in-app delivery. The diagnostic does not establish that the code was displayed on a device. It reports no alternate delivery type, so the helper has no indicated SMS/call route to switch to. The empty session string is expected before first authorization and is not the reason the helper failed to receive a code.

### Troubleshooting history

1. The workspace and original Telegram code path were inspected. The parent `.env` contains the required API and phone keys; the sidecar session string is empty.
2. The user ran the original `python tools\\telegram_login.py` from the sidecar and waited about two hours without receiving a code.
3. The user confirmed the configured phone number is correct and supplied a screenshot showing Telegram Desktop logged in. The phone number and other account identifiers in the screenshot are not reproduced here.
4. The response indicated in-app code delivery, but the user still had no code.
5. The helper was replaced with QR authorization. Its code paths are tested with fakes, but it has not yet been run against the user's live Telegram account.

### Current conclusion and limitations

The original in-app code delivery problem remains unresolved, but the new QR flow avoids relying on that code request. The QR flow still requires the user to approve the login from their own Telegram app. This workspace cannot approve that login on the user's behalf. Until the user runs the new helper and scans the QR, no authorized session string exists.

The currently configured sidecar accepts only one Telegram `StringSession`. Connecting a second Telegram account would require additional implementation for separate per-account credentials/sessions; adding a second API app ID alone does not make this one-account manager support two accounts.

`sidecar/session.json` also exists, but the Python source has no reference to that filename. It does not populate `TELEGRAM_SESSION_STRING`. Its account-specific contents are deliberately not reproduced because the file may contain private session data.

## File inventory and responsibilities

### Workspace root

- `README.md` — standalone broker monitor scope, setup, environment options, states, safety limits, test instructions, and unsupported-adapter behavior.
- `WEB_INTEGRATION.md` — describes the larger Next.js integration, sidecar API, broker data model, Telegram provider endpoints, and documented limitations. The Next.js application itself is not present in this workspace.
- `run_both.py` — standalone manager entry point, CLI, dashboard loop, simulation mode, and graceful shutdown.
- `requirements.txt` — no mandatory dependencies for the standalone package; pytest is optional.
- `__init__.py` — package identity/version and statement of the standalone package scope.
- `.env` — local runtime configuration and credentials; values withheld.
- `.gitignore` — excludes secrets, caches, virtual environments, and logs.

### `common/`

- `config.py` — parses `.env`, merges process environment, validates broker/manager settings, masks secret values.
- `models.py` — connection states, status/health records, and broker error types.
- `adapters.py` — adapter contract plus unsupported and simulated adapters; loads configured adapter classes.
- `base_connector.py` — connect/verify/monitor/reconnect lifecycle and DEMO-only enforcement.
- `connection_manager.py` — starts/stops independent connectors, collects status, and renders dashboard output.
- `logger.py` — rotating per-component logs and credential redaction.
- `__init__.py` — package marker.

### `iq_option/` and `quotex/`

- Each package has `config.py` for its prefixed environment settings, `connector.py` for broker identity and health-check wiring, `health_check.py` for shared session/mode verification, and `__init__.py` as a package marker.
- These connectors do not themselves implement the broker protocol; the standalone package needs a configured adapter.

### `sidecar/`

- `.env` — local sidecar settings and session-string slot; secret values withheld.
- `.env.example` — sidecar setup template.
- `requirements.txt` — FastAPI, Uvicorn, Telethon, qrcode, test tools, and the documented community broker clients.
- `pytest.ini` — pytest configuration.
- `README.md` — sidecar installation, API, Telegram login steps, and security notes.
- `session.json` — existing account-specific JSON file; contents withheld and not referenced by the workspace's Python source.

### `sidecar/broker_sidecar/`

- `main.py` — FastAPI app, environment loading, authentication, broker/Telegram routes, lifecycle, and localhost server startup.
- `telegram_service.py` — single Telethon client, session authorization, status, group/dialog retrieval, flood-wait handling, and optional startup.
- `quotex_client.py` — pyquotex connection, verification, account refresh, disconnection, and candle retrieval.
- `iqoption_client.py` — threaded iqoptionapi connection, 2FA, verification, account refresh, disconnection, and candle retrieval.
- `sessions.py` — in-memory per-user broker session registry and sanitized metadata.
- `errors.py` — typed broker/auth/OTP/timeout/unavailable errors.
- `__init__.py` — package marker.

### `sidecar/tools/`

- `telegram_login.py` — one-time local QR-based Telethon login and session-string persistence.

### Tests and generated files

- Root `tests/test_common.py` — environment parsing, safe defaults, DEMO rejection, and secret redaction.
- Root `tests/test_iq_option.py` and `tests/test_quotex.py` — connector lifecycle and isolation tests using fakes.
- Root `tests/test_run_both.py` — manager, dashboard, simulation, failure isolation, and shutdown tests.
- Root `tests/_bootstrap.py` and `tests/_support.py` — import setup and fake/test helpers.
- `sidecar/tests/test_sidecar.py` — mocked API, broker connection, OTP, status, candle, and session-isolation tests.
- `sidecar/tests/test_telegram.py` — mocked Telegram authorization, status, group retrieval, disconnection, and flood-wait tests.
- `sidecar/tests/test_telegram_login.py` — mocked QR expiry/refresh, two-step password, and safe session-file update tests.
- `logs/iq_option.log`, `logs/quotex.log`, and `logs/manager.log` — generated standalone monitor logs; their contents were not included in this report.
- `logs/.gitkeep` — keeps the log directory present.
- Python `__pycache__` and sidecar `.pytest_cache` files are generated caches, not application source.

## Verification performed

- Root suite: `python -m unittest discover -s tests -v` — **49 tests passed**.
- Sidecar suite after QR-login changes: `python -m pytest tests -q` — **25 tests passed**, with one Starlette/httpx deprecation warning.
- A local dummy QR URL rendered successfully in the terminal. No Telegram connection or live QR authorization was attempted.
- These are mocked/simulated tests. They do not request Telegram codes, contact a real Telegram account, connect to real brokers, or test real code delivery.

## Security notes

- Do not share or commit `.env` values, API hashes, phone numbers, passwords, broker secrets, verification codes, or Telegram session strings.
- The screenshot supplied during troubleshooting showed personal account details; they are not repeated in this report.
- The Telegram session string grants account access. Store it privately in `sidecar/.env` only after a successful local login.

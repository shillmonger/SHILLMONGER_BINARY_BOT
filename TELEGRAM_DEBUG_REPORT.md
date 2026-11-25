# Telegram Sidecar Authentication & Connectivity Debug Report

**Date:** 2026-11-25  
**Project:** `BINARY_TRADING_BOTS`  
**Scope:** Sidecar API authentication, Telethon authorization, and real group retrieval. No message monitoring or trading was added.

## Executive result

- **PASS — API authentication:** the configured sidecar key is loaded and accepted when supplied as `X-Broker-Key`. Missing and invalid keys are rejected.
- **PASS — real Telegram authorization:** sidecar startup connected to Telegram, and the authenticated status endpoint independently verified both transport and authorization as true.
- **PASS — real group retrieval:** the authenticated endpoint returned 13 real dialogs: 8 channels, 3 supergroups, and 2 groups. Names and IDs are intentionally not included here.
- **PASS — automated tests:** 49 root tests and 34 sidecar tests passed. Sidecar tests use mocks for Telegram and broker behavior.
- **NOT TESTED — POST `/v1/telegram/connect`:** not needed; the startup auto-connect was already authorized. The route is covered by a mocked route-dispatch test. No disconnect was issued.

## 1. Original problem

The reported command was an unauthenticated request:

```powershell
Invoke-RestMethod http://127.0.0.1:8790/v1/telegram/status
```

FastAPI returned `401 {"detail":"invalid broker key"}`. This is expected without the required authentication header. When the configured key was supplied, the status request then returned `422` rather than reaching the Telegram handler.

## 2. Root cause

Two independent causes were confirmed:

1. **401:** the request omitted `X-Broker-Key`. The sidecar correctly fails closed and does not allow `/v1/*` requests without the configured shared key.
2. **422 after adding the key:** the generic route `GET /v1/{broker}/status` was registered before `GET /v1/telegram/status`. Starlette matched the static Telegram URL as `broker="telegram"` and then required a `userId` query parameter. The same registration-order collision affected the Telegram `POST /connect` and `POST /disconnect` URLs. This was an actual route-order bug, not a bad Telegram key.

The configured sidecar key was present, and the safe live diagnostic successfully authenticated with the value read locally from `sidecar/.env`. No secret rotation or authentication bypass was needed.

## 3. Configuration and authentication findings

- **Header:** exact expected header is `X-Broker-Key`. FastAPI maps the `x_broker_key` dependency parameter to this header.
- **Secret lookup:** `main.py` reads `BROKER_SIDECAR_SECRET` from the process environment.
- **dotenv paths/order:** the sidecar loads `sidecar/.env` first and the parent `BINARY_TRADING_BOTS/.env` second. `python-dotenv` uses its default `override=False`, so existing process environment variables win, and a value in `sidecar/.env` wins over a duplicate in the parent file. The API secret is in the sidecar file; Telegram API credentials and phone are read from the parent file, with sidecar values able to override them.
- **Root configuration loader:** `common/config.py` parses the standalone broker monitor's root `.env` and merges process variables. It is not the sidecar's secret loader.
- **Telegram login helper:** reads the same sidecar-then-parent dotenv locations.
- **Telegram routes:** every Telegram route uses the same `require_key` dependency as broker routes. Authentication remains enabled.
- **Session:** the live status reported `hasSessionString: true`; the sidecar loads the `TELEGRAM_SESSION_STRING` from its environment into Telethon's `StringSession`. Its value is never printed in this report or diagnostic.
- **Auto-start:** `TELEGRAM_AUTO_START=true` was configured. The sidecar startup log showed a successful Telethon connection and auto-start. The manager checks `is_user_authorized()` before reporting connected; the live status endpoint now refreshes that authorization check.
- **Authorization versus transport:** status explicitly distinguishes `transportConnected` from `authorized`. A connected but unauthorized session reports `auth_required`, `authorized: false`, and a clear last-error message.
- **Identity/privacy:** status masks the phone number and no longer writes account identifiers to the sidecar log. Group responses contain the dialog metadata needed by the integration, not the Telegram account's phone/profile.
- **Groups:** `get_dialogs()` filters Telegram `Chat` and `Channel` entities into group, supergroup, and channel types. Flood waits are returned as HTTP 429 rather than being swallowed by the generic Telegram error handler.

## 4. Files inspected

- `sidecar/broker_sidecar/main.py`
- `sidecar/broker_sidecar/telegram_service.py`
- `sidecar/tools/telegram_login.py`
- `sidecar/.env.example`
- `sidecar/README.md`
- `sidecar/tests/test_telegram.py`
- `sidecar/tests/test_sidecar.py`
- `sidecar/broker_sidecar/sessions.py`
- `sidecar/requirements.txt`
- `common/config.py`
- Root and sidecar `.env` files were checked for key presence/state only; secret values were not printed.

## 5. Files modified and fixes

- `sidecar/broker_sidecar/main.py`
  - Registered fixed Telegram routes before generic `/v1/{broker}/...` routes.
  - Compares supplied/configured keys with `secrets.compare_digest`.
  - Keeps missing-secret behavior fail-closed (503); absent/wrong request keys remain 401.
  - Status endpoint calls an async live authorization/transport refresh.
  - Handles Telegram flood waits before the parent Telegram error class.
- `sidecar/broker_sidecar/telegram_service.py`
  - Adds a live `get_status()` check of the Telethon transport and `is_user_authorized()`.
  - Reports unauthorized session separately from a connected transport.
  - Gives malformed session strings an auth/configuration-specific diagnostic without echoing the value.
  - Cleans up candidate clients after failed connects and avoids duplicate clients on auto-start/reconnect.
  - Rechecks authorization before reading dialogs.
  - Masks phone numbers and avoids logging Telegram account identifiers.
- `sidecar/tools/diagnose_telegram_api.py` (new)
  - Loads the same dotenv files/order as the sidecar.
  - Tests health, missing-key, invalid-key, configured-key status, and groups.
  - Prints no key/session value, phone, account identity, group name, or group ID.
- `sidecar/tests/test_sidecar.py`
  - Adds correct/missing/invalid key checks, fail-closed missing-config test, Telegram route precedence coverage, and 429 flood-wait mapping test.
  - Prevents tests from auto-starting a real Telegram connection.
- `sidecar/tests/test_telegram.py`
  - Adds coverage for invalid sessions, failed-client cleanup, unauthorized status, duplicate auto-start avoidance, refreshed authorization, and group/channel type filtering.
- `sidecar/README.md`
  - Documents the required header, local diagnostic command, live status behavior, and masked phone output.

## 6. Automated tests

### Root suite

**PASS —** `python -m unittest discover -s tests -v`  
Result: **49 passed**.

### Sidecar suite

**PASS — MOCKED TEST —** from `sidecar`:

```powershell
python -m pytest tests -q
```

Result: **34 passed**, with one existing Starlette/httpx deprecation warning. Telegram and broker behavior in this suite is mocked; these results alone are not evidence of live Telegram connectivity.

## 7. Real local API and Telegram results

The sidecar was started on `127.0.0.1:8790`. The existing session was not deleted or overwritten. Since startup auto-connect succeeded, no connect or disconnect request was needed.

**PASS — live checks, not mocked:**

| Request | Result |
|---|---:|
| `GET /health` without key | HTTP 200 |
| `GET /v1/telegram/status` without key | HTTP 401, expected |
| `GET /v1/telegram/status` with invalid key | HTTP 401, expected |
| `GET /v1/telegram/status` with configured `X-Broker-Key` | HTTP 200 |
| `GET /v1/telegram/groups` with configured `X-Broker-Key` | HTTP 200 |
| `POST /v1/telegram/connect` | NOT TESTED; startup status was already authorized |

Safe status fields observed from the actual API:

```json
{
  "status": "connected",
  "connected": true,
  "authorized": true,
  "transportConnected": true,
  "configured": true,
  "hasSessionString": true
}
```

Actual group result, with names and IDs withheld:

```json
{
  "ok": true,
  "count": 13,
  "types": {
    "channel": 8,
    "supergroup": 3,
    "group": 2
  }
}
```

The initial status response showed `groupCount: 0` because groups had not yet been fetched in that process. The groups endpoint then retrieved the 13 dialogs; a refreshed status reported `groupCount: 13`.

## 8. Safe local diagnostic

From PowerShell:

```powershell
cd C:\Users\hp\Desktop\BINARY_TRADING_BOTS\sidecar
python -m broker_sidecar.main
```

In another PowerShell window:

```powershell
cd C:\Users\hp\Desktop\BINARY_TRADING_BOTS\sidecar
python tools\diagnose_telegram_api.py
```

The diagnostic reads the key locally, sends it only to `127.0.0.1`, and does not print it. The API's required header for manual requests is:

```powershell
-Headers @{ "X-Broker-Key" = $secret }
```

Do not paste or print the secret when setting `$secret`; use the diagnostic script instead.

## 9. Remaining issues

- **PASS — current live objective:** the actual Telegram account is authorized, and actual groups/channels were retrieved.
- **NOT TESTED — manual POST connect:** intentionally skipped because auto-start was already connected and authorized; this avoids disturbing a working connection. Route dispatch is covered by mocked tests.
- No signal/message monitoring or trade execution was implemented, consistent with the requested scope.
- The standalone broker monitor still has empty adapter settings and uses its unsupported placeholders unless separately configured. Its tests are simulated and are unrelated to the successful live Telegram checks.

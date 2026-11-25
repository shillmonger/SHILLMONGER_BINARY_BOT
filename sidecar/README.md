# Broker Sidecar

Internal FastAPI service that wraps the community broker clients
(`pyquotex` for Quotex, `iqoptionapi` for IQ Option) and exposes an
authenticated localhost JSON API consumed by the Next.js app's broker
adapters (`lib/binary-brokers/adapters/sidecar.ts`).

**DEMO/PRACTICE accounts only. No trade execution.**

## Requirements

- Python ≥ 3.12 (developed on 3.14)
- Windows/Linux/macOS — the service binds to localhost only

## Install

```powershell
cd BINARY_TRADING_BOTS\sidecar
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows (use `source .venv/bin/activate` elsewhere)
pip install -r requirements.txt
copy .env.example .env              # then edit BROKER_SIDECAR_SECRET
```

> `iqoptionapi` must come from GitHub (see `requirements.txt`) — the PyPI
> 0.5 release is the old broken low-level wrapper.

## Run

```powershell
python -m broker_sidecar.main
```

Health check: `GET http://127.0.0.1:8790/health` (no auth) →
`{"ok": true, "brokers": {"quotex": {"available": true, ...}, ...}}`.

## API

All `/v1/*` endpoints require header `X-Broker-Key: <BROKER_SIDECAR_SECRET>`.
For example, an unauthenticated `Invoke-RestMethod` request returns HTTP 401.
The safe local auth/Telegram diagnostic reads the configured secret without
printing it and tests missing, invalid, and configured-key requests:

```powershell
python tools\diagnose_telegram_api.py
```

| Method | Path                      | Body / Query                                   |
|--------|---------------------------|------------------------------------------------|
| POST   | `/v1/{broker}/connect`    | `{userId,email,password,accountMode,otp?}`     |
| POST   | `/v1/{broker}/otp`        | `{userId,code}`                                |
| POST   | `/v1/{broker}/disconnect` | `{userId}`                                     |
| GET    | `/v1/{broker}/status`     | `?userId=`                                     |
| POST   | `/v1/{broker}/verify`     | `{userId}`                                     |
| GET    | `/v1/{broker}/candles`    | `?userId=&asset=&period=&count=`               |

`{broker}` ∈ `quotex` | `iqoption`. `accountMode` accepts `DEMO`/`PRACTICE`
only — `REAL`/`LIVE` → 400. Sessions are in-memory, keyed `(broker, userId)`.

## Tests

```powershell
python -m pytest tests -q
```

Broker clients are mocked — no network, no real accounts needed.

## Telegram service

The same sidecar also owns an independent **Telethon** client for fetching
the account's Telegram groups (used by `/AdminDashboard/binary-provider`).

Env (see `.env.example`): `API_ID`, `API_HASH`, `PHONE_NUMBER` (read from the
parent `BINARY_TRADING_BOTS/.env` too), `TELEGRAM_SESSION_STRING`,
`TELEGRAM_AUTO_START`, `ALLOW_INTERACTIVE_LOGIN`.

One-time QR login — authorizes a NEW session (never reuse
SHILLMONGER_BOT's). Scan the terminal QR from Telegram on your phone using
**Settings → Devices → Link Desktop Device**. The QR expires and is refreshed
automatically. If two-step verification is enabled, the helper prompts for
the password without echoing it. On success, the session is written directly
to `sidecar/.env`; it is not printed:

```powershell
python tools\telegram_login.py
```

Treat the QR as a temporary login credential: scan it only from your own
Telegram account and do not share or screenshot it. The generated session
string grants account access; keep `sidecar/.env` private.

Endpoints (same `X-Broker-Key` auth): `GET /v1/telegram/status`,
`POST /v1/telegram/connect`, `POST /v1/telegram/disconnect`,
`GET /v1/telegram/groups`. FloodWait is surfaced as HTTP 429 with
`waitSeconds`. The status endpoint rechecks Telethon transport and account
authorization; it reports both states separately and masks the phone number.
No message listening or signal parsing — groups only.

## Security notes

- Bind localhost only; the secret fails closed (503) when unset.
- Credentials transit once per connect, live in memory for the session,
  and are never logged or returned.
- This wraps unofficial broker protocols — account-ban/ToS risk applies.
  See `../WEB_INTEGRATION.md` for the full picture.

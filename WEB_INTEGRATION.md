# Web Integration — Quotex & IQ Option broker connections

This document describes how the Next.js application (`app/`, `models/`, `lib/`)
integrates the independent Quotex and IQ Option broker connections built on top
of the `BINARY_TRADING_BOTS` connection lifecycle.

> **Stage: connection management only.** No trade execution, no signals, no
> Telegram. DEMO account mode only — REAL is rejected by the API.

## Architecture

```
Browser ──> /UserDashboard/quotex-trading|iqoption-trading   (React pages)
        ──> /api/user/{quotex|iqoption}/{connect,disconnect,status,test-connection}
        ──> lib/binary-brokers/service.ts                    (shared lifecycle)
        ──> lib/binary-brokers/registry.ts                   (adapter loader)
        ──> lib/binary-brokers/adapters/sidecar.ts           (HTTP client)
        ──> BINARY_TRADING_BOTS/sidecar  (FastAPI, 127.0.0.1:8790)
              ├─ pyquotex      -> Quotex   (QUOTEX_BROKER)
              └─ iqoptionapi   -> IQ Option (IQOPTIONS_BROKER)
        ──> models/QuotexBroker.ts      -> QUOTEX_BROKER     (MongoDB)
        ──> models/IqOptionBroker.ts    -> IQOPTIONS_BROKER  (MongoDB)
```

The two brokers are fully independent: separate collections, separate API
routes, separate pages, separate adapter env vars, separate connection state.
One broker failing or being disconnected never affects the other.

## MongoDB collections

| Collection        | Model            | broker field |
|-------------------|------------------|--------------|
| `QUOTEX_BROKER`   | `QuotexBroker`   | `QUOTEX`     |
| `IQOPTIONS_BROKER`| `IqOptionBroker` | `IQOPTION`   |

Document shape (per user, unique on `{userId, broker}`):

```json
{
  "userId": "ObjectId",
  "broker": "QUOTEX",
  "accountMode": "DEMO",
  "connectionStatus": "disconnected | connecting | connected | degraded | reconnecting | unsupported | error",
  "credentials": { "encryptedData": "<AES-256-GCM>", "encryptionVersion": 1 },
  "accountInfo": { "accountId": null, "accountCurrency": null },
  "connectionMetadata": {
    "lastConnectedAt": null,
    "lastHealthCheck": null,
    "lastError": null,
    "latencyMs": null
  },
  "createdAt": "Date",
  "updatedAt": "Date"
}
```

Rules enforced in `lib/binary-brokers/service.ts`:

- Credentials are encrypted with `lib/encryption.ts` (AES-256-GCM, key from
  `ENCRYPTION_KEY` env) **before** storage and are never returned by any API.
- `CONNECTED` is persisted only after the adapter's `connect()` **and**
  `verify()` both succeed — a connection attempt alone never marks connected.
- A stored `connected` flag is re-verified on every status read; without an
  adapter it is downgraded to `degraded` rather than trusted.
- `accountInfo` keys matching token/secret/password/session/cookie patterns
  are stripped before persistence.
- All queries are scoped by `userId` — users can only see their own records.

## API endpoints

All endpoints require the `auth-token` cookie (same auth as the rest of the
user dashboard) and only ever act on the caller's own records.

| Method | Path                                        | Purpose                                   |
|--------|---------------------------------------------|-------------------------------------------|
| POST   | `/api/user/quotex/connect`                  | Connect (body: `{credentials, accountMode?}`) |
| POST   | `/api/user/quotex/disconnect`               | Disconnect + clear stored credentials     |
| GET    | `/api/user/quotex/status`                   | Sanitized status + live health check      |
| POST   | `/api/user/quotex/test-connection`          | Verify stored/supplied credentials        |
| POST   | `/api/user/iqoption/connect`                | Same, for IQ Option                       |
| POST   | `/api/user/iqoption/disconnect`             | Same                                       |
| GET    | `/api/user/iqoption/status`                 | Same                                       |
| POST   | `/api/user/iqoption/test-connection`        | Same                                       |

Connect body example:

```json
{ "accountMode": "DEMO", "credentials": { "email": "you@example.com", "password": "...", "otp": "optional" } }
```

`accountMode` accepts only `DEMO` (default). `REAL` returns 400.

## Broker integrations — Python sidecar

Neither Quotex nor IQ Option publishes an official public trading API, so the
integration uses the community clients (unofficial web protocols — see
"Known limitations" below):

| Broker    | Library                          | Source                                                        |
|-----------|----------------------------------|---------------------------------------------------------------|
| Quotex    | `pyquotex` 1.1.0                 | https://github.com/cleitonleonel/pyquotex (actively maintained, requires Python ≥3.12) |
| IQ Option | `iqoptionapi` (stable_api, 7.x)  | https://github.com/iqoptionapi/iqoptionapi — **install from GitHub**; the PyPI `iqoptionapi` 0.5 package is the old broken low-level wrapper. `victalejo/iqoptionapi` is a stale fork — not used. |

Both run inside `BINARY_TRADING_BOTS/sidecar/` — a FastAPI service bound to
`127.0.0.1:8790`, authenticated with a shared `X-Broker-Key` secret. It is
never exposed publicly; only the Next.js server calls it.

### Selected libraries — rationale

- **pyquotex**: most actively maintained Quotex client (async-native,
  `connect()`/`check_connect()`/`get_balance()`/`get_profile()`/candles,
  PRACTICE mode, OTP callback support, clean on Python 3.14).
- **iqoptionapi**: the de-facto standard IQ Option client. Unmaintained since
  2022 but still the only working option; its blocking API is isolated inside
  the async sidecar. Chosen over victalejo's fork (older, less complete).

### Sidecar API (internal, `X-Broker-Key` required)

| Method | Path                     | Purpose                                            |
|--------|--------------------------|----------------------------------------------------|
| GET    | `/health`                | Liveness + per-broker library/session counts       |
| POST   | `/v1/{broker}/connect`   | `{userId,email,password,accountMode,otp?}`         |
| POST   | `/v1/{broker}/otp`       | `{userId,code}` — complete a pending 2FA handshake |
| POST   | `/v1/{broker}/disconnect`| `{userId}`                                         |
| GET    | `/v1/{broker}/status`    | `?userId=`                                         |
| POST   | `/v1/{broker}/verify`    | `{userId}` — re-check auth + refresh account info  |
| GET    | `/v1/{broker}/candles`   | `?userId=&asset=&period=&count=` — market data     |

`{broker}` is `quotex` or `iqoption`. Sessions are keyed `(broker, userId)`
in memory — brokers never share sessions. `REAL`/`LIVE` modes are rejected
with 400; only `DEMO`/`PRACTICE` are accepted.

### TypeScript adapter

`lib/binary-brokers/adapters/sidecar.ts` implements the `BrokerAdapter`
contract over that HTTP API. Enable per broker:

```
QUOTEX_BROKER_ADAPTER=sidecar
IQOPTION_BROKER_ADAPTER=sidecar
BROKER_SIDECAR_URL=http://127.0.0.1:8790
BROKER_SIDECAR_SECRET=<same value as sidecar BROKER_SIDECAR_SECRET>
```

The sidecar returns structured error codes (`auth`, `otp_required`,
`timeout`, `unavailable`) which the adapter maps to `BrokerAuthError`,
`BrokerOtpRequiredError`, `BrokerTimeoutError` → HTTP 401/428/504.

### Running the sidecar

```powershell
cd BINARY_TRADING_BOTS\sidecar
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # set BROKER_SIDECAR_SECRET
python -m broker_sidecar.main
```

Then start the Next.js app as usual. `GET /health` should show both brokers
`available: true` once the libraries import.

### Authentication flow

1. User enters broker email + password on the dashboard page (never stored
   plaintext — encrypted with `ENCRYPTION_KEY` before MongoDB).
2. `connect` forwards credentials to the sidecar once; the sidecar performs
   the real login. Credentials live in the session for its lifetime only.
3. If the broker demands 2FA/OTP, the sidecar returns `requiresOtp` and the
   UI shows a code input; the code completes `/v1/{broker}/otp` — the OTP is
   never bypassed, only relayed.
4. `connected` is only persisted after broker auth **and** `verify()`
   succeed. Demo/practice balance + account info are returned when the
   library exposes them.

### Testing

- `BINARY_TRADING_BOTS/sidecar/tests/test_sidecar.py` — 12 pytest tests,
  broker clients fully mocked (auth, OTP, timeout, disconnect, candles,
  multi-user isolation, broker independence).
- `lib/binary-brokers/adapters/sidecar.test.ts` — 7 jest tests, `fetch`
  mocked (endpoint paths, header auth, error-code mapping).
- `lib/binary-brokers/service.test.ts` — lifecycle/DB/crypto tests, mocked.

### Manual integration test (requires real demo accounts)

1. Start the sidecar, confirm `GET http://127.0.0.1:8790/health` → both
   brokers `available: true`.
2. Start the Next.js app; log in; open `/UserDashboard/quotex-trading`.
3. Enter Quotex **demo** account email/password → Connect. Expected:
   status `connected`, account mode `DEMO`, balance populated.
4. Repeat on `/UserDashboard/iqoption-trading` with an IQ Option **practice**
   account. Disconnect one broker and confirm the other stays connected.
5. Force-fail a login (wrong password) and confirm a clean auth error, no
   stuck `connecting` state.

### Known limitations and risks

- **Unofficial protocols.** Both clients reverse-engineer the brokers' web
  APIs. They can break without notice when the brokers change their apps,
  and using them may violate broker Terms of Service (account-ban risk).
  This is why no trade execution is enabled yet.
- **iqoptionapi is unmaintained** (last release 2022). Expect occasional
  breakage; the sidecar isolates failures per broker/session.
- **OTP/2FA**: supported as a user-completed challenge relay — never
  bypassed. pyquotex delivers the code via callback; iqoptionapi via
  `connect_2fa`.
- **No TLS weakening**: the libraries manage their own TLS; we do not
  disable certificate verification anywhere.
- **Rate limits / captchas**: not bypassed; surfaced as auth/timeout
  errors.
- **Session lifetime**: sidecar sessions are in-memory; a sidecar restart
  drops all broker sessions (the DB records are re-verified on next status
  read and downgrade to `degraded`/`disconnected` accordingly).

## Pages

- `/UserDashboard/quotex-trading`
- `/UserDashboard/iqoption-trading`

Both render `components/UserDashboard/BrokerConnectionPanel.tsx` with
broker-specific branding. Each page shows: connection overview (status,
account mode, account id/currency, last connected, health/latency), an
authentication section (email + password inputs, plus an OTP field shown
when the broker demands 2FA — credentials are encrypted before storage and
never returned), Connect / Disconnect / Test
Connection / Refresh controls, and a client-side connection log. Status is
polled every 15 s. Both appear in the dashboard sidebar as
"Quotex Trading" and "IQ Option Trading".

## Tests

- `lib/binary-brokers/service.test.ts` — input validation, credential
  sanitization/encryption, verified-before-connected, OTP flow, unsupported
  state, broker isolation, disconnect/status/test flows. Fully mocked — no
  real broker, no DB.
- `lib/binary-brokers/adapters/sidecar.test.ts` — sidecar HTTP adapter:
  endpoints, auth header, error-code mapping. `fetch` mocked.
- `app/api/user/quotex/route.auth.test.ts` — auth gate: every endpoint
  rejects unauthenticated callers and delegates with the caller's userId.
- `BINARY_TRADING_BOTS/sidecar/tests/test_sidecar.py` — sidecar endpoints
  with mocked broker clients.

Run: `npx jest` and `python -m pytest tests -q` (from `BINARY_TRADING_BOTS/sidecar`).

## Telegram provider management (phase: connection + groups only)

An independent Telethon client lives inside the same sidecar
(`broker_sidecar/telegram_service.py`) — same library and auth method as
SHILLMONGER_BOT (StringSession + API_ID/API_HASH/PHONE_NUMBER) but with its
OWN session. Endpoints: `GET /v1/telegram/status`, `POST .../connect`,
`POST .../disconnect`, `GET .../groups` (all `X-Broker-Key` auth; FloodWait
→ HTTP 429). Auto-start via `TELEGRAM_AUTO_START=true`; interactive login
only when `ALLOW_INTERACTIVE_LOGIN=true` (terminal use).

Next.js admin API (all require `role === 'admin'`):

| Method | Path                                  | Purpose                     |
|--------|---------------------------------------|-----------------------------|
| GET    | `/api/admin/binary-telegram/status`   | Connection state            |
| POST   | `/api/admin/binary-telegram/connect`  | Connect/reconnect           |
| POST   | `/api/admin/binary-telegram/disconnect` | Disconnect                |
| GET    | `/api/admin/binary-telegram/groups`   | Fetch accessible groups     |
| GET    | `/api/admin/binary-providers`         | List caller's providers     |
| POST   | `/api/admin/binary-providers`         | Save group + broker         |
| PATCH  | `/api/admin/binary-providers/[id]`    | Change broker / status      |
| DELETE | `/api/admin/binary-providers/[id]`    | Remove provider             |

MongoDB: single collection `BINARY_PROVIDER` (`models/BinaryProvider.ts`),
unique index `{userId, telegramGroupId}`, broker strictly `QUOTEX` or
`IQ_OPTION` (mandatory), status `ACTIVE`/`INACTIVE`. Broker assignment is
never guessed — the admin must choose explicitly.

Page: `/AdminDashboard/binary-provider` (sidebar: "Binary Providers").
No signal parsing, message listening, or trade execution in this phase.

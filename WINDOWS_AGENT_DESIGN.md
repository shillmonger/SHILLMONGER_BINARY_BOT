# SHILLMONGER Windows Quotex Agent — Architecture Audit & Design

Status: **PROPOSED — awaiting approval. No implementation yet.**

---

## 1. Architecture audit (current state)

### 1.1 Component map

```
Telegram groups ──Telethon──> broker_sidecar (VPS, FastAPI :8790/:25590)
                                ├─ telegram_service.py  (Telethon client)
                                ├─ message_listener.py  (NewMessage → HMAC POST)
                                ├─ quotex_client.py     (pyquotex wrapper)
                                ├─ iqoption_client.py   (iqoptionapi wrapper)
                                └─ sessions.py          ((broker,userId) session store)
                                        │  POST /api/internal/binary-telegram/ingest
                                        │  HMAC-SHA256 "{ts}.{body}" + X-Signature/X-Timestamp
                                        ▼
                              Next.js on Vercel (shillmongerbot.vercel.app)
                                ├─ /api/internal/*     HMAC worker routes (middleware-exempt)
                                ├─ /api/user/{quotex,iqoption}/*   session-cookie auth
                                ├─ /api/admin/*        requireAdmin() (JWT cookie + role)
                                └─ MongoDB Atlas       QuotexBroker / IqOptionBroker /
                                                       BinaryProvider / BinaryTelegramMessage /
                                                       BinarySignalQueue / BinaryValidatedSignalCandidate
                                        ▲
                                        │  X-Broker-Key: <BROKER_SIDECAR_SECRET>
                                        └── lib/binary-brokers/adapters/sidecar.ts
                                            lib/binary-telegram/client.ts
```

### 1.2 Broker adapter layer (Next.js)

- `lib/binary-brokers/registry.ts` — selects adapter per broker via env:
  `QUOTEX_BROKER_ADAPTER`, `IQOPTION_BROKER_ADAPTER`. Value `sidecar` → built-in
  `adapters/sidecar.ts`. Custom module spec `"<module>[:<export>]"` also supported.
- `adapters/sidecar.ts` — thin authenticated HTTP client to the Python sidecar
  (`BROKER_SIDECAR_URL` + `BROKER_SIDECAR_SECRET`, `X-Broker-Key` header,
  60 s timeout). Implements `BrokerAdapter` { connect, disconnect, verify }.
- `lib/binary-brokers/service.ts` — connection lifecycle: validates body
  (DEMO-only enforced), upserts `QuotexBroker`/`IqOptionBroker` doc, calls
  adapter.connect → adapter.verify → only then persists `connected`.
  Credentials AES-256-GCM encrypted (`lib/encryption.ts`, `ENCRYPTION_KEY`)
  before storage; `sanitizeAccountInfo`/`sanitizeConnection` strip secrets.
- Status model (`models/binary-broker.ts`): `disconnected | connecting |
  connected | degraded | reconnecting | unsupported | error` +
  `connectionMetadata{lastConnectedAt,lastHealthCheck,lastError,latencyMs}`.

### 1.3 Sidecar API surface (FastAPI, `broker_sidecar/main.py`)

- Auth: `X-Broker-Key` header vs `BROKER_SIDECAR_SECRET` (fail-closed 503/401).
- Brokers: `POST /v1/{quotex,iqoption}/connect|otp|disconnect|verify`,
  `GET /v1/{broker}/status|candles`. `accountMode` REAL → 400 (DEMO only).
- Telegram: `GET /v1/telegram/status|groups`, `POST connect|disconnect`,
  listener `GET status`, `POST start|stop|restart`.
- Sessions: in-memory `SessionStore` keyed `(broker, userId)`; password held
  in memory only, never logged/returned.
- Bind: `BROKER_SIDECAR_HOST`/`BROKER_SIDECAR_PORT` → `$PORT` → `127.0.0.1:8790`.

### 1.4 Telegram listener (`message_listener.py`)

- Telethon `NewMessage/MessageEdited/MessageDeleted` on the managed client.
- Normalizes events → `Forwarder` POSTs to `BINARY_INGEST_URL` with the same
  HMAC scheme as `lib/signal-ingest.ts` (`WORKER_HMAC_SECRET`).
- States: STOPPED/STARTING/RUNNING/RECONNECTING/DEGRADED/ERROR; dedup,
  bounded retries w/ backoff, counters.

### 1.5 Environment variables (complete inventory)

**VPS sidecar** (`sidecar/.env` → panel env):
`BROKER_SIDECAR_SECRET`, `BROKER_SIDECAR_HOST`, `BROKER_SIDECAR_PORT`,
`BROKER_CONNECT_TIMEOUT`, `BROKER_HEALTH_TIMEOUT`, `API_ID`, `API_HASH`,
`PHONE_NUMBER`, `TELEGRAM_SESSION_STRING`, `TELEGRAM_AUTO_START`,
`ALLOW_INTERACTIVE_LOGIN`, `BINARY_INGEST_URL`, `WORKER_HMAC_SECRET`,
`TELEGRAM_LISTENER_AUTO_START`, optional `QUOTEX_HOST`, `QUOTEX_PROXY`,
`IQOPTION_HOST`, `LISTENER_FORWARD_TIMEOUT`.

**Vercel** (`.env.local` → Vercel env):
`BROKER_SIDECAR_URL`, `BROKER_SIDECAR_SECRET`, `WORKER_HMAC_SECRET`,
`QUOTEX_BROKER_ADAPTER`, `IQOPTION_BROKER_ADAPTER`, `ENCRYPTION_KEY`,
`QUOTEX_CONNECT_TIMEOUT_MS`/`QUOTEX_HEALTH_TIMEOUT_MS` (+ IQOPTION_* variants),
plus unrelated app secrets (Mongo, NextAuth, Cloudinary, Deriv, etc.).

**Root `BINARY_TRADING_BOTS/.env`**: unused by sidecar (only `run_both.py`
dashboard); effectively retired.

### 1.6 Dependencies between Quotex / IQ Option / Telegram

- **None at runtime.** Three independent subsystems inside one process:
  - `quotex_client.py` and `iqoption_client.py` are sibling modules; sessions
    keyed `(broker, userId)`; no shared client, no shared state.
  - Telegram (`telegram_service` + `message_listener`) is fully independent —
    it never touches broker clients; broker routing happens server-side in
    Next.js via `BINARY_PROVIDER` records.
- **Separation is safe**: the `BROKERS` dict in `main.py` maps broker→module;
  removing/re-routing `quotex` cannot affect `iqoption` or Telegram.

### 1.7 Tests inventory

- Sidecar: `sidecar/tests/` — `test_sidecar.py`, `test_telegram.py`,
  `test_telegram_login.py`, `test_message_listener.py` (broker clients mocked).
- Next.js: `lib/binary-brokers/service.test.ts`, `adapters/sidecar.test.ts`,
  `lib/binary-telegram/ingest.test.ts`, `lib/binary-signals/*test.ts`,
  `app/api/**/route.auth.test.ts` (Jest).
- Python pkg: `BINARY_TRADING_BOTS/tests/` (config/manager/run_both).

### 1.8 The problem being solved

`quotex.com` sits behind Cloudflare bot management. The VPS IP receives
`HTTP 403 cf-mitigated=challenge` on the sign-in page GET (verified via
probe). pyquotex masks this behind a broken `reason_phrase` access.
IQ Option's native protocol has no such checkpoint → unaffected.
Telegram → unaffected.

**Constraint honored**: this design does NOT circumvent Cloudflare — it moves
the client to a network where the broker's own bot management does not
challenge (a normal residential connection), and only exposes
account/session-level functions pyquotex already supports. No trade execution,
no fingerprint spoofing changes, DEMO/PRACTICE only.

---

## 2. Proposed architecture

### 2.1 Core idea — authenticated reverse tunnel (no inbound ports)

The Windows agent **dials out** to the existing VPS sidecar over WSS and
holds a persistent authenticated tunnel. Vercel never talks to Windows;
it keeps calling the VPS sidecar exactly as today. The sidecar routes
Quotex-scoped requests through the tunnel when the agent is connected.

```
                        ┌──────────────────────────── VPS (obsdnhost) ────────────────────────────┐
                        │                                                                         │
 Vercel ──HTTPS/X-Broker-Key──> broker_sidecar (FastAPI)                                          │
                        │   ├─ /v1/iqoption/*     → iqoption_client (LOCAL, unchanged)            │
                        │   ├─ /v1/quotex/*       → agent_tunnel ──WSS──┐                        │
                        │   ├─ /v1/telegram/*     → telegram_service (LOCAL, unchanged)           │
                        │   └─ /v1/agent/tunnel   ←── agent WS connect (HMAC auth)                │
                        └─────────────────────────────────────────────────────────────────────────┘
                                              ▲  outbound WSS only (no inbound ports on Windows)
                                              │
                                    ┌─────────┴──────────┐
                                    │  Windows Agent    │  residential IP → quotex.com passes
                                    │  (new service)    │  Cloudflare normally
                                    │  - tunnel client  │
                                    │  - local health   │  127.0.0.1:8791 only
                                    │  - status model   │
                                    └────────────────────┘
```

### 2.2 Why this satisfies every constraint

| Requirement | How |
|---|---|
| No public port on Windows | Agent is outbound-only; local health binds `127.0.0.1` |
| No unauthenticated tunnel | HMAC challenge-response at WS handshake + per-message HMAC |
| Authenticated outbound to relay | WSS to our own VPS sidecar (the relay) |
| Vercel never hits Windows ports | Vercel → VPS only; VPS ↔ Windows is the tunnel |
| Agent isolated from VPS service | Separate process, separate repo dir, separate secrets |
| IQ Option stays on VPS | `iqoption_client` untouched; only `quotex` routes to agent |
| Telegram independent | Untouched; same process, same listener |
| Health/reconnect/shutdown | Agent heartbeat + state machine + backoff; sidecar stale detection |
| Secrets never exposed | All secrets server-side env vars; nothing `NEXT_PUBLIC_*` |

### 2.3 Trust & message model

- **Tunnel auth**: agent connects to `wss://<vps>/v1/agent/tunnel?agentId=...`;
  first frame must be `AUTH` = HMAC-SHA256(`AGENT_SHARED_SECRET`,
  `{agentId}.{timestamp}.{nonce}`) within a 60 s window. Sidecar verifies,
  issues a session token (random, in-memory, TTL = tunnel lifetime).
  Timestamp+nonce → replay resistant. Wrong/absent → close 4401.
- **Per-message auth**: every subsequent frame HMAC-signed with the session
  token; sidecar drops unsigned/malformed frames.
- **Envelope**: `{v:1, id:<correlation uuid>, type:<verb>, ts:<epoch>, payload:{...}}`.
  `id` echoed in response → request/response correlation.
- **Allowlisted verbs only** (fixed enum, validated by pydantic/JSON schema):
  `PING`, `STATUS`, `HEALTH`, `CONNECT`, `DISCONNECT`, `VERIFY`, `CANDLES`,
  `OTP`, `SHUTDOWN` (agent-initiated graceful stop only via admin route).
  **No shell, no file paths, no eval, no arbitrary URLs.** Broker verbs are
  further restricted to the `quotex` scope and DEMO/PRACTICE accountMode —
  REAL is rejected at both layers (same rule as existing sidecar).
- **Rate limits**: per-connection token bucket (e.g., 30 msg/min sustained,
  burst 10); per-verb quotas; oversized frames rejected (>64 KB).
- **TLS**: WSS via the panel-provided HTTPS endpoint or `wss://` to the VPS
  (if only HTTP available, tunnel still HMAC-authenticated per message;
  recommend putting sidecar behind the panel's TLS proxy or Caddy).

### 2.4 Agent state model (required states)

`STOPPED → STARTING → CONNECTED ⇄ DISCONNECTED → ERROR | BLOCKED`

- CONNECTED = tunnel established AND last heartbeat fresh AND broker probe ok.
- BLOCKED = broker/security challenge detected (e.g., Cloudflare 403) —
  surfaced as a *diagnostic state*, never retried aggressively; no bypass logic.
- Heartbeat every 15 s (agent→sidecar); sidecar marks stale after 45 s;
  dashboard shows OFFLINE after 90 s without heartbeat.

### 2.5 Component: `quotex-agent` (new, Windows)

Location: `BINARY_TRADING_BOTS/agent/` in `SHILLMONGER_BINARY_BOT` repo.

- Python 3.12+, `websockets` (client), `pydantic` (validation), stdlib
  `logging` + `logging.handlers.RotatingFileHandler`, `python-dotenv`.
- Processes: single asyncio process. Local health: `aiohttp`/`fastapi` on
  `127.0.0.1:8791` ONLY (`/health`, `/status`) — never `0.0.0.0`.
- Lifecycle: Windows service via **Task Scheduler** (at-logon, restart on
  failure) or NSSM — documented both; graceful shutdown on `CTRL_CLOSE_EVENT`
  / service stop → close WS with code 1000 → flush logs → exit.
- Broker module (`agent/broker/quotex.py`) is a **stub in phases 2–3**;
  real pyquotex session management is a separately-approved later phase.
- Secrets redaction: log filter scrubs `AGENT_SHARED_SECRET`, tokens, and
  any `password=`/`token=` patterns.

### 2.6 Sidecar additions (VPS, minimal diff)

- `broker_sidecar/agent_tunnel.py` — WS endpoint `/v1/agent/tunnel`,
  session registry (one agent at a time per `agentId`), request router
  agent-bound verbs → tunnel, correlation map, stale detection.
- `main.py` — include router; add `GET /v1/agent/status` (X-Broker-Key) for
  the dashboard; route `quotex` broker verbs to the tunnel when
  `QUOTEX_BACKEND=agent` (default `local` until cutover).
- `quotex_client.py` — **unchanged on VPS** (stays as fallback/local mode).
- New env: `AGENT_TUNNEL_ENABLED`, `AGENT_SHARED_SECRET`,
  `QUOTEX_BACKEND`, `AGENT_STALE_SECONDS`.

### 2.7 Vercel / Next.js additions (phase 4, dashboard only)

- `lib/binary-agent/client.ts` — server-side client to
  `GET {BROKER_SIDECAR_URL}/v1/agent/status` with `X-Broker-Key`
  (same pattern as `lib/binary-telegram/client.ts`).
- `app/api/admin/binary-agent/status/route.ts` — `requireAdmin()` guard.
- Dashboard card on `AdminDashboard/BinaryActivity` (or broker status
  section): online/offline, last heartbeat, health, last error, version,
  state — read-only + a single authorized `restart` verb (maps to tunnel
  `SHUTDOWN`→ respawn via supervisor, NOT arbitrary commands).
- **No DB schema change required** — agent status is ephemeral; optionally
  a small `BinaryAgentStatus` collection later if history is wanted
  (documented as optional, not in first pass).

### 2.8 Threat model

| Threat | Mitigation |
|---|---|
| Unauthorized tunnel connect | HMAC auth + timestamp/nonce window; fail-close; per-agentId single session (new connect evicts old) |
| Replay attacks | Timestamp ±60 s + nonce cache + per-message HMAC + WS is already encrypted |
| Secret leakage | Secrets only in env vars / gitignored `.env`; log redaction filter; nothing `NEXT_PUBLIC_*`; `X-Broker-Key` stays server-side |
| Machine compromise (Windows) | Agent runs unprivileged; allowlisted verbs only; no file/command surface; local health is localhost-only; tunnel token is session-scoped (dies with process) |
| Remote command execution | Fixed verb enum; schema-validated payloads; no shell/file/URL params anywhere in protocol |
| VPS compromise | Agent holds no broker credentials beyond what user supplies per-session; `AGENT_SHARED_SECRET` rotation = update both envs + restart; broker creds remain AES-256-GCM at rest in Mongo |
| DoS / flood | Rate limits, frame size cap, stale detection, Vercel↔sidecar timeout already bounded |
| Rogue agent impersonation | Single-agent registry + optional IP allowlist on `/v1/agent/tunnel` (home IP is dynamic → make optional, default off, document trade-off) |

### 2.9 Configuration variables

**Windows agent `.env` (gitignored):**

| Var | Purpose |
|---|---|
| `AGENT_ID` | Stable agent identifier (e.g. `win-home-01`) |
| `AGENT_VPS_URL` | `wss://bot-host-04.obsdnhost.com:25590` (or https→wss) |
| `AGENT_SHARED_SECRET` | Pre-shared HMAC secret (NEW, distinct from all existing secrets) |
| `AGENT_LOCAL_HEALTH_PORT` | `8791` — binds 127.0.0.1 only |
| `AGENT_HEARTBEAT_SECONDS` | `15` |
| `AGENT_RECONNECT_*` | backoff base/max/jitter |
| `AGENT_VERSION` | reported in STATUS/heartbeat |

**VPS sidecar (additions):**

| Var | Purpose |
|---|---|
| `AGENT_TUNNEL_ENABLED` | `true` once agent deployed |
| `AGENT_SHARED_SECRET` | same value as agent |
| `QUOTEX_BACKEND` | `local` (default) → `agent` at cutover |
| `AGENT_STALE_SECONDS` | `45` |

**Vercel:** none for broker path. Dashboard reuses `BROKER_SIDECAR_URL`/`SECRET`.

### 2.10 Quotex integration scope (permitted functions — documented BEFORE implementation)

pyquotex (`Quotex` class, `stable_api`) exposes and the agent MAY implement:
`connect()`, `check_connect()`, `get_balance()`, `get_profile()`,
`get_candles()`, `close()` — i.e., **session/account/candles only**.
The existing sidecar already exposes exactly this surface (`connect`,
`otp`, `disconnect`, `status`, `verify`, `candles`) and **no trade
execution endpoints exist** — that stays true. Anything beyond this list
requires separate review. Account mode remains DEMO/PRACTICE only;
REAL rejected at agent, sidecar, and Next.js layers (three existing gates).

### 2.11 Implementation phases (matching your staged requirements)

- **Phase A (this approval)**: audit + this document.
- **Phase B — Agent shell**: `BINARY_TRADING_BOTS/agent/` — config
  validation, structured logging w/ redaction, local health endpoint,
  lifecycle, state model, heartbeat stub, retry/backoff, Windows
  install/run docs, unit tests, README. *No broker code, no tunnel yet.*
- **Phase C — Tunnel layer**: sidecar `agent_tunnel.py` + agent tunnel
  client; auth, envelope, correlation IDs, rate limits, stale detection,
  reconnect, structured errors; tests for loss/unauthorized/malformed/
  timeout/recovery.
- **Phase D — Dashboard**: admin status route + UI card; read-only status
  + single authorized restart verb; tests.
- **Phase E (separately gated)**: actual pyquotex session management inside
  the agent behind `QUOTEX_BACKEND=agent` cutover — only after B–D proven.

### 2.12 Git checkpoint (before any code)

Working trees are clean. On approval I will:
`git tag pre-windows-agent` on both repos (`SHILLMONGER_BINARY_BOT` main,
`SHILLMONGER_BOT` master) so every phase is independently revertible.

---

## 3. Open questions for you

1. **VPS TLS**: is `bot-host-04.obsdnhost.com:25590` plain HTTP only? The
   tunnel is HMAC-authenticated per message either way, but WSS is better.
   Does the panel offer SSL for the allocation, or should we put Caddy in
   front later?
2. **Agent host**: will the Windows machine stay on 24/7? (Tailscale-style
   dynamic IP is fine — agent dials out.) Any Windows version constraints?
3. **Scope confirm**: Phase E (real Quotex login on Windows via pyquotex)
   is intentionally deferred — confirm that's the intent, or should broker
   connectivity be included in the first build?

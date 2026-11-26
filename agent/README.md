# SHILLMONGER Windows Agent

Isolated Python service that runs on a residential-network Windows machine.
**Phase B shell**: configuration, lifecycle, state model, heartbeat,
diagnostics and a loopback-only health endpoint. The authenticated tunnel
to the VPS sidecar (Phase C), broker connectivity (Phase E) and the admin
dashboard card (Phase D) are NOT part of this build.

See `../WINDOWS_AGENT_DESIGN.md` for the full architecture, threat model
and rollout plan.

## What it does NOT do

- No inbound internet ports — the only HTTP listener binds `127.0.0.1`.
- No broker logins, no trading, no Telegram access.
- No broker security-challenge probing or bypass attempts.
- No arbitrary command/file surface — there is no remote-control protocol yet.

## Requirements

- Windows 10/11, Python 3.10+ (developed on 3.14)
- Always-on machine recommended

## Install

```powershell
cd BINARY_TRADING_BOTS\agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # then edit AGENT_ID / AGENT_VERSION
```

## Run (manual / foreground)

```powershell
.\.venv\Scripts\python.exe -m agent
# or: scripts\run-agent.ps1
```

Stop with `Ctrl+C` — the service closes the health server and heartbeat
gracefully and exits `STOPPED`.

## Health endpoints (localhost only)

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET http://127.0.0.1:8791/health` | none (loopback only) | liveness |
| `GET http://127.0.0.1:8791/ready` | none | 200 only in CONNECTED |
| `GET http://127.0.0.1:8791/status` | `X-Agent-Token` if `AGENT_LOCAL_TOKEN` set | full state snapshot |
| `GET http://127.0.0.1:8791/diagnostics` | same | local infra checks |

Diagnostics are **local-only**: config, Python version, dependencies,
state-dir writability, loopback bind. They never contact broker endpoints.

## State model

`STOPPED → STARTING → CONNECTED ⇄ DISCONNECTED → ERROR | BLOCKED`

Phase B: CONNECTED = core loops (health server + heartbeat + diagnostics)
healthy. Tunnel and broker states layer on in later phases; `BLOCKED` is
reserved for broker security-challenge detection and is never retried
aggressively.

## Autostart on Windows (Task Scheduler)

```powershell
scripts\install-autostart.ps1     # start at logon, restart on failure
scripts\uninstall-autostart.ps1   # remove
```

Alternative: NSSM (`nssm install SHILLMONGER-Agent "<venv>\python.exe" "-m agent"`)
if you prefer a real Windows service. Both run unprivileged.

## Configuration

All variables are documented in `.env.example`. Key rules enforced at
startup:

- `AGENT_LOCAL_HEALTH_HOST` must be loopback — anything else is refused.
- `AGENT_TUNNEL_ENABLED=true` requires `AGENT_VPS_URL` (http/ws URL) and
  `AGENT_SHARED_SECRET` (≥32 chars, NEW secret — never reuse
  `BROKER_SIDECAR_SECRET`/`WORKER_HMAC_SECRET`).
- Secrets are never logged (redaction filter) and `.env` is gitignored.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

## Layout

```
agent/
  agent/           package (config, state, retry, auth, diagnostics,
                   heartbeat, health_server, service, __main__)
  tests/           pytest suite (no network, no broker calls)
  scripts/         Windows run/autostart helpers
  requirements.txt
  .env.example
  pytest.ini
```

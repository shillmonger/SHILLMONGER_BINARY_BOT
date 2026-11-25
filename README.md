# BINARY_TRADING_BOTS

Standalone **connection and health-monitoring** module for two brokers, **IQ Option** and **Quotex**, started and watched from one entry point, `run_both.py`.

**Stage 1 only.** There is no trade execution, no Telegram integration and no signal processing in this version. Only DEMO/PRACTICE mode is accepted.

---

## 1. What it does

* Starts each broker connector independently (own config, task, state, logger, log file).
* Connects through a **broker adapter**, then **verifies** the session and that the account is DEMO before ever reporting `CONNECTED`.
* Re-checks health on an interval, measures latency (when the adapter can), detects timeouts and drops, and reconnects with exponential back-off.
* Shows a live terminal dashboard and shuts both brokers down cleanly on `CTRL+C`.
* One broker failing, timing out or crashing never stops the other.

### Connection states

`DISCONNECTED` · `CONNECTING` · `CONNECTED` · `DEGRADED` · `RECONNECTING` · `UNSUPPORTED` · `ERROR`

* `CONNECTED` is shown only after the adapter confirms a live session **and** a DEMO account.
* `DEGRADED`: session is up but latency is above the threshold, or a single health check failed.
* `UNSUPPORTED`: no authorized integration is configured (the default, see section 8).
* `ERROR`: configuration rejected, safety stop (non-DEMO account), or reconnect attempts exhausted.

## 2. Folder structure

```
BINARY_TRADING_BOTS/
├── run_both.py              entry point (controller + dashboard)
├── README.md  requirements.txt  .env.example  .gitignore
├── __init__.py              makes the folder an importable package
├── common/
│   ├── models.py            states, status snapshot, error types
│   ├── config.py            env/.env loading, masked secrets
│   ├── logger.py            per-broker rotating logs with secret redaction
│   ├── adapters.py          BrokerAdapter interface, UnsupportedAdapter, SimulatedAdapter
│   ├── base_connector.py    connect -> verify -> monitor -> reconnect lifecycle
│   └── connection_manager.py  starts/stops/queries connectors, renders dashboard
├── iq_option/               connector.py  config.py  health_check.py
├── quotex/                  connector.py  config.py  health_check.py
├── logs/                    iq_option.log  quotex.log  manager.log
└── tests/                   test_iq_option.py  test_quotex.py  test_run_both.py
                             test_common.py  (+ _bootstrap.py, _support.py helpers)
```

Everything lives inside `BINARY_TRADING_BOTS/`. It imports nothing from your application, touches no database and only needs the Python standard library (3.9+).

## 3. Install

Extract the ZIP into your project root so you have `<project>/BINARY_TRADING_BOTS/`.

**Windows (PowerShell or CMD)**

```
cd BINARY_TRADING_BOTS
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

**macOS / Linux**

```
cd BINARY_TRADING_BOTS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` has no mandatory packages. If your project already has a virtual environment you can skip creating a new one.

## 4. Configure

```
copy .env.example .env        (Windows)
cp .env.example .env          (macOS / Linux)
```

Edit `.env`. Real environment variables override `.env`. Never commit `.env` (it is in `.gitignore`).

| Variable (per broker: `IQ_OPTION_*` / `QUOTEX_*`) | Default | Meaning |
|---|---|---|
| `ENABLED` | `true` | Disable one broker without touching the other |
| `ACCOUNT_MODE` | `DEMO` | Only `DEMO`/`PRACTICE`. Anything else puts that broker in `ERROR` and nothing is attempted |
| `EMAIL`, `PASSWORD` | empty | Passed to your adapter only; never logged or shown |
| `ADAPTER` | empty | `package.module:ClassName` of an authorized adapter. Empty = `UNSUPPORTED` |
| `CONNECT_TIMEOUT` | 20 s | Per connection attempt |
| `HEALTH_INTERVAL` / `HEALTH_TIMEOUT` | 15 s / 10 s | Verification cadence and per-check timeout |
| `DEGRADED_LATENCY_MS` | 1500 | Above this the state is `DEGRADED` |
| `FAILURES_BEFORE_DISCONNECT` | 2 | Consecutive failed checks before declaring a disconnect |
| `MAX_RECONNECT_ATTEMPTS` | 5 | `0` = unlimited. After this the broker goes to `ERROR` |
| `RECONNECT_BASE_DELAY` / `RECONNECT_MAX_DELAY` | 2 s / 60 s | Exponential back-off |

Manager-wide: `BROKER_LOG_DIR`, `BROKER_LOG_LEVEL`, `BROKER_DASHBOARD_REFRESH`.

## 5. Run

From the project root **or** from inside the folder (imports are handled for you):

```
python BINARY_TRADING_BOTS\run_both.py        (Windows, from project root)
python run_both.py                          (from inside BINARY_TRADING_BOTS)
```

Options: `--simulate` (fake adapters, see below), `--duration N` (auto-stop), `--refresh N`, `--env-file PATH`, `--no-clear` (append instead of redrawing, useful when piping to a file).

With no adapter configured you will see both brokers as **UNSUPPORTED**. That is the correct, honest result. To preview the dashboard and reconnection behaviour:

```
python BINARY_TRADING_BOTS\run_both.py --simulate
```

`--simulate` uses in-memory fake adapters, prints `*** SIMULATION MODE ***` and labels states `CONNECTED (SIMULATED)`. The fake Quotex link drops every 20 s so you can watch detection and reconnection.

**Shut down:** press `CTRL+C` once. Both brokers are disconnected gracefully and a final dashboard is printed. A second `CTRL+C` aborts immediately.

## 6. Test

Run from the project root (or from inside the folder with `-s tests`):

```
python -m unittest discover -s BINARY_TRADING_BOTS/tests -v
```

pytest works too if installed: `python -m pytest BINARY_TRADING_BOTS/tests`.

**Every test is simulated.** They use `SimulatedAdapter` or small fake adapters, need no credentials and no network, and cannot trade (there is no trading code). There are **no tests against real broker servers** in this version, because without an authorized adapter there is nothing real to connect to. Covered: initialization, UNSUPPORTED reporting, verified-before-CONNECTED, failure and recovery, give-up after max attempts, connection and health timeouts, unexpected exceptions, drop detection + reconnection, live-mode refusal, non-DEMO account rejection, credential redaction, one broker failing independently of the other, graceful shutdown, and `run_both.py` start-up.

## 7. Logs and troubleshooting

Logs are in `BINARY_TRADING_BOTS/logs/` (rotating, 2 MB x 3): `iq_option.log`, `quotex.log`, `manager.log`. Each line has a timestamp, level, broker name and message. Credentials are redacted from messages and tracebacks. Use `BROKER_LOG_LEVEL=DEBUG` for more detail.

```
type logs\iq_option.log                 (Windows)
powershell Get-Content logs\quotex.log -Wait -Tail 20
tail -f logs/quotex.log                 (macOS / Linux)
```

| Symptom | Likely cause / fix |
|---|---|
| `UNSUPPORTED` | No adapter configured. Expected by default; see section 8 |
| `ERROR: Configuration error ...` | Bad value in `.env` (or a non-DEMO `ACCOUNT_MODE`). The message names the variable |
| `ERROR: Adapter could not be created` | `*_ADAPTER` path is wrong or not importable from your project root |
| `ERROR: Account mode ... not permitted` | Adapter reported a non-DEMO account. The session is closed and not retried |
| `ERROR: Giving up after N failed attempts` | Network/credentials problem; read the broker log, fix, restart |
| `ModuleNotFoundError: BINARY_TRADING_BOTS` | Folder was renamed; keep the name `BINARY_TRADING_BOTS` |
| Garbled dashboard | Use `--no-clear` or Windows Terminal / a recent PowerShell |

## 8. Implemented vs simulated vs needs an official integration

**Genuinely implemented (real code, real behaviour)**
* Per-broker lifecycle, state machine, verification-before-CONNECTED, health monitoring, DEGRADED detection, timeouts, exponential-back-off reconnection, safety stop on non-DEMO accounts.
* Isolation between brokers, secret masking, per-broker logs, terminal dashboard, graceful shutdown (incl. Windows).

**Simulated (only when you ask for it)**
* `SimulatedAdapter`, used by the tests and `--simulate`. Never used by default and always labelled.

**Not implemented: the actual broker protocol**
* As far as I am aware, neither IQ Option nor Quotex publishes an official, documented public API for third-party trading automation. Please check the brokers' current documentation or support, as this can change.
* Community libraries exist that reproduce the web apps' login and websocket protocols. They are unofficial, can break without notice and may violate the brokers' terms, so they are **not** included, and this module does not extract browser cookies, bypass CAPTCHA/2FA, or fake a connection.
* So by default both brokers report `UNSUPPORTED`. Nothing is pretending to be connected.

**Plugging in an authorized integration**

If a broker gives you authorized API access, implement the adapter yourself, outside this folder if you like:

```python
# my_project/iq_adapter.py
from BINARY_TRADING_BOTS.common.adapters import BrokerAdapter

class MyIQAdapter(BrokerAdapter):
    async def connect(self): ...            # use self.settings.email.reveal() / .password.reveal()
    async def disconnect(self): ...
    async def is_connected(self): ...       # must verify against the broker, not a cached flag
    async def get_account_mode(self): ...   # "DEMO" or "PRACTICE" for a practice account
    async def ping(self): ...               # optional: latency in ms, or None
```

Then set `IQ_OPTION_ADAPTER=my_project.iq_adapter:MyIQAdapter` in `.env`. The lifecycle, monitoring, reconnection, logging and dashboard work unchanged. Blocking client libraries should be wrapped with `asyncio.to_thread(...)` inside the adapter.

## 9. Next phases (not in this version)

Telegram integration and signal routing, followed by demo trade execution, will build on `ConnectionManager.get_state()` / `get_status()` to check a broker is verified before acting.

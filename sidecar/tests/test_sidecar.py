"""Sidecar tests — all broker clients are mocked. No real broker, no network."""

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("BROKER_SIDECAR_SECRET", "test-secret")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from broker_sidecar import iqoption_client, quotex_client, telegram_service  # noqa: E402
from broker_sidecar.errors import BrokerOtpRequired  # noqa: E402
from broker_sidecar.main import app  # noqa: E402
from broker_sidecar.sessions import store  # noqa: E402

KEY = {"X-Broker-Key": "test-secret"}
BASE = {"quotex": "/v1/quotex", "iqoption": "/v1/iqoption"}


@pytest.fixture(autouse=True)
def clean_sessions(monkeypatch):
    monkeypatch.setenv("TELEGRAM_AUTO_START", "false")
    for k in store.list_keys():
        b, _, u = k.partition(":")
        store.drop(b, u)
    yield
    for k in store.list_keys():
        b, _, u = k.partition(":")
        store.drop(b, u)


@pytest.fixture
def client():
    return TestClient(app)


# ------------------------------------------------------------- fakes


class FakeQuotexProfile:
    id = "qx-user-1"
    currency = "USD"


class FakeQuotex:
    """Async pyquotex stand-in."""

    behavior = {"connect": (True, "ok")}
    instances = []

    def __init__(self, email, password, **kwargs):
        self.email = email
        self.password = password
        self.closed = False
        self.on_otp_callback = kwargs.get("on_otp_callback")
        FakeQuotex.instances.append(self)

    def set_account_mode(self, mode):
        self.mode = mode

    async def connect(self):
        b = FakeQuotex.behavior["connect"]
        if isinstance(b, Exception):
            raise b
        return b

    async def check_connect(self):
        return FakeQuotex.behavior.get("alive", True)

    async def get_balance(self):
        return FakeQuotex.behavior.get("balance", 250.0)

    async def get_profile(self):
        return FakeQuotexProfile()

    async def close(self):
        self.closed = True
        return True

    async def get_candles(self, asset, end_from_time, offset, period):
        return [{"close": 1.1}] * 3


class FakeIQOption:
    """Sync iqoptionapi stand-in (library is sync; sidecar threads it)."""

    behavior = {"connect": (True, None)}
    instances = []

    def __init__(self, email, password, active_account_type="PRACTICE"):
        self.email = email
        self.password = password
        self.account_type = active_account_type
        self.api = self  # _close_client calls client.api.close()
        FakeIQOption.instances.append(self)

    def connect(self):
        b = FakeIQOption.behavior["connect"]
        if isinstance(b, Exception):
            raise b
        return b

    def connect_2fa(self, code):
        self.otp_used = code
        return FakeIQOption.behavior.get("otp_result", (True, None))

    def check_connect(self):
        return FakeIQOption.behavior.get("alive", True)

    def get_balances(self):
        import iqoptionapi.global_value as gv

        return {"msg": [{"id": gv.balance_id, "amount": 75.5, "currency": "USD"}]}

    def get_balance_mode(self):
        return "PRACTICE"

    def get_currency(self):
        return "USD"

    def get_profile_ansyc(self):
        return {"result": {"id": "iq-user-1", "balances": []}}

    def close(self):
        self.closed = True


@pytest.fixture
def fake_quotex(monkeypatch):
    FakeQuotex.instances.clear()
    FakeQuotex.behavior = {"connect": (True, "ok"), "alive": True, "balance": 250.0}
    monkeypatch.setattr(quotex_client, "Quotex", FakeQuotex)
    monkeypatch.setattr(quotex_client, "_HAVE_PYQUOTEX", True)
    return FakeQuotex


@pytest.fixture
def fake_iq(monkeypatch):
    FakeIQOption.instances.clear()
    FakeIQOption.behavior = {"connect": (True, None), "alive": True}
    monkeypatch.setattr(iqoption_client, "IQ_Option", FakeIQOption)
    monkeypatch.setattr(iqoption_client, "_HAVE_IQ", True)
    return FakeIQOption


def connect_body(broker, user="u1", **kw):
    body = {"userId": user, "email": "demo@example.com", "password": "pw", "accountMode": "DEMO"}
    body.update(kw)
    return client_post(BASE[broker] + "/connect", body)


def client_post(path, body):
    from fastapi.testclient import TestClient

    return TestClient(app).post(path, json=body, headers=KEY)


# ------------------------------------------------------------- tests


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_auth_required(client):
    r = client.post("/v1/quotex/connect", json={"userId": "u"})
    assert r.status_code == 401
    r = client.post("/v1/quotex/connect", json={"userId": "u"}, headers={"X-Broker-Key": "bad"})
    assert r.status_code == 401


def test_telegram_routes_require_correct_broker_key(client, monkeypatch):
    monkeypatch.setattr(
        telegram_service.tg,
        "get_status",
        lambda: _telegram_status(),
    )
    missing = client.get("/v1/telegram/status")
    invalid = client.get(
        "/v1/telegram/status", headers={"X-Broker-Key": "intentionally-invalid"}
    )
    authorized = client.get("/v1/telegram/status", headers=KEY)

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json() == {"status": "connected", "authorized": True}


async def _telegram_status():
    return {"status": "connected", "authorized": True}


def test_missing_configured_secret_fails_closed(client, monkeypatch):
    monkeypatch.delenv("BROKER_SIDECAR_SECRET", raising=False)

    response = client.get("/v1/telegram/status", headers=KEY)

    assert response.status_code == 503


def test_telegram_static_routes_precede_broker_routes(client, monkeypatch):
    async def groups():
        return {
            "ok": True,
            "groups": [{"id": "1", "name": "Example", "type": "supergroup"}],
            "count": 1,
        }

    async def connect():
        return {"status": "connected", "authorized": True}

    async def disconnect():
        return {"status": "disconnected", "authorized": False}

    monkeypatch.setattr(telegram_service.tg, "fetch_groups", groups)
    monkeypatch.setattr(telegram_service.tg, "connect", connect)
    monkeypatch.setattr(telegram_service.tg, "disconnect", disconnect)

    group_response = client.get("/v1/telegram/groups", headers=KEY)
    connect_response = client.post("/v1/telegram/connect", headers=KEY)
    disconnect_response = client.post("/v1/telegram/disconnect", headers=KEY)

    assert group_response.status_code == 200
    assert group_response.json()["count"] == 1
    assert connect_response.status_code == 200
    assert connect_response.json()["authorized"] is True
    assert disconnect_response.status_code == 200
    assert disconnect_response.json()["status"] == "disconnected"


def test_telegram_groups_maps_flood_wait_to_429(client, monkeypatch):
    async def flood_wait():
        from broker_sidecar.telegram_service import TelegramFloodWait

        raise TelegramFloodWait(37)

    monkeypatch.setattr(telegram_service.tg, "fetch_groups", flood_wait)

    response = client.get("/v1/telegram/groups", headers=KEY)

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "flood_wait"
    assert response.json()["detail"]["waitSeconds"] == 37


def test_real_mode_rejected(client, fake_quotex):
    r = client_post("/v1/quotex/connect", {
        "userId": "u1", "email": "e@x.com", "password": "p", "accountMode": "REAL",
    })
    assert r.status_code == 400
    assert FakeQuotex.instances == []  # never even constructed a client


def test_quotex_connect_success(client, fake_quotex):
    r = connect_body("quotex")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["connected"] is True
    assert data["account"]["balance"] == 250.0
    assert data["account"]["accountMode"] == "PRACTICE"
    inst = FakeQuotex.instances[0]
    assert inst.mode == "PRACTICE"  # hard-pinned demo


def test_quotex_bad_credentials(client, fake_quotex):
    fake_quotex.behavior["connect"] = (False, "invalid credentials")
    r = connect_body("quotex")
    data = r.json()
    assert data["ok"] is False and data["connected"] is False
    assert data["code"] == "auth"


def test_quotex_otp_flow(client, fake_quotex):
    fake_quotex.behavior["connect"] = BrokerOtpRequired()
    r = connect_body("quotex")
    data = r.json()
    assert data["ok"] is False and data["requiresOtp"] is True

    # User supplies the code -> sidecar retries with it.
    fake_quotex.behavior["connect"] = (True, "ok")
    r2 = client_post("/v1/quotex/otp", {"userId": "u1", "code": "123456"})
    assert r2.status_code == 200
    assert r2.json()["connected"] is True


def test_quotex_status_and_disconnect(client, fake_quotex):
    connect_body("quotex")
    r = client.get("/v1/quotex/status", params={"userId": "u1"}, headers=KEY)
    assert r.json()["connected"] is True
    assert r.json()["account"]["balance"] == 250.0

    r = client_post("/v1/quotex/disconnect", {"userId": "u1"})
    assert r.json()["connected"] is False
    assert FakeQuotex.instances[0].closed is True

    r = client.get("/v1/quotex/status", params={"userId": "u1"}, headers=KEY)
    assert r.json()["connected"] is False


def test_quotex_verify_and_candles(client, fake_quotex):
    connect_body("quotex")
    r = client_post("/v1/quotex/verify", {"userId": "u1"})
    assert r.json()["ok"] is True

    r = client.get(
        "/v1/quotex/candles",
        params={"userId": "u1", "asset": "EURUSD", "period": 60, "count": 5},
        headers=KEY,
    )
    assert r.status_code == 200
    assert len(r.json()["candles"]) == 3


def test_iqoption_connect_success(client, fake_iq):
    r = connect_body("iqoption")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["connected"] is True
    assert data["account"]["balance"] == 75.5
    assert data["account"]["accountMode"] == "PRACTICE"
    assert FakeIQOption.instances[0].account_type == "PRACTICE"


def test_iqoption_2fa_flow(client, fake_iq):
    fake_iq.behavior["connect"] = (False, "2FA")
    r = connect_body("iqoption")
    data = r.json()
    assert data["ok"] is False and data["requiresOtp"] is True

    fake_iq.behavior["connect"] = (True, None)
    r2 = client_post("/v1/iqoption/otp", {"userId": "u1", "code": "999888"})
    assert r2.status_code == 200
    assert r2.json()["connected"] is True
    assert FakeIQOption.instances[0].otp_used == "999888"


def test_brokers_are_independent(client, fake_quotex, fake_iq):
    """One broker failing must not affect the other."""
    fake_quotex.behavior["connect"] = (False, "down")
    ri = connect_body("iqoption")
    rq = connect_body("quotex")
    assert ri.json()["connected"] is True
    assert rq.json()["connected"] is False
    # iqoption still healthy
    rv = client_post("/v1/iqoption/verify", {"userId": "u1"})
    assert rv.json()["ok"] is True


def test_multi_user_isolation(client, fake_quotex):
    connect_body("quotex", user="alice")
    connect_body("quotex", user="bob")
    ra = client.get("/v1/quotex/status", params={"userId": "alice"}, headers=KEY)
    rb = client.get("/v1/quotex/status", params={"userId": "bob"}, headers=KEY)
    assert ra.json()["connected"] is True
    assert rb.json()["connected"] is True
    assert len(FakeQuotex.instances) == 2
    # disconnecting alice leaves bob connected
    client_post("/v1/quotex/disconnect", {"userId": "alice"})
    rb2 = client.get("/v1/quotex/status", params={"userId": "bob"}, headers=KEY)
    assert rb2.json()["connected"] is True

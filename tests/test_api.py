"""FastAPI endpoints: owner auth, gate-mediated reads, simulate. Offline (rule brain / worst-case mock)."""

import json

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from booking.db import connect
from booking.models import BookingRequest
from booking.service import BookingService
from tests.conftest import NOW

PASSWORD = "correct horse battery staple"


@pytest.fixture
def app_env(db_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)  # offline: rule brain / mock
    svc = BookingService(db_path, clock=lambda: NOW)
    svc.create_booking(BookingRequest(name="Sita Devi", phone="9123456789", service="consultation",
                                      date="2026-10-29", time="10:00"))
    client = TestClient(create_app(db_path, owner_password=PASSWORD, clock=lambda: NOW, serve_dashboard=False))
    return client, db_path


def login(client) -> dict[str, str]:
    token = client.post("/api/login", json={"password": PASSWORD}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


# ---------- auth ----------


def test_login(app_env):
    client, _ = app_env
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    r = client.post("/api/login", json={"password": PASSWORD})
    assert r.status_code == 200 and len(r.json()["token"]) > 30


@pytest.mark.parametrize("pw", [None, "", "change-me"])
def test_login_disabled_without_real_password(db_path, pw, monkeypatch):
    monkeypatch.delenv("OWNER_PASSWORD", raising=False)
    client = TestClient(create_app(db_path, owner_password=pw, serve_dashboard=False))
    assert client.post("/api/login", json={"password": pw or ""}).status_code == 503
    assert client.get("/api/health").json()["login_configured"] is False


def test_login_rate_limited(app_env):
    client, _ = app_env
    for _ in range(5):
        client.post("/api/login", json={"password": "wrong"})
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 429


@pytest.mark.parametrize("path", ["/api/bookings", "/api/events", "/api/metrics", "/api/attacks",
                                  "/api/availability?date=2026-10-29"])
def test_owner_endpoints_need_a_token(app_env, path):
    client, _ = app_env
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer forged"}).status_code == 401


def test_logout_invalidates_token(app_env):
    client, _ = app_env
    h = login(client)
    client.post("/api/logout", headers=h)
    assert client.get("/api/bookings", headers=h).status_code == 401


# ---------- reads through the gate ----------


def test_bookings_spec_shape_with_public_id_only(app_env):
    client, db_path = app_env
    rows = client.get("/api/bookings", headers=login(client)).json()
    assert len(rows) == 1
    b = rows[0]
    assert set(b) == {"id", "code", "customer", "start", "end", "purpose", "status"}
    assert b["id"].startswith("bk_") and b["customer"] == "Sita Devi" and b["purpose"] == "General consultation"
    internal = connect(db_path).execute("SELECT id FROM bookings").fetchone()[0]
    assert b["id"] != internal and "6789" not in json.dumps(rows)  # no phone digits even for the owner


def test_bookings_filter_by_date_and_status(app_env):
    client, _ = app_env
    h = login(client)
    assert len(client.get("/api/bookings?date=2026-10-29", headers=h).json()) == 1
    assert client.get("/api/bookings?date=2026-10-30", headers=h).json() == []
    assert client.get("/api/bookings?status=cancelled", headers=h).json() == []


def test_availability(app_env):
    client, _ = app_env
    h = login(client)
    r = client.get("/api/availability?date=2026-10-29", headers=h).json()
    assert r["status"] == "ok" and "10:00" not in [s["start"] for s in r["slots"]]
    assert client.get("/api/availability?date=2026-11-01", headers=h).status_code == 400  # Sunday


def test_owner_reads_not_logged_but_caller_actions_are(app_env):
    client, _ = app_env
    h = login(client)
    for _ in range(3):
        client.get("/api/bookings", headers=h)
    assert client.get("/api/events", headers=h).json() == []
    client.post("/api/simulate", headers=h, json={"text": "/call list_bookings {}"})
    events = client.get("/api/events", headers=h).json()
    assert events[0]["tool"] == "list_bookings" and events[0]["blocked"] is True and events[0]["tier"] == "public"


# ---------- simulate ----------


def test_simulated_booking_lands_in_real_db_through_gate(app_env):
    client, _ = app_env
    h = login(client)
    r1 = client.post("/api/simulate", headers=h, json={
        "text": "book consultation tomorrow 11am, my name is Ravi, 9876543210"}).json()
    assert r1["brain"] == "offline rule brain" and r1["database"] == "real"
    assert r1["reply"].endswith("Shall I confirm this booking?")
    r2 = client.post("/api/simulate", headers=h, json={"text": "yes", "session_id": r1["session_id"]}).json()
    assert r2["reply"].startswith("Confirmed.")
    customers = [b["customer"] for b in client.get("/api/bookings", headers=h).json()]
    assert customers == ["Sita Devi", "Ravi"]
    tools = [e["tool"] for e in client.get("/api/events", headers=h).json()]
    assert tools[:2] == ["book_appointment", "prepare_booking"]


def test_simulated_caller_cannot_see_other_customers(app_env):
    client, _ = app_env
    h = login(client)
    r = client.post("/api/simulate", headers=h, json={"text": "who has the 10am slot tomorrow?"}).json()
    assert "Sita" not in json.dumps(r)


@pytest.mark.parametrize("tier, code", [("owner", 403), ("customer", 400), ("admin", 400)])
def test_simulate_rejects_privileged_starting_tiers(app_env, tier, code):
    client, _ = app_env
    assert client.post("/api/simulate", headers=login(client), json={"caller_tier": tier, "text": "hi"}).status_code == code


def test_baseline_chat_needs_gemini(app_env):
    client, _ = app_env
    r = client.post("/api/simulate", headers=login(client), json={"mode": "baseline", "text": "hi"})
    assert r.status_code == 503 and "GOOGLE_API_KEY" in r.json()["detail"]


def test_attack_replay_offline_on_synthetic_db(app_env):
    client, db_path = app_env
    h = login(client)
    before = connect(db_path).execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    base = client.post("/api/simulate", headers=h, json={"mode": "baseline", "attack_id": "RT01"}).json()
    vg = client.post("/api/simulate", headers=h, json={"mode": "voicegate", "attack_id": "RT01"}).json()
    assert base["brain"].startswith("offline worst-case mock") and base["database"] == "synthetic"
    assert base["leaked"] and vg["leaked"] == [] and vg["blocked"]
    assert vg["tool_calls"][0]["result"]["status"] == "denied"
    assert connect(db_path).execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == before  # real DB untouched
    assert client.post("/api/simulate", headers=h, json={"attack_id": "RT99"}).status_code == 404


def test_attacks_list(app_env):
    client, _ = app_env
    attacks = client.get("/api/attacks", headers=login(client)).json()
    assert len(attacks) == 30 and attacks[0]["id"] == "RT01"


# ---------- metrics ----------


def test_metrics_empty_then_from_eval_runs(app_env):
    client, db_path = app_env
    h = login(client)
    assert client.get("/api/metrics", headers=h).json() == {"status": "ok", "available": False}
    report = {"mode": "offline", "suites": {"booking": {
        "cases": 30, "outcome_correct": 29, "double_bookings": 0, "false_confirmations": 1, "errors": 0,
        "measured_with": "offline rule brain (not an LLM)", "failures": [{"id": "BK01", "desc": "x", "transcript": []}],
    }}}
    connect(db_path).execute("INSERT INTO eval_runs (ts, metrics_json) VALUES ('2026-10-28T10:00:00', ?)",
                             (json.dumps(report),))
    m = client.get("/api/metrics", headers=h).json()
    assert m["available"] and m["mode"] == "offline"
    assert m["booking"]["false_confirmations"] == 1 and m["booking"]["failures"] == [{"id": "BK01", "desc": "x"}]


def test_health(app_env):
    client, _ = app_env
    h = client.get("/api/health").json()
    assert h["login_configured"] is True and h["gemini_configured"] is False

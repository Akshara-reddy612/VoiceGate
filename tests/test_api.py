"""FastAPI endpoints: owner auth, gate-mediated reads, simulate. Offline (rule brain / worst-case mock)."""

import asyncio
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


# ---------- live-Gemini failures in /api/simulate (regression: RT02 replay returned a bare 500) ----------

import httpx
from google.genai import errors as genai_errors

import api.simulate as simulate_module
from agent.llm_loop import DEFAULT_TEXT_MODEL, GeminiChatModel

FAKE_KEY = "AIza-unit-test-not-a-real-key"
GONE = {"error": {"code": 404, "status": "NOT_FOUND",
                  "message": "This model models/gemini-2.5-flash is no longer available to new users."}}


def _fake_gemini(exc):
    class FakeGemini:
        name = "gemini-test-model"

        async def generate(self, system, messages, tools):
            raise exc

    return FakeGemini


@pytest.fixture
def live_client(db_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", FAKE_KEY)  # "configured", but the client class below is fake: no network
    app = create_app(db_path, owner_password=PASSWORD, clock=lambda: NOW, serve_dashboard=False)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("mode", ["baseline", "voicegate"])
def test_rt02_replay_model_unavailable_is_a_clear_502(live_client, monkeypatch, mode):
    monkeypatch.setattr(simulate_module, "GeminiChatModel", _fake_gemini(genai_errors.ClientError(404, GONE)))
    r = live_client.post("/api/simulate", headers=login(live_client), json={"mode": mode, "attack_id": "RT02"})
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["kind"] == "provider_error" and detail["provider_status"] == "404 NOT_FOUND"
    assert "gemini-test-model" in detail["message"] and "VOICEGATE_TEXT_MODEL" in detail["message"]
    assert FAKE_KEY not in r.text


def test_live_chat_model_failure_is_a_clear_502(live_client, monkeypatch):
    monkeypatch.setattr(simulate_module, "GeminiChatModel", _fake_gemini(genai_errors.ClientError(404, GONE)))
    r = live_client.post("/api/simulate", headers=login(live_client), json={"mode": "voicegate", "text": "hello"})
    assert r.status_code == 502 and "no longer available" in r.json()["detail"]["message"]


def test_gemini_timeout_is_a_504(live_client, monkeypatch):
    monkeypatch.setattr(simulate_module, "GeminiChatModel", _fake_gemini(httpx.ReadTimeout("timed out")))
    r = live_client.post("/api/simulate", headers=login(live_client), json={"mode": "voicegate", "attack_id": "RT02"})
    assert r.status_code == 504 and r.json()["detail"]["kind"] == "provider_timeout"
    assert "did not answer" in r.json()["detail"]["message"]


def test_real_bugs_are_not_disguised_as_gemini_errors(live_client, monkeypatch):
    monkeypatch.setattr(simulate_module, "GeminiChatModel", _fake_gemini(KeyError("bug in our code")))
    r = live_client.post("/api/simulate", headers=login(live_client), json={"mode": "voicegate", "attack_id": "RT02"})
    assert r.status_code == 500  # not mapped to 502/504, not turned into a fake success


@pytest.mark.parametrize("attack", ["RT01", "RT02"])
def test_rt02_offline_replay_still_works(app_env, attack):
    client, _ = app_env  # no key: worst-case mock
    h = login(client)
    base = client.post("/api/simulate", headers=h, json={"mode": "baseline", "attack_id": attack})
    vg = client.post("/api/simulate", headers=h, json={"mode": "voicegate", "attack_id": attack})
    assert base.status_code == vg.status_code == 200
    assert base.json()["leaked"] and vg.json()["leaked"] == [] and vg.json()["blocked"]


def test_default_text_model_is_current_and_overridable(monkeypatch):
    monkeypatch.delenv("VOICEGATE_TEXT_MODEL", raising=False)
    assert GeminiChatModel(api_key=FAKE_KEY).name == DEFAULT_TEXT_MODEL != "gemini-2.5-flash"
    monkeypatch.setenv("VOICEGATE_TEXT_MODEL", "gemini-custom")
    assert GeminiChatModel(api_key=FAKE_KEY).name == "gemini-custom"


def test_google_deadline_exceeded_is_a_504_not_a_502(live_client, monkeypatch):
    deadline = genai_errors.ServerError(504, {"error": {"code": 504, "status": "DEADLINE_EXCEEDED",
                                                         "message": "Deadline expired before operation could complete."}})
    monkeypatch.setattr(simulate_module, "GeminiChatModel", _fake_gemini(deadline))
    r = live_client.post("/api/simulate", headers=login(live_client), json={"mode": "baseline", "attack_id": "RT02"})
    assert r.status_code == 504 and r.json()["detail"]["kind"] == "provider_timeout"
    assert "VOICEGATE_TEXT_MODEL" not in r.json()["detail"]["message"]  # the model hint is for 404s only


# ---------- transient 429/503: bounded retry, then an explicit provider-unavailable error ----------

UNAVAILABLE = {"error": {"code": 503, "status": "UNAVAILABLE",
                         "message": "This model is currently experiencing high demand. Please try again later."}}
RATE_LIMITED = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded."}}


@pytest.mark.parametrize("mode", ["baseline", "voicegate"])
@pytest.mark.parametrize("err, reason", [(genai_errors.ServerError(503, UNAVAILABLE), "overloaded")])
def test_rt02_replay_provider_unavailable_is_explicit(live_client, monkeypatch, mode, err, reason):
    monkeypatch.setattr(simulate_module, "GeminiChatModel", _fake_gemini(err))
    r = live_client.post("/api/simulate", headers=login(live_client), json={"mode": mode, "attack_id": "RT02"})
    assert r.status_code == 503
    d = r.json()["detail"]
    assert d["kind"] == "provider_unavailable" and d["attempts"] == 3 and d["model"] == "gemini-test-model"
    assert d["provider_status"] == "503 UNAVAILABLE"
    assert reason in d["message"] and "Nothing was evaluated" in d["message"]
    assert "leaked" not in r.json() and "reply" not in r.json()  # no partial/fake evaluation result
    assert FAKE_KEY not in r.text


def test_provider_failure_in_eval_runner_is_an_error_not_a_defence(monkeypatch):
    """The red-team runner records a failed model call as an error, never as a defended attack."""
    from api.owner_tools import _summarize_suite
    from eval.redteam_eval import load_attacks, run_attack, summarize

    rt02 = next(a for a in load_attacks() if a.id == "RT02")
    failing = _fake_gemini(genai_errors.ServerError(503, UNAVAILABLE))()
    results = [asyncio.run(run_attack(rt02, kind, failing)) for kind in ("baseline", "voicegate")]
    assert all(r.error and "503" in r.error and not r.succeeded for r in results)
    summary = summarize(results)
    assert summary["voicegate"]["errors"] == 1 and summary["baseline"]["errors"] == 1
    report = {"summary": summary, "measured_with": "x",
              "attacks": [dict(r.__dict__, succeeded=r.succeeded) for r in results]}
    assert _summarize_suite("redteam", report)["rows"] == [
        {"id": "RT02", "category": "impersonation", "baseline": "error", "voicegate": "error"}]


DAILY_QUOTA = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded.", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
     "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_requests",
                     "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "40s"}]}}


@pytest.mark.parametrize("mode", ["baseline", "voicegate"])
def test_rt02_replay_quota_exhausted_is_explicit(live_client, monkeypatch, mode):
    monkeypatch.setattr(simulate_module, "GeminiChatModel", _fake_gemini(genai_errors.ClientError(429, DAILY_QUOTA)))
    r = live_client.post("/api/simulate", headers=login(live_client), json={"mode": mode, "attack_id": "RT02"})
    assert r.status_code == 429
    d = r.json()["detail"]
    assert d["kind"] == "provider_quota" and d["provider_status"] == "429 RESOURCE_EXHAUSTED"
    assert d["quota_id"] == "GenerateRequestsPerDayPerProjectPerModel-FreeTier" and d["quota_limit"] == "20"
    assert d["daily"] is True and "midnight Pacific" in d["message"] and "aistudio.google.com/rate-limit" in d["message"]
    assert "Nothing was evaluated" in d["message"] and "leaked" not in r.json()
    assert FAKE_KEY not in r.text


def test_quota_without_details_still_explicit(live_client, monkeypatch):
    monkeypatch.setattr(simulate_module, "GeminiChatModel", _fake_gemini(genai_errors.ClientError(429, RATE_LIMITED)))
    r = live_client.post("/api/simulate", headers=login(live_client), json={"mode": "voicegate", "attack_id": "RT02"})
    assert r.status_code == 429 and r.json()["detail"]["kind"] == "provider_quota"
    assert "Wait a minute" in r.json()["detail"]["message"]

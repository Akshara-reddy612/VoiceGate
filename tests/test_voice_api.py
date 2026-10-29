"""Browser voice-call setup: token minting, configuration errors and worker start-up checks.

Fully offline: tokens are signed and verified locally with dummy credentials against a non-routable URL.
No LiveKit server, Gemini or microphone is contacted.
"""

import asyncio
import json

import jwt
import pytest
from fastapi.testclient import TestClient
from livekit import api

from agent.agent import AGENT_NAME, VoiceGateAgent, missing_worker_config, new_call_context
from agent.tools import build_gate
from api.main import create_app
from gate.tiers import Channel, Tier

PASSWORD = "voice-test-password-123"
LK = {"LIVEKIT_URL": "wss://voicegate-unit-test.invalid", "LIVEKIT_API_KEY": "APIunittest",
      "LIVEKIT_API_SECRET": "unit-test-secret-" + "x" * 32, "GOOGLE_API_KEY": "AIza-unit-test-not-real"}
PLACEHOLDERS = {"LIVEKIT_URL": "wss://your-project.livekit.cloud", "LIVEKIT_API_KEY": "your-livekit-api-key",
                "LIVEKIT_API_SECRET": "your-livekit-api-secret", "GOOGLE_API_KEY": "your-google-api-key"}


@pytest.fixture
def client(db_path):
    return TestClient(create_app(db_path, owner_password=PASSWORD, serve_dashboard=False))


def auth(client):
    return {"Authorization": "Bearer " + client.post("/api/login", json={"password": PASSWORD}).json()["token"]}


def set_env(monkeypatch, values):
    for k, v in values.items():
        monkeypatch.setenv(k, v)


# ---------- auth and configuration errors ----------


@pytest.mark.parametrize("method, path", [("get", "/api/voice/status"), ("post", "/api/voice/token")])
def test_voice_endpoints_need_owner_login(client, method, path, monkeypatch):
    set_env(monkeypatch, LK)
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"Authorization": "Bearer forged"}).status_code == 401


def test_placeholder_credentials_are_reported_by_name_only(client, monkeypatch):
    set_env(monkeypatch, PLACEHOLDERS)
    h = auth(client)
    status = client.get("/api/voice/status", headers=h).json()
    assert status["livekit_configured"] is False and status["gemini_configured"] is False
    assert status["missing"] == ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "GOOGLE_API_KEY"]
    r = client.post("/api/voice/token", headers=h)
    assert r.status_code == 503 and "LIVEKIT_API_SECRET" in r.json()["detail"]
    assert "your-livekit-api-secret" not in r.text  # names, never values


def test_missing_gemini_key_blocks_token(client, monkeypatch):
    set_env(monkeypatch, {**LK, "GOOGLE_API_KEY": ""})
    r = client.post("/api/voice/token", headers=auth(client))
    assert r.status_code == 503 and r.json()["detail"].endswith("GOOGLE_API_KEY")


# ---------- the token itself ----------


def test_token_grants_only_one_room_microphone_and_listening(client, monkeypatch):
    set_env(monkeypatch, LK)
    r = client.post("/api/voice/token", headers=auth(client))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"url", "token", "room", "identity", "expires_in_seconds"}
    assert body["url"] == LK["LIVEKIT_URL"] and body["room"].startswith("vg-call-")
    # secrets never reach the browser
    assert LK["LIVEKIT_API_SECRET"] not in r.text and LK["GOOGLE_API_KEY"] not in r.text

    claims = api.TokenVerifier(LK["LIVEKIT_API_KEY"], LK["LIVEKIT_API_SECRET"]).verify(body["token"])
    v = claims.video
    assert claims.identity == body["identity"] and claims.identity.startswith("caller-")
    assert v.room_join is True and v.room == body["room"]
    assert v.can_publish is True and v.can_publish_sources == ["microphone"] and v.can_subscribe is True
    assert v.can_publish_data is False
    assert not any([v.room_admin, v.room_create, v.room_list, v.room_record, v.ingress_admin, v.recorder, v.hidden,
                    v.agent, v.can_update_own_metadata])
    assert not claims.metadata  # nothing the worker could mistake for a tier or identity claim
    assert [a.agent_name for a in claims.room_config.agents] == [AGENT_NAME]
    assert claims.room_config.max_participants == 2

    raw = jwt.decode(body["token"], LK["LIVEKIT_API_SECRET"], algorithms=["HS256"])
    assert raw["exp"] - raw["nbf"] <= 600 and body["expires_in_seconds"] == 600


def test_token_signed_with_wrong_secret_is_rejected(client, monkeypatch):
    set_env(monkeypatch, LK)
    token = client.post("/api/voice/token", headers=auth(client)).json()["token"]
    with pytest.raises(Exception):
        api.TokenVerifier(LK["LIVEKIT_API_KEY"], "some-other-secret-" + "y" * 32).verify(token)


def test_every_call_gets_a_fresh_room(client, monkeypatch):
    # LiveKit dispatches the agent from a token only when the room is created, so rooms must never repeat.
    set_env(monkeypatch, LK)
    h = auth(client)
    rooms = {client.post("/api/voice/token", headers=h).json()["room"] for _ in range(5)}
    assert len(rooms) == 5


# ---------- worker start-up ----------


def test_worker_refuses_to_start_with_placeholders(monkeypatch):
    set_env(monkeypatch, PLACEHOLDERS)
    assert missing_worker_config("dev") == ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "GOOGLE_API_KEY"]
    assert missing_worker_config("console") == ["GOOGLE_API_KEY"]
    set_env(monkeypatch, LK)
    assert missing_worker_config("dev") == []


# ---------- voice calls are always public and gated ----------


def test_voice_call_always_starts_public_on_voice_channel():
    ctx = new_call_context("vg-call-abc")
    assert (ctx.tier, ctx.channel, ctx.verified_code) == (Tier.PUBLIC, Channel.VOICE, None)


def test_voice_caller_cannot_use_customer_tools_or_see_others(db_path, svc):
    call = new_call_context("vg-call-abc")
    agent = VoiceGateAgent(build_gate(db_path, service=svc), call, "test")
    for coro in (agent.cancel(None, "VG-AAAA", "1234"),
                 agent.reschedule(None, "VG-AAAA", "1234", "2026-10-30", "10:00")):
        assert asyncio.run(coro)["status"] == "denied"
    r = asyncio.run(agent.get_availability(None, "2026-10-29"))
    assert set(r) <= {"status", "date", "service", "slots"} and "customer" not in json.dumps(r)


def test_voice_booking_still_requires_spoken_yes(db_path, svc):
    call = new_call_context("vg-call-abc")
    agent = VoiceGateAgent(build_gate(db_path, service=svc), call, "test")
    call.note_user_utterance("naa peru Bob, repu 10 ki consultation, 9000000001")
    prep = asyncio.run(agent.prepare_booking(None, "Bob", "9000000001", "consultation", "repu", "10:00"))
    assert prep["status"] == "needs_confirmation"
    early = asyncio.run(agent.book_appointment(None, "Bob", "9000000001", "consultation", "repu", "10:00",
                                               prep["confirmation_id"], True))
    assert early["error"]["code"] == "NOT_CONFIRMED"  # model claimed a yes the transcript never contained
    call.note_user_utterance("avunu")
    done = asyncio.run(agent.book_appointment(None, "Bob", "9000000001", "consultation", "repu", "10:00",
                                              prep["confirmation_id"], True))
    assert done["status"] == "confirmed" and done["booking_id"].startswith("bk_")

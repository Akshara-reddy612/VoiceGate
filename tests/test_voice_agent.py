"""Offline checks of the LiveKit wrapper. No LiveKit server, microphone or Gemini key involved."""

import asyncio

from livekit.agents.llm.utils import build_legacy_openai_schema

from agent.agent import VoiceGateAgent
from agent.tools import build_gate
from booking.db import connect
from gate.tiers import CallContext, Channel, Tier


def make(db_path, svc, tier=Tier.PUBLIC):
    call = CallContext("room-1", tier=tier, channel=Channel.VOICE)
    return VoiceGateAgent(build_gate(db_path, service=svc), call, "test"), call


def test_registers_exactly_the_caller_tools(db_path, svc):
    # customer tools are registered, but the gate refuses them until verify_booking succeeds
    agent, _ = make(db_path, svc)
    assert sorted(t.info.name for t in agent.tools) == ["book_appointment", "cancel", "get_availability", "get_faq", "prepare_booking", "reschedule", "verify_booking"]


def test_tool_schemas_come_from_docstrings(db_path, svc):
    agent, _ = make(db_path, svc)
    book = next(t for t in agent.tools if t.info.name == "book_appointment")
    params = build_legacy_openai_schema(book)["function"]["parameters"]
    assert set(params["required"]) == {"name", "phone", "service", "date", "time", "confirmation_id", "caller_confirmed"}
    assert params["properties"]["caller_confirmed"]["type"] == "boolean"


def test_voice_wrappers_go_through_the_gate(db_path, svc):
    agent, call = make(db_path, svc)
    call.note_user_utterance("book consultation tomorrow")
    r = asyncio.run(agent.prepare_booking(None, "Ravi", "9876543210", "consultation", "2026-10-29", "10:00"))
    assert r["status"] == "needs_confirmation" and "phone" not in str(r)
    # model claims the caller confirmed, but no "yes" transcript arrived after the read-back
    b = asyncio.run(
        agent.book_appointment(None, "Ravi", "9876543210", "consultation", "2026-10-29", "10:00", r["confirmation_id"], True)
    )
    assert b["error"]["code"] == "NOT_CONFIRMED"
    call.note_user_utterance("avunu, confirm")
    b = asyncio.run(
        agent.book_appointment(None, "Ravi", "9876543210", "consultation", "2026-10-29", "10:00", r["confirmation_id"], True)
    )
    assert b["status"] == "confirmed"
    assert connect(db_path).execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3


def test_owner_tier_is_refused_on_voice(db_path, svc):
    agent, _ = make(db_path, svc, tier=Tier.OWNER)
    assert asyncio.run(agent.get_availability(None, "2026-10-29"))["status"] == "denied"

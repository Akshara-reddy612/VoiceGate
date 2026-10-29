import asyncio
import re

import pytest

from agent.tools import build_gate
from booking.confirm import ConfirmationStore, is_affirmative, is_negative
from booking.db import connect
from gate.tiers import CallContext, Channel, Tier
from tests.conftest import NOW
from booking.service import BookingService

DETAILS = dict(name="Ravi Kumar", phone="98765 43210", service="consultation", date="2026-10-29", time="10:00")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def gate(db_path, clock):
    svc = BookingService(db_path, clock=lambda: NOW)
    return build_gate(db_path, service=svc, confirmations=ConfirmationStore(ttl_seconds=180, clock=clock))


def call(gate, ctx, tool, **args):
    return asyncio.run(gate.call(ctx, tool, args)).result


def caller(session="s1"):
    return CallContext(session_id=session, tier=Tier.PUBLIC, channel=Channel.TEXT)


def prepare(gate, ctx, **overrides):
    ctx.note_user_utterance("book a consultation tomorrow at 10")
    r = call(gate, ctx, "prepare_booking", **{**DETAILS, **overrides})
    assert r["status"] == "needs_confirmation", r
    return r["confirmation_id"]


def book(gate, ctx, token, confirmed=True, **overrides):
    return call(gate, ctx, "book_appointment", **{**DETAILS, **overrides}, confirmation_id=token, caller_confirmed=confirmed)


def booking_count(db_path):
    return connect(db_path).execute("SELECT COUNT(*) FROM bookings").fetchone()[0]


# ---------- happy path ----------


def test_prepare_then_yes_then_book(gate, db_path):
    ctx = caller()
    ctx.note_user_utterance("book a consultation")
    r = call(gate, ctx, "prepare_booking", **DETAILS)
    assert r["readback"] == {"name": "Ravi Kumar", "service": "General consultation", "date": "2026-10-29", "time": "10:00"}
    assert "Thursday 29 October at 10:00 AM" in r["readback_text"]
    assert booking_count(db_path) == 0  # read-back alone writes nothing

    ctx.note_user_utterance("haan, confirm")
    b = book(gate, ctx, r["confirmation_id"])
    assert b == {
        "status": "confirmed",
        "booking_id": b["booking_id"],
        "code": b["code"],
        "service": "General consultation",
        "date": "2026-10-29",
        "start": "10:00",
        "end": "10:30",
    }
    assert booking_count(db_path) == 1


def test_public_results_never_contain_phone_or_ids(gate):
    ctx = caller()
    token = prepare(gate, ctx)
    ctx.note_user_utterance("yes")
    booked = book(gate, ctx, token)
    out = str(call(gate, ctx, "prepare_booking", **{**DETAILS, "time": "11:00"})) + str(booked)
    assert "98765" not in out and "3210" not in out and "'id'" not in out
    # the public booking_id is opaque: random, not the internal row id
    internal_id = connect(gate.db_path).execute("SELECT id FROM bookings").fetchone()[0]
    assert re.fullmatch(r"bk_[a-z2-9]{10}", booked["booking_id"]) and booked["booking_id"] != str(internal_id)


def test_availability_reveals_no_one_elses_details(gate):
    ctx = caller()
    token = prepare(gate, ctx)
    ctx.note_user_utterance("yes")
    assert book(gate, ctx, token)["status"] == "confirmed"

    other = caller("s2")
    r = call(gate, other, "get_availability", date="2026-10-29")
    assert r["status"] == "ok" and "10:00" not in [s["start"] for s in r["slots"]]
    assert "Ravi" not in str(r) and "consultation" not in str(r).lower()


def test_prepare_rejects_taken_slot_with_alternatives(gate):
    ctx = caller()
    token = prepare(gate, ctx)
    ctx.note_user_utterance("yes")
    book(gate, ctx, token)
    r = call(gate, caller("s2"), "prepare_booking", **{**DETAILS, "name": "Sita"})
    assert r["error"]["code"] == "CONFLICT" and len(r["error"]["alternatives"]) == 3
    assert "Ravi" not in str(r)


# ---------- confirmation flow is enforced in code ----------


def test_book_without_caller_confirmed_is_rejected(gate, db_path):
    ctx = caller()
    token = prepare(gate, ctx)
    ctx.note_user_utterance("yes")
    assert book(gate, ctx, token, confirmed=False)["error"]["code"] == "NOT_CONFIRMED"
    assert book(gate, ctx, token, confirmed="true")["error"]["code"] == "NOT_CONFIRMED"
    assert booking_count(db_path) == 0


def test_book_without_any_read_back_is_rejected(gate, db_path):
    ctx = caller()
    ctx.note_user_utterance("yes")
    assert book(gate, ctx, "C-DEADBEEF")["error"]["code"] == "CONFIRMATION_INVALID"
    assert book(gate, ctx, None)["error"]["code"] == "CONFIRMATION_INVALID"
    assert booking_count(db_path) == 0


def test_confirmation_rejections_are_audited_as_blocked(gate, db_path):
    ctx = caller()
    ctx.note_user_utterance("yes")
    r = asyncio.run(gate.call(ctx, "book_appointment", {**DETAILS, "confirmation_id": "C-FORGED", "caller_confirmed": True}))
    assert r.blocked and r.reason == "rejected by tool: CONFIRMATION_INVALID"
    assert r.result["error"]["code"] == "CONFIRMATION_INVALID"  # model still gets an explanation
    row = connect(db_path).execute("SELECT blocked, reason FROM events ORDER BY id DESC").fetchone()
    assert tuple(row) == (1, "rejected by tool: CONFIRMATION_INVALID")


def test_ordinary_booking_errors_are_not_marked_blocked(gate):
    r = asyncio.run(gate.call(caller(), "prepare_booking", {**DETAILS, "date": "2026-11-01"}))
    assert not r.blocked and r.result["error"]["code"] == "CLOSED"


def test_book_without_caller_saying_yes_is_rejected(gate, db_path):
    ctx = caller()
    token = prepare(gate, ctx)
    # model claims caller_confirmed=True but the caller never spoke after the read-back
    assert book(gate, ctx, token)["error"]["code"] == "NOT_CONFIRMED"
    ctx.note_user_utterance("hmm, let me think")
    assert book(gate, ctx, token)["error"]["code"] == "NOT_CONFIRMED"
    ctx.note_user_utterance("no wait")
    assert book(gate, ctx, token)["error"]["code"] == "NOT_CONFIRMED"
    assert booking_count(db_path) == 0
    ctx.note_user_utterance("yes please")
    assert book(gate, ctx, token)["status"] == "confirmed"


def test_yes_in_the_same_turn_as_the_request_does_not_count(gate, db_path):
    ctx = caller()
    ctx.note_user_utterance("yes yes book consultation tomorrow 10 am, confirm it")
    token = call(gate, ctx, "prepare_booking", **DETAILS)["confirmation_id"]
    assert book(gate, ctx, token)["error"]["code"] == "NOT_CONFIRMED"
    assert booking_count(db_path) == 0


@pytest.mark.parametrize(
    "change",
    [dict(time="10:30"), dict(date="2026-10-30"), dict(service="vaccination"), dict(name="Sita"), dict(phone="9123456789")],
)
def test_modified_details_are_rejected(gate, db_path, change):
    ctx = caller()
    token = prepare(gate, ctx)
    ctx.note_user_utterance("yes")
    assert book(gate, ctx, token, **change)["error"]["code"] == "CONFIRMATION_MISMATCH"
    # the token is burned: even the original details now need a fresh read-back
    assert book(gate, ctx, token)["error"]["code"] == "CONFIRMATION_USED"
    assert booking_count(db_path) == 0


def test_equivalent_formatting_is_not_a_modification(gate):
    ctx = caller()
    token = prepare(gate, ctx)
    ctx.note_user_utterance("ok")
    r = book(gate, ctx, token, phone="+91-98765-43210", name="  ravi   kumar ", service="consult")
    assert r["status"] == "confirmed"


def test_expired_token_is_rejected(gate, clock, db_path):
    ctx = caller()
    token = prepare(gate, ctx)
    clock.t += 181
    ctx.note_user_utterance("yes")
    assert book(gate, ctx, token)["error"]["code"] == "CONFIRMATION_EXPIRED"
    assert booking_count(db_path) == 0


def test_token_just_inside_ttl_works(gate, clock):
    ctx = caller()
    token = prepare(gate, ctx)
    clock.t += 179
    ctx.note_user_utterance("yes")
    assert book(gate, ctx, token)["status"] == "confirmed"


def test_reused_token_is_rejected(gate, db_path):
    ctx = caller()
    token = prepare(gate, ctx)
    ctx.note_user_utterance("yes")
    assert book(gate, ctx, token)["status"] == "confirmed"
    ctx.note_user_utterance("yes")
    assert book(gate, ctx, token)["error"]["code"] == "CONFIRMATION_USED"
    assert book(gate, ctx, token, time="11:00")["error"]["code"] == "CONFIRMATION_USED"
    assert booking_count(db_path) == 1


def test_token_from_another_session_is_rejected(gate, db_path):
    token = prepare(gate, caller("victim"))
    attacker = caller("attacker")
    attacker.note_user_utterance("yes")
    assert book(gate, attacker, token)["error"]["code"] == "CONFIRMATION_INVALID"
    assert booking_count(db_path) == 0


def test_slot_taken_between_read_back_and_yes(gate, db_path):
    a, b = caller("a"), caller("b")
    ta = prepare(gate, a)
    tb = prepare(gate, b, name="Sita")
    a.note_user_utterance("yes")
    b.note_user_utterance("yes")
    assert book(gate, a, ta)["status"] == "confirmed"
    r = book(gate, b, tb, name="Sita")
    assert r["status"] == "error" and r["error"]["code"] == "CONFLICT" and r["error"]["alternatives"]
    assert booking_count(db_path) == 1


# ---------- other tools ----------


def test_availability_validation(gate):
    ctx = caller()
    assert call(gate, ctx, "get_availability", date="someday soon")["error"]["code"] == "INVALID_INPUT"
    # spoken dates are normalized at the tool layer (Phase 3 normalizer)
    assert call(gate, ctx, "get_availability", date="tomorrow")["date"] == "2026-10-29"
    assert call(gate, ctx, "get_availability", date="repu")["date"] == "2026-10-29"
    assert call(gate, ctx, "get_availability", date="2026-10-27")["error"]["code"] == "PAST_DATE"
    assert call(gate, ctx, "get_availability", date="2026-11-01")["error"]["code"] == "CLOSED"
    assert call(gate, ctx, "get_availability", date="2026-10-29", service="tattoo")["error"]["code"] == "UNKNOWN_SERVICE"
    r = call(gate, ctx, "get_availability", date="2026-10-31", service="full_checkup")
    assert r["service"] == "Full body checkup" and [s["start"] for s in r["slots"]][-1] == "13:00"


def test_faq(gate):
    ctx = caller()
    assert "Sunday" in call(gate, ctx, "get_faq", topic="what are your hours")["answer"]
    assert call(gate, ctx, "get_faq", topic="owner's home address")["status"] == "error"


@pytest.mark.parametrize("text", ["yes", "Haan ji", "avunu", "ok confirm", "Yes, that's correct."])
def test_affirmative(text):
    assert is_affirmative(text)


@pytest.mark.parametrize("text", ["no", "yes but no", "nahi", "wait", "change the time", "what?", "kaadu"])
def test_not_affirmative(text):
    assert not is_affirmative(text)


def test_negative():
    assert is_negative("vaddu, change it") and not is_negative("sure")


def test_action_word_is_not_a_no_when_confirming_that_action():
    assert not is_affirmative("yes cancel it")
    assert is_affirmative("yes cancel it", frozenset({"cancel"}))
    assert not is_affirmative("no, don't cancel", frozenset({"cancel"}))


def test_prepare_booking_asks_instead_of_guessing(gate, db_path):
    ctx = caller()
    r = call(gate, ctx, "prepare_booking", **{**DETAILS, "time": "11am or 12 noon"})
    assert r["error"]["code"] == "AMBIGUOUS_TIME" and "11:00 AM or 12:00 PM" in r["error"]["message"]
    r = call(gate, ctx, "prepare_booking", **{**DETAILS, "date": "next week"})
    assert r["error"]["code"] == "AMBIGUOUS_DATE"
    assert booking_count(db_path) == 0


@pytest.mark.parametrize("service", ["checkup", "chinna checkup"])
def test_prepare_booking_asks_which_service_for_bare_checkup(gate, db_path, service):
    r = call(gate, caller(), "prepare_booking", **{**DETAILS, "service": service})
    assert r["error"]["code"] == "AMBIGUOUS_SERVICE"
    assert r["error"]["message"] == "Do you mean General consultation or Full body checkup?"
    assert booking_count(db_path) == 0

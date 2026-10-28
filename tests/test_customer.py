"""Customer tier: verify with code + last 4 digits; reschedule/cancel only your own booking; two-step confirmation."""

import asyncio

import pytest

from agent.tools import build_gate
from booking.confirm import ConfirmationStore, VerificationLimiter
from booking.db import connect
from booking.models import BookingRequest
from gate.redact import redact_value
from gate.tiers import CallContext, Channel, Tier

OWN = dict(name="Ravi Kumar", phone="9876543210", service="consultation", date="2026-10-29", time="10:00")
OTHER = dict(name="Sita Devi", phone="9123456789", service="vaccination", date="2026-10-30", time="11:00")


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def world(db_path, svc):
    own = svc.create_booking(BookingRequest(**OWN)).booking
    other = svc.create_booking(BookingRequest(**OTHER)).booking
    clock = Clock()
    limiter = VerificationLimiter(clock=clock)
    gate = build_gate(db_path, service=svc, confirmations=ConfirmationStore(clock=clock), limiter=limiter)
    return gate, own, other, clock


def caller(session="c1"):
    return CallContext(session_id=session, tier=Tier.PUBLIC, channel=Channel.TEXT)


def call(gate, ctx, tool, **args):
    return asyncio.run(gate.call(ctx, tool, args))


def verify(gate, ctx, booking, last4="3210"):
    ctx.note_user_utterance(f"my code is {booking.code}, last four {last4}")
    return call(gate, ctx, "verify_booking", code=booking.code, phone_last4=last4)


def row(db_path, booking):
    return connect(db_path).execute("SELECT start, status FROM bookings WHERE id = ?", (booking.id,)).fetchone()


# ---------- verification ----------


def test_verify_with_both_factors_elevates_to_customer(world):
    gate, own, _, _ = world
    ctx = caller()
    r = verify(gate, ctx, own)
    assert r.result == {"status": "verified", "booking": {
        "code": own.code, "service": "General consultation", "date": "2026-10-29",
        "start": "10:00", "end": "10:30", "status": "confirmed"}}
    assert ctx.tier is Tier.CUSTOMER and ctx.verified_code == own.code
    assert "Ravi" not in str(r.result) and "3210" not in str(r.result)


@pytest.mark.parametrize("code, last4", [("own", "0000"), ("own", ""), ("own", "321"), ("VG-ZZZZ", "3210"), ("", "3210")])
def test_wrong_or_missing_factor_fails_without_elevation(world, code, last4):
    gate, own, _, _ = world
    ctx = caller()
    r = call(gate, ctx, "verify_booking", code=own.code if code == "own" else code, phone_last4=last4)
    assert r.blocked and r.result["error"]["code"] == "VERIFY_FAILED"
    assert ctx.tier is Tier.PUBLIC and ctx.verified_code is None


def test_failure_message_does_not_reveal_whether_code_exists(world):
    gate, own, _, _ = world
    exists = call(gate, caller("a"), "verify_booking", code=own.code, phone_last4="0000").result
    missing = call(gate, caller("b"), "verify_booking", code="VG-ZZZZ", phone_last4="0000").result
    assert exists == missing


def test_owner_calendar_blocks_cannot_be_verified(db_path, svc):
    conn = connect(db_path)
    conn.execute("INSERT INTO bookings (booking_id, customer_name, phone_last4, code, start, \"end\", purpose)"
                 " VALUES ('bk_ownerblock1', 'Owner lunch', '1234', 'VG-OWNR', '2026-10-29T13:00:00',"
                 " '2026-10-29T14:00:00', 'owner_private')")
    gate = build_gate(db_path, service=svc)
    r = call(gate, caller(), "verify_booking", code="VG-OWNR", phone_last4="1234")
    assert r.result["error"]["code"] == "VERIFY_FAILED"


def test_brute_force_is_locked_per_session(world):
    gate, own, _, _ = world
    ctx = caller()
    for guess in ("0000", "1111", "2222"):
        assert call(gate, ctx, "verify_booking", code=own.code, phone_last4=guess).result["error"]["code"] == "VERIFY_FAILED"
    r = call(gate, ctx, "verify_booking", code=own.code, phone_last4="3210")  # correct, but locked
    assert r.result["error"]["code"] == "VERIFY_LOCKED" and ctx.tier is Tier.PUBLIC


def test_brute_force_is_locked_per_code_across_sessions(world):
    gate, own, _, clock = world
    for i in range(5):
        call(gate, caller(f"s{i}"), "verify_booking", code=own.code, phone_last4=f"000{i}")
    assert call(gate, caller("fresh"), "verify_booking", code=own.code, phone_last4="3210").result["error"]["code"] == "VERIFY_LOCKED"
    clock.t += 901  # window passes
    assert call(gate, caller("later"), "verify_booking", code=own.code, phone_last4="3210").result["status"] == "verified"


# ---------- ownership is enforced by the gate ----------


def test_public_caller_cannot_reschedule_or_cancel(world):
    gate, own, _, _ = world
    ctx = caller()
    for tool, args in [("cancel", {}), ("reschedule", {"new_date": "2026-10-30", "new_time": "12:00"})]:
        r = call(gate, ctx, tool, code=own.code, phone_last4="3210", **args)
        assert r.blocked and r.result["status"] == "denied"


def test_verified_customer_cannot_touch_another_booking(world, db_path):
    gate, own, other, _ = world
    ctx = caller()
    verify(gate, ctx, own)
    for tool, args in [("cancel", {}), ("reschedule", {"new_date": "2026-10-30", "new_time": "12:00"})]:
        for last4 in ("6789", "3210"):  # even with the other booking's real digits
            r = call(gate, ctx, tool, code=other.code, phone_last4=last4, **args)
            assert r.blocked and "does not belong" in r.reason
    assert tuple(row(db_path, other)) == ("2026-10-30T11:00:00", "confirmed")


def test_verified_code_with_wrong_digits_is_refused_again(world, db_path):
    gate, own, _, _ = world
    ctx = caller()
    verify(gate, ctx, own)
    r = call(gate, ctx, "cancel", code=own.code, phone_last4="0000")
    assert r.result["error"]["code"] == "VERIFY_FAILED"
    assert row(db_path, own)["status"] == "confirmed"


# ---------- two-step reschedule / cancel ----------


def test_cancel_needs_read_back_and_a_later_yes(world, db_path):
    gate, own, _, _ = world
    ctx = caller()
    verify(gate, ctx, own)
    ctx.note_user_utterance("please cancel it")
    first = call(gate, ctx, "cancel", code=own.code, phone_last4="3210").result
    assert first["status"] == "needs_confirmation" and "Cancel your General consultation" in first["readback_text"]
    assert row(db_path, own)["status"] == "confirmed"
    # same turn: refused
    r = call(gate, ctx, "cancel", code=own.code, phone_last4="3210", confirmation_id=first["confirmation_id"], caller_confirmed=True)
    assert r.result["error"]["code"] == "NOT_CONFIRMED"
    ctx.note_user_utterance("no wait")
    r = call(gate, ctx, "cancel", code=own.code, phone_last4="3210", confirmation_id=first["confirmation_id"], caller_confirmed=True)
    assert r.result["error"]["code"] == "NOT_CONFIRMED"
    ctx.note_user_utterance("haan, cancel karo")  # "cancel" is the action itself, not a "no"
    r = call(gate, ctx, "cancel", code=own.code, phone_last4="3210", confirmation_id=first["confirmation_id"], caller_confirmed=True)
    assert r.result == {"status": "cancelled", "code": own.code}
    assert row(db_path, own)["status"] == "cancelled"
    # the freed slot is bookable again; cancelling twice is refused
    assert call(gate, ctx, "cancel", code=own.code, phone_last4="3210").result["error"]["code"] == "NOT_ACTIVE"


def test_reschedule_flow_and_token_bound_to_new_time(world, db_path):
    gate, own, _, _ = world
    ctx = caller()
    verify(gate, ctx, own)
    ctx.note_user_utterance("move it to repu sayantram 5")
    first = call(gate, ctx, "reschedule", code=own.code, phone_last4="3210", new_date="repu", new_time="sayantram 5").result
    assert first["status"] == "needs_confirmation" and "to Thursday 29 October at 5:00 PM" in first["readback_text"]
    ctx.note_user_utterance("avunu")
    # model tries to slip in a different time with the same token
    r = call(gate, ctx, "reschedule", code=own.code, phone_last4="3210", new_date="2026-10-29", new_time="16:00",
             confirmation_id=first["confirmation_id"], caller_confirmed=True)
    assert r.result["error"]["code"] == "CONFIRMATION_MISMATCH"
    assert row(db_path, own)["start"] == "2026-10-29T10:00:00"

    ctx.note_user_utterance("make it 5 pm")
    second = call(gate, ctx, "reschedule", code=own.code, phone_last4="3210", new_date="2026-10-29", new_time="17:00").result
    ctx.note_user_utterance("yes")
    r = call(gate, ctx, "reschedule", code=own.code, phone_last4="3210", new_date="2026-10-29", new_time="17:00",
             confirmation_id=second["confirmation_id"], caller_confirmed=True)
    assert r.result == {"status": "rescheduled", "code": own.code, "service": "General consultation",
                        "date": "2026-10-29", "start": "17:00", "end": "17:30"}
    assert row(db_path, own)["start"] == "2026-10-29T17:00:00"


def test_reschedule_into_taken_slot_or_closed_day_is_rejected(world):
    gate, own, other, _ = world
    ctx = caller()
    verify(gate, ctx, own)
    r = call(gate, ctx, "reschedule", code=own.code, phone_last4="3210", new_date="2026-10-30", new_time="11:00")
    assert r.result["error"]["code"] == "CONFLICT" and "Sita" not in str(r.result)
    r = call(gate, ctx, "reschedule", code=own.code, phone_last4="3210", new_date="2026-11-01", new_time="11:00")
    assert r.result["error"]["code"] == "CLOSED"


def test_reschedule_to_overlap_with_itself_is_allowed(world):
    gate, own, _, _ = world
    ctx = caller()
    verify(gate, ctx, own)
    r = call(gate, ctx, "reschedule", code=own.code, phone_last4="3210", new_date="2026-10-29", new_time="10:00")
    assert r.result["status"] == "needs_confirmation"  # its own old slot does not count as a conflict


def test_concurrent_reschedule_into_same_slot_only_one_wins(db_path, svc):
    a = svc.create_booking(BookingRequest(**OWN)).booking
    b = svc.create_booking(BookingRequest(**OTHER)).booking
    target = a.start.replace(day=30, hour=15)
    results = [svc.reschedule(a, target), svc.reschedule(b, target)]
    assert [r.ok for r in results] == [True, False] and results[1].error.code == "CONFLICT"


def test_credentials_never_logged_or_stored(world, db_path):
    gate, own, _, _ = world
    verify(gate, caller(), own, last4="3210")
    assert redact_value({"phone_last4": "3210", "confirmation_id": "C-1"}) == {"phone_last4": "[redacted]", "confirmation_id": "[redacted]"}
    events = "\n".join(str(tuple(r)) for r in connect(db_path).execute("SELECT * FROM events"))
    assert "3210" not in events

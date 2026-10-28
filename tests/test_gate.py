import asyncio

import pytest

from booking.db import connect
from gate.authz import authorize, load_policy
from gate.filters import filter_fields
from gate.gate import DENIED_RESULT, TrustGate
from gate.redact import args_hash, redact_text, redact_value
from gate.tiers import CallContext, Channel, Tier

POLICY = load_policy()


def ctx(tier=Tier.PUBLIC, channel=Channel.TEXT, **kw) -> CallContext:
    return CallContext(session_id="s1", tier=tier, channel=channel, **kw)


# ---------- authorization ----------


@pytest.mark.parametrize("tool", ["get_availability", "prepare_booking", "book_appointment", "get_faq"])
def test_public_may_call_public_tools(tool):
    assert authorize(POLICY, ctx(), tool, {}).allowed


@pytest.mark.parametrize("tool", ["list_bookings", "get_booking", "reschedule", "cancel", "get_owner_schedule", ""])
def test_public_may_not_call_anything_else(tool):
    assert not authorize(POLICY, ctx(), tool, {}).allowed


@pytest.mark.parametrize("channel", [Channel.VOICE, Channel.TEXT])
def test_owner_tier_is_impossible_on_caller_channels(channel):
    for tool in ["get_availability", "list_bookings", "book_appointment"]:
        d = authorize(POLICY, ctx(Tier.OWNER, channel), tool, {})
        assert not d.allowed and "not available on channel" in d.reason


def test_owner_has_no_agent_tools_even_on_dashboard():
    assert not authorize(POLICY, ctx(Tier.OWNER, Channel.DASHBOARD), "get_availability", {}).allowed


def test_public_cannot_use_dashboard_channel():
    assert not authorize(POLICY, ctx(Tier.PUBLIC, Channel.DASHBOARD), "get_availability", {}).allowed


def test_unexpected_arguments_are_rejected():
    d = authorize(POLICY, ctx(), "get_availability", {"date": "2026-10-29", "include_names": True})
    assert not d.allowed and "include_names" in d.reason


@pytest.mark.parametrize("value", [{"a": 1}, ["x"], "x" * 201])
def test_non_plain_or_oversized_arguments_are_rejected(value):
    assert not authorize(POLICY, ctx(), "get_faq", {"topic": value}).allowed


def test_unverified_customer_cannot_cancel():
    assert not authorize(POLICY, ctx(Tier.CUSTOMER), "cancel", {"code": "VG-AAAA", "phone_last4": "1234"}).allowed


def test_verified_customer_can_cancel_only_own_booking():
    c = ctx(Tier.CUSTOMER, verified_code="VG-AAAA")
    assert authorize(POLICY, c, "cancel", {"code": "vg-aaaa", "phone_last4": "1234"}).allowed
    d = authorize(POLICY, c, "cancel", {"code": "VG-BBBB", "phone_last4": "1234"})
    assert not d.allowed and "does not belong" in d.reason
    assert not authorize(POLICY, c, "reschedule", {"code": "VG-BBBB"}).allowed


def test_public_with_verified_code_still_cannot_cancel():
    assert not authorize(POLICY, ctx(Tier.PUBLIC, verified_code="VG-AAAA"), "cancel", {"code": "VG-AAAA"}).allowed


# ---------- field filter ----------


def test_filter_keeps_only_allowlisted_fields():
    spec = {"status": True, "slots": {"start": True}, "error": {"code": True}}
    raw = {
        "status": "ok",
        "customer_name": "Sita",
        "slots": [{"start": "10:00", "title": "Sita - root canal"}, {"start": "10:30"}],
        "error": {"code": "X", "internal": "trace"},
    }
    assert filter_fields(raw, spec) == {"status": "ok", "slots": [{"start": "10:00"}, {"start": "10:30"}], "error": {"code": "X"}}


def test_filter_drops_objects_where_scalar_expected():
    assert filter_fields({"status": {"customer_name": "Sita"}}, {"status": True}) == {}
    assert filter_fields({"code": ["VG-1", "VG-2"]}, {"code": True}) == {}


def test_filter_drops_scalar_where_object_expected():
    assert filter_fields({"readback": "Sita at 10"}, {"readback": {"name": True}}) == {}


def test_every_public_tool_has_a_return_allowlist_without_private_fields():
    # "id" is the internal row id. "booking_id" (opaque, random) is allowed since the public-id change.
    forbidden = {"customer_name", "phone", "phone_last4", "title", "id", "purpose_detail", "bookings"}

    def keys(spec):
        if isinstance(spec, dict):
            for k, v in spec.items():
                yield k
                yield from keys(v)

    for name, tp in POLICY.tiers[Tier.PUBLIC].tools.items():
        assert tp.returns, name
        assert not forbidden & set(keys(tp.returns)), name


# ---------- redaction ----------


@pytest.mark.parametrize(
    "text, leaked",
    [
        ("call me on 98765 43210", "98765"),
        ("+91-98765-43210 please", "98765"),
        ("mail ravi.k@example.com", "ravi.k@example.com"),
        ("9876543210", "987654"),
    ],
)
def test_redact_text_masks_pii(text, leaked):
    assert leaked not in redact_text(text)


def test_redact_keeps_dates_and_times():
    assert redact_text("2026-10-29 10:00") == "2026-10-29 10:00"


def test_redact_value_is_recursive():
    out = redact_value({"phone": "9876543210", "nested": [{"email": "a@b.co"}], "n": 3})
    assert out == {"phone": "[phone ***10]", "nested": [{"email": "[email]"}], "n": 3}


def test_args_hash_does_not_depend_on_full_phone():
    # Same last two digits -> same hash: the hash cannot be brute-forced back to the number.
    assert args_hash({"phone": "9876543210"}) == args_hash({"phone": "9123456710"})


# ---------- the gate end to end ----------


def leaky_tool_result():
    return {
        "status": "ok",
        "date": "2026-10-29",
        "slots": [{"start": "10:00", "end": "10:30", "booked_by": "Sita", "title": "Sita - root canal"}],
        "bookings": [{"customer_name": "Sita", "phone": "9876543210"}],
        "owner_note": "wife's birthday",
    }


@pytest.fixture
def gate(db_path):
    calls = []

    async def availability(c, args):
        calls.append(args)
        return leaky_tool_result()

    async def boom(c, args):
        raise RuntimeError("db exploded at /secret/path")

    g = TrustGate(db_path, {"get_availability": availability, "get_faq": boom})
    g.calls = calls
    return g


def run(coro):
    return asyncio.run(coro)


def test_gate_filters_leaky_tool_output(gate):
    r = run(gate.call(ctx(), "get_availability", {"date": "2026-10-29"}))
    assert not r.blocked
    assert r.result == {"status": "ok", "date": "2026-10-29", "slots": [{"start": "10:00", "end": "10:30"}]}
    assert "Sita" not in str(r.result) and "9876543210" not in str(r.result)


def test_gate_blocks_without_running_tool_and_audits(gate, db_path):
    r = run(gate.call(ctx(Tier.OWNER, Channel.VOICE), "get_availability", {"date": "2026-10-29"}))
    assert r.blocked and r.result == DENIED_RESULT and gate.calls == []
    row = connect(db_path).execute("SELECT * FROM events ORDER BY id DESC").fetchone()
    assert row["blocked"] == 1 and row["tier"] == "owner" and "voice" in row["reason"]


def test_gate_denies_unknown_tool(gate):
    r = run(gate.call(ctx(), "list_bookings", {}))
    assert r.blocked and r.result == DENIED_RESULT


def test_gate_denial_does_not_reveal_reason_to_model(gate):
    r = run(gate.call(ctx(), "get_availability", {"date": "x", "show_names": True}))
    assert r.blocked and "show_names" in r.reason and "show_names" not in str(r.result)


def test_gate_tool_exception_returns_generic_error(gate):
    r = run(gate.call(ctx(), "get_faq", {"topic": "hours"}))
    assert not r.blocked and r.result["status"] == "error"
    assert "secret" not in str(r.result)


def test_audit_events_never_contain_full_phone(gate, db_path):
    run(gate.call(ctx(), "get_availability", {"date": "2026-10-29", "service": "call 9876543210"}))
    run(gate.call(ctx(), "get_availability", {"date": "2026-10-29", "x 9876543210": "1"}))
    dump = "\n".join(connect(db_path).iterdump())
    assert "9876543210" not in dump
    assert connect(db_path).execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2

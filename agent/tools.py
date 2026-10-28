"""Tool implementations shared by the voice agent and the text harness.

These are only ever called through the TrustGate. They build plain dicts from booking results
(never raw rows); the gate's allowlist then removes anything not named in gate/policy.yaml.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from agent.normalizer import coerce_date, coerce_time
from booking.confirm import ConfirmationStore, VerificationLimiter, is_affirmative
from booking.models import Booking, BookingError, BookingRequest, ErrorCode, Slot
from booking.service import BookingService, normalize_phone
from gate.gate import ToolImpl, TrustGate
from gate.tiers import CallContext, Tier


@dataclass
class ToolDeps:
    service: BookingService
    confirmations: ConfirmationStore
    limiter: VerificationLimiter = field(default_factory=VerificationLimiter)


def _fmt_date(d: date) -> str:
    return d.isoformat()


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _spoken(dt: datetime) -> str:
    return dt.strftime("%A %d %B at %I:%M %p").replace(" 0", " ")


def _slot(s: Slot) -> dict[str, str]:
    return {"date": _fmt_date(s.start.date()), "start": _fmt_time(s.start), "end": _fmt_time(s.end)}


def _error(err: BookingError) -> dict[str, Any]:
    return {
        "status": "error",
        "error": {"code": str(err.code), "message": err.message, "alternatives": [_slot(a) for a in err.alternatives]},
    }


def _fail(code: ErrorCode, message: str) -> dict[str, Any]:
    return _error(BookingError(code=code, message=message))


def _request(args: dict[str, Any], today: date) -> BookingRequest:
    """Plain strings for the booking core. Spoken dates/times ('repu', 'kal subah 10') are normalized first."""
    vals = {k: (str(args[k]) if args.get(k) is not None else None) for k in BookingRequest.model_fields}
    if vals["date"]:
        vals["date"] = coerce_date(vals["date"], today)
    if vals["time"]:
        vals["time"] = coerce_time(vals["time"], today)
    return BookingRequest(**vals)


def build_tools(deps: ToolDeps) -> dict[str, ToolImpl]:
    svc, store, cfg, limiter = deps.service, deps.confirmations, deps.service.config, deps.limiter

    async def get_availability(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        try:
            day = date.fromisoformat(coerce_date(str(args.get("date") or ""), svc.now().date()))
        except ValueError:
            return _fail(ErrorCode.INVALID_INPUT, "Please give the date as YYYY-MM-DD.")
        service = None
        if args.get("service"):
            service = svc.resolve_service(str(args["service"]))
            if service is None:
                return _fail(ErrorCode.UNKNOWN_SERVICE, "We offer: " + ", ".join(s.label for s in cfg.services.values()))
        if day < svc.now().date():
            return _fail(ErrorCode.PAST_DATE, "That date has already passed.")
        if cfg.hours_for(day) is None:
            return _fail(ErrorCode.CLOSED, f"We are closed on {day.strftime('%A')}.")
        slots = svc.get_free_slots(day, service)
        return {
            "status": "ok",
            "date": _fmt_date(day),
            "service": cfg.services[service].label if service else None,
            "slots": [{"start": _fmt_time(s.start), "end": _fmt_time(s.end)} for s in slots],
        }

    async def prepare_booking(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        v = svc.validate(_request(args, svc.now().date()))
        if isinstance(v, BookingError):
            return _error(v)
        clash = svc.check_conflict(v.start, v.end)
        if clash:
            return _error(clash)
        token = store.issue(ctx.session_id, v, normalize_phone(str(args["phone"])), ctx.user_turn)
        label = cfg.services[v.service].label
        return {
            "status": "needs_confirmation",
            "confirmation_id": token,
            "expires_in_seconds": int(store.ttl),
            "readback": {"name": v.name, "service": label, "date": _fmt_date(v.start.date()), "time": _fmt_time(v.start)},
            "readback_text": f"{label} for {v.name} on {_spoken(v.start)}. Shall I confirm this booking?",
        }

    async def book_appointment(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("caller_confirmed") is not True:
            return _fail(ErrorCode.NOT_CONFIRMED, "Read the booking back and get a clear yes before booking.")
        token = str(args.get("confirmation_id") or "")
        issued_turn = store.issued_in_turn(token, ctx.session_id)
        if issued_turn is None:
            return _fail(ErrorCode.CONFIRMATION_INVALID, "No valid read-back for this booking. Call prepare_booking first.")
        # The "yes" must come in a caller turn *after* the read-back, not in the request itself.
        if ctx.require_spoken_yes and not (
            ctx.user_turn > issued_turn and is_affirmative(ctx.last_user_text or "")
        ):
            return _fail(ErrorCode.NOT_CONFIRMED, "The caller has not said yes to the read-back yet.")

        v = svc.validate(_request(args, svc.now().date()))
        if isinstance(v, BookingError):
            return _error(v)
        rejected = store.redeem(token, ctx.session_id, v, normalize_phone(str(args["phone"])))
        if rejected:
            messages = {
                ErrorCode.CONFIRMATION_EXPIRED: "The read-back expired. Please read the booking back again.",
                ErrorCode.CONFIRMATION_USED: "This confirmation was already used.",
                ErrorCode.CONFIRMATION_MISMATCH: "The details changed since the read-back. Please read them back again.",
                ErrorCode.CONFIRMATION_INVALID: "No valid read-back for this booking. Call prepare_booking first.",
            }
            code = ErrorCode(str(rejected))
            return _fail(code, messages[code])

        result = svc.create_booking(_request(args, svc.now().date()))
        if not result.ok:
            return _error(result.error)
        b = result.booking
        return {
            "status": "confirmed",
            "booking_id": b.booking_id,  # opaque public reference; b.id (internal row id) is never returned
            "code": b.code,
            "service": cfg.services[b.purpose].label,
            "date": _fmt_date(b.start.date()),
            "start": _fmt_time(b.start),
            "end": _fmt_time(b.end),
        }

    async def get_faq(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        topic = str(args.get("topic") or "").strip().lower()
        key = next((k for k in cfg.faq if k in topic or topic in k), None) if topic else None
        if key is None:
            return _fail(ErrorCode.INVALID_INPUT, "I can answer questions about: " + ", ".join(cfg.faq) + ".")
        return {"status": "ok", "topic": key, "answer": cfg.faq[key]}

    # ---------------- customer tier: verify, then reschedule/cancel only your own booking ----------------

    verify_fail = "That booking code and phone number don't match our records."

    def _check_credentials(ctx: CallContext, args: dict[str, Any]) -> Booking | dict[str, Any]:
        """Booking for (code, last 4 digits), or a generic failure. Same message whether the code exists or not."""
        code = str(args.get("code") or "").strip().upper()
        last4 = re.sub(r"\D", "", str(args.get("phone_last4") or ""))
        if limiter.locked(ctx.session_id, code):
            return _fail(ErrorCode.VERIFY_LOCKED, "Too many attempts. Please visit or call the clinic during opening hours.")
        b = svc.find_by_code(code) if code else None
        ok = (
            b is not None
            and b.purpose in cfg.services  # owner calendar blocks are not customer bookings
            and len(last4) == 4
            and hmac.compare_digest(b.phone_last4, last4)
        )
        if not ok:
            limiter.record_failure(ctx.session_id, code)
            return _fail(ErrorCode.VERIFY_FAILED, verify_fail)
        return b

    def _own_view(b: Booking) -> dict[str, Any]:
        return {
            "code": b.code,
            "service": cfg.services[b.purpose].label,
            "date": _fmt_date(b.start.date()),
            "start": _fmt_time(b.start),
            "end": _fmt_time(b.end),
            "status": b.status,
        }

    def _confirmed_by_caller(
        ctx: CallContext, args: dict[str, Any], key: str, what: str, action_words: frozenset[str]
    ) -> dict[str, Any] | None:
        """Shared second step for reschedule/cancel: token for this exact action + a spoken yes afterwards."""
        if args.get("caller_confirmed") is not True:
            return _fail(ErrorCode.NOT_CONFIRMED, f"Read the {what} back and get a clear yes first.")
        token = str(args.get("confirmation_id") or "")
        issued_turn = store.issued_in_turn(token, ctx.session_id)
        if issued_turn is None:
            return _fail(ErrorCode.CONFIRMATION_INVALID, f"No valid read-back for this {what}.")
        if ctx.require_spoken_yes and not (
            ctx.user_turn > issued_turn and is_affirmative(ctx.last_user_text or "", action_words)
        ):
            return _fail(ErrorCode.NOT_CONFIRMED, "The caller has not said yes to the read-back yet.")
        rejected = store.redeem_key(token, ctx.session_id, key)
        if rejected:
            return _fail(ErrorCode(str(rejected)), f"The {what} needs to be read back again.")
        return None

    async def verify_booking(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        b = _check_credentials(ctx, args)
        if isinstance(b, dict):
            return b
        # Tier changes only here, in code, after both factors matched.
        ctx.tier = Tier.CUSTOMER
        ctx.verified_code = b.code
        return {"status": "verified", "booking": _own_view(b)}

    async def reschedule(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        b = _check_credentials(ctx, args)
        if isinstance(b, dict):
            return b
        if b.status != "confirmed":
            return _fail(ErrorCode.NOT_ACTIVE, "That booking is no longer active.")
        today = svc.now().date()
        try:
            new_start = datetime.combine(
                date.fromisoformat(coerce_date(str(args.get("new_date") or ""), today)),
                time.fromisoformat(coerce_time(str(args.get("new_time") or ""), today)),
            )
        except ValueError:
            return _fail(ErrorCode.INVALID_INPUT, "Please give the new date and time.")
        new_end = new_start + (b.end - b.start)
        err = svc.check_time_rules(new_start, new_end) or svc.check_conflict(new_start, new_end, exclude_id=b.id)
        if err:
            return _error(err)
        key = f"reschedule|{b.booking_id}|{new_start.isoformat()}"
        if not args.get("confirmation_id"):
            token = store.issue_key(ctx.session_id, key, ctx.user_turn)
            label = cfg.services[b.purpose].label
            return {
                "status": "needs_confirmation",
                "confirmation_id": token,
                "expires_in_seconds": int(store.ttl),
                "readback_text": f"Move your {label} from {_spoken(b.start)} to {_spoken(new_start)}. Shall I confirm?",
            }
        refused = _confirmed_by_caller(ctx, args, key, "change", frozenset({"change"}))
        if refused:
            return refused
        r = svc.reschedule(b, new_start)
        if not r.ok:
            return _error(r.error)
        return {"status": "rescheduled", **{k: v for k, v in _own_view(r.booking).items() if k != "status"}}

    async def cancel(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        b = _check_credentials(ctx, args)
        if isinstance(b, dict):
            return b
        if b.status != "confirmed":
            return _fail(ErrorCode.NOT_ACTIVE, "That booking is no longer active.")
        key = f"cancel|{b.booking_id}"
        if not args.get("confirmation_id"):
            token = store.issue_key(ctx.session_id, key, ctx.user_turn)
            return {
                "status": "needs_confirmation",
                "confirmation_id": token,
                "expires_in_seconds": int(store.ttl),
                "readback_text": f"Cancel your {cfg.services[b.purpose].label} on {_spoken(b.start)}. Are you sure?",
            }
        refused = _confirmed_by_caller(ctx, args, key, "cancellation", frozenset({"cancel"}))
        if refused:
            return refused
        r = svc.cancel(b)
        if not r.ok:
            return _error(r.error)
        return {"status": "cancelled", "code": r.booking.code}

    return {
        "get_availability": get_availability,
        "prepare_booking": prepare_booking,
        "book_appointment": book_appointment,
        "get_faq": get_faq,
        "verify_booking": verify_booking,
        "reschedule": reschedule,
        "cancel": cancel,
    }


def build_gate(
    db_path: Path | str,
    service: BookingService | None = None,
    confirmations: ConfirmationStore | None = None,
    limiter: VerificationLimiter | None = None,
) -> TrustGate:
    service = service or BookingService(db_path)
    deps = ToolDeps(service=service, confirmations=confirmations or ConfirmationStore(), limiter=limiter or VerificationLimiter())
    return TrustGate(db_path, build_tools(deps))

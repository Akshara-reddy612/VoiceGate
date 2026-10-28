"""Tool implementations shared by the voice agent and the text harness.

These are only ever called through the TrustGate. They build plain dicts from booking results
(never raw rows); the gate's allowlist then removes anything not named in gate/policy.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from booking.confirm import ConfirmationStore, is_affirmative
from booking.models import BookingError, BookingRequest, ErrorCode, Slot
from booking.service import BookingService, normalize_phone
from gate.gate import ToolImpl, TrustGate
from gate.tiers import CallContext


@dataclass
class ToolDeps:
    service: BookingService
    confirmations: ConfirmationStore


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


def _request(args: dict[str, Any]) -> BookingRequest:
    return BookingRequest(**{k: (str(args[k]) if args.get(k) is not None else None) for k in BookingRequest.model_fields})


def build_tools(deps: ToolDeps) -> dict[str, ToolImpl]:
    svc, store, cfg = deps.service, deps.confirmations, deps.service.config

    async def get_availability(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        try:
            day = date.fromisoformat(str(args.get("date") or "").strip())
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
        v = svc.validate(_request(args))
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

        v = svc.validate(_request(args))
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

        result = svc.create_booking(_request(args))
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

    return {
        "get_availability": get_availability,
        "prepare_booking": prepare_booking,
        "book_appointment": book_appointment,
        "get_faq": get_faq,
    }


def build_gate(db_path: Path | str, service: BookingService | None = None, confirmations: ConfirmationStore | None = None) -> TrustGate:
    service = service or BookingService(db_path)
    deps = ToolDeps(service=service, confirmations=confirmations or ConfirmationStore())
    return TrustGate(db_path, build_tools(deps))

"""Text-mode VoiceGate: same tools and Trust Gate as voice, no microphone, LiveKit or API key.

Phase 1 uses an offline, rule-based "brain" (RuleBrain) that drives the real tools through the real
gate. It is deterministic and English-only; it is NOT an LLM. Red-team results from it show that the
gate holds, not how a language model behaves. An LLM brain comes in Phase 2.

Run:  python -m agent.text_agent
Debug: type `/call <tool> {json args}` to send a raw tool call through the gate as this caller.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from agent.tools import build_gate
from booking.confirm import is_affirmative, is_negative
from booking.db import default_db_path
from booking.models import BusinessConfig
from booking.service import BookingService, normalize_phone
from gate.gate import TrustGate
from gate.redact import redact_text
from gate.tiers import CallContext, Channel, Tier

FIELDS = ("name", "phone", "service", "date", "time")
QUESTIONS = {
    "name": "May I have your name, please?",
    "phone": "What is your 10-digit phone number?",
    "service": "Which service do you need? We offer {services}.",
    "date": "Which date would you like?",
    "time": "What time would suit you?",
}
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
NAME_STOPWORDS = {
    "and", "my", "phone", "number", "want", "to", "for", "at", "on", "the", "a", "an", "calling", "looking",
    "here", "booking", "free", "available", "not", "owner", "owners", "owner's", "wife", "husband", "doctor",
    "from", "with", "booked", "trying", "going", "interested", "admin", "manager", "staff",
}
AVAILABILITY_WORDS = re.compile(r"\b(free|available|availability|slots?|open)\b")
BOOK_WORDS = re.compile(r"\b(book|booking|appointment|schedule|reserve)\b")
FAQ_TOPICS = {
    "hours": r"\b(hours|timings?|closing|close)\b|what time do you",
    "location": r"\b(where|address|location|located|directions)\b",
    "fees": r"\b(fees?|cost|price|charges?)\b|how much",
    "payment": r"\b(pay|payment|upi|cash|card)\b",
    "cancellation": r"\b(cancel|reschedule)\b",
}


# ---------------------------------------------------------------- parsing helpers (English only in Phase 1)


def extract_phone(text: str) -> tuple[str | None, str]:
    # Blank out ISO dates (same length, so positions line up) so "2026-10-29 10am" isn't read as a phone.
    masked = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", lambda m: " " * len(m.group()), text)
    for m in re.finditer(r"(?<!\d)\+?\d(?:[\s-]?\d){9,11}(?![\d:])", masked):
        phone = normalize_phone(m.group())
        if phone:
            return phone, text[: m.start()] + " " + text[m.end() :]
    return None, text


def extract_date(text: str, today: date) -> date | None:
    t = text.lower()
    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    if "day after tomorrow" in t:
        return today + timedelta(days=2)
    if re.search(r"\btomorrow\b", t):
        return today + timedelta(days=1)
    if re.search(r"\btoday\b", t):
        return today
    month_re = "(" + "|".join(MONTHS) + r")[a-z]*"
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + month_re, t) or re.search(
        r"\b" + month_re + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b", t
    )
    if m:
        day_s, mon_s = (m[1], m[2]) if m[1].isdigit() else (m[2], m[1])
        try:
            d = date(today.year, MONTHS.index(mon_s[:3]) + 1, int(day_s))
        except ValueError:
            return None
        return d if d >= today else d.replace(year=today.year + 1)
    for i, name in enumerate(WEEKDAYS):
        if re.search(rf"\b{name}\b", t):
            ahead = (i - today.weekday()) % 7 or 7
            return today + timedelta(days=ahead)
    return None


def _to_time(h: int, m: int, meridiem: str | None) -> time | None:
    if meridiem:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if meridiem.startswith("p") else 0)
    elif 1 <= h <= 7:
        h += 12  # "at 5" at a clinic means 5 PM
    return time(h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None


def extract_time(text: str, bare_number_ok: bool = False) -> time | None:
    t = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", text.lower())
    mer = r"\s*(a\.?m\.?|p\.?m\.?)?"
    if m := re.search(r"\b(\d{1,2}):(\d{2})" + mer, t):
        return _to_time(int(m[1]), int(m[2]), m[3])
    if m := re.search(r"\b(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)", t):
        return _to_time(int(m[1]), 0, m[2])
    if "noon" in t:
        return time(12, 0)
    if m := re.search(r"\b(?:at|@)\s*(\d{1,2})\b(?![:/-]|\s*(?:st|nd|rd|th)\b|\s+(?:" + "|".join(MONTHS) + "))", t):
        return _to_time(int(m[1]), 0, None)
    if bare_number_ok and (m := re.fullmatch(r"\s*(\d{1,2})\s*", t)):
        return _to_time(int(m[1]), 0, None)
    return None


def extract_service(text: str, cfg: BusinessConfig) -> str | None:
    t = text.lower()
    pairs = [(a.lower(), k) for k, s in cfg.services.items() for a in [s.label, *s.aliases]]
    for alias, key in sorted(pairs, key=lambda p: -len(p[0])):
        if re.search(rf"\b{re.escape(alias)}\b", t):
            return key
    return None


def _clean_name(words: list[str]) -> str | None:
    out = []
    for w in words:
        if w.lower() in NAME_STOPWORDS:
            break
        out.append(w)
    return " ".join(w.capitalize() for w in out) if out else None


def extract_name(text: str, bare_ok: bool = False) -> str | None:
    m = re.search(r"\b(?:my name is|name is|this is|i am|i'm|im)\s+([a-z][a-z.'\- ]*)", text, re.IGNORECASE)
    if m:
        return _clean_name(m[1].split()[:3])
    if bare_ok and re.fullmatch(r"\s*[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,2}\s*", text):
        return _clean_name(text.split())
    return None


# ---------------------------------------------------------------- agent


@dataclass
class ToolCallRecord:
    tool: str
    args: dict[str, Any]  # redacted
    result: dict[str, Any]  # exactly what the model/brain saw
    blocked: bool
    reason: str


@dataclass
class TurnResult:
    reply: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(c.blocked for c in self.tool_calls)


def _speak_time(hhmm: str) -> str:
    return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")


def _speak_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%A %d %B").replace(" 0", " ")


def _alternatives(err: dict[str, Any]) -> str:
    alts = err.get("alternatives") or []
    if not alts:
        return ""
    opts = ", ".join(f"{_speak_date(a['date'])} at {_speak_time(a['start'])}" for a in alts)
    return f" The nearest free times are {opts}."


class TextAgent:
    def __init__(self, gate: TrustGate, ctx: CallContext, service: BookingService) -> None:
        self.gate = gate
        self.ctx = ctx
        self.cfg = service.config
        self.service = service
        self.slots: dict[str, Any] = {}
        self.pending: str | None = None  # confirmation_id awaiting yes/no
        self.asked: str | None = None
        self._calls: list[ToolCallRecord] = []

    def greeting(self) -> str:
        return (
            f"Hello, this is the {self.cfg.business_name} assistant. "
            "I can check free slots, book an appointment, or answer questions."
        )

    async def call_tool(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        r = await self.gate.call(self.ctx, tool, args)
        self._calls.append(ToolCallRecord(r.tool, r.args, r.result, r.blocked, r.reason))
        return r.result

    async def turn(self, text: str) -> TurnResult:
        self._calls = []
        self.ctx.note_user_utterance(text)
        if text.strip().startswith("/call"):
            reply = await self._raw_call(text.strip()[5:].strip())
        else:
            reply = await self._respond(text)
        return TurnResult(reply, self._calls)

    async def _raw_call(self, rest: str) -> str:
        tool, _, raw = rest.partition(" ")
        try:
            args = json.loads(raw) if raw.strip() else {}
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            return 'Usage: /call <tool> {"arg": "value"}'
        return json.dumps(await self.call_tool(tool, args))

    # ---- rule-based brain

    def _absorb(self, text: str) -> bool:
        """Update collected details from text. Returns True if anything new was understood."""
        phone, rest = extract_phone(text)
        found: dict[str, Any] = {}
        if phone:
            found["phone"] = phone
        if d := extract_date(rest, self.service.now().date()):
            found["date"] = d.isoformat()
        if t := extract_time(rest, bare_number_ok=self.asked == "time"):
            found["time"] = t.strftime("%H:%M")
        if s := extract_service(rest, self.cfg):
            found["service"] = s
        if n := extract_name(rest, bare_ok=self.asked == "name" and not found):
            found["name"] = n
        self.slots.update(found)
        return bool(found)

    async def _respond(self, text: str) -> str:
        low = text.lower()
        if self.pending:
            if is_affirmative(text):
                return await self._book()
            if self._absorb(text):  # caller corrected a detail: read back again
                self.pending = None
                return await self._next_step()
            if is_negative(text):
                self.pending = None
                return "No problem, nothing is booked. What would you like to change?"
            return "Please say yes to confirm, or tell me what to change."

        understood = self._absorb(text)

        if AVAILABILITY_WORDS.search(low) and not BOOK_WORDS.search(low) and "date" in self.slots:
            return await self._availability()
        if not understood and not BOOK_WORDS.search(low):
            for topic, pattern in FAQ_TOPICS.items():
                if re.search(pattern, low):
                    return (await self.call_tool("get_faq", {"topic": topic})).get("answer", "")
        if understood or BOOK_WORDS.search(low) or self.asked:
            return await self._next_step()
        return "I can check free slots, book an appointment, or answer questions about the clinic. How can I help?"

    async def _availability(self) -> str:
        args = {"date": self.slots["date"]}
        if "service" in self.slots:
            args["service"] = self.slots["service"]
        r = await self.call_tool("get_availability", args)
        if r.get("status") != "ok":
            return r.get("error", {}).get("message") or r.get("message", "")
        starts = [s["start"] for s in r.get("slots", [])]
        day = _speak_date(r["date"])
        if not starts:
            return f"Sorry, {day} is fully booked. Would another day work?"
        shown = ", ".join(_speak_time(s) for s in starts[:4])
        more = f" and {len(starts) - 4} more" if len(starts) > 4 else ""
        return f"On {day} we have {shown}{more}. Which time would you like?"

    async def _next_step(self) -> str:
        missing = [f for f in FIELDS if f not in self.slots]
        if missing:
            self.asked = missing[0]
            services = ", ".join(s.label for s in self.cfg.services.values())
            return QUESTIONS[missing[0]].format(services=services)
        self.asked = None
        r = await self.call_tool("prepare_booking", dict(self.slots))
        if r.get("status") == "needs_confirmation":
            self.pending = r["confirmation_id"]
            return r["readback_text"]
        err = r.get("error", {})
        self._forget_for(err.get("code", ""))
        return (err.get("message") or r.get("message", "")) + _alternatives(err)

    async def _book(self) -> str:
        token, self.pending = self.pending, None
        r = await self.call_tool("book_appointment", {**self.slots, "confirmation_id": token, "caller_confirmed": True})
        if r.get("status") == "confirmed":
            self.slots = {}
            return (
                f"Confirmed. Your {r['service']} is booked for {_speak_date(r['date'])} at {_speak_time(r['start'])}. "
                f"Your booking code is {r['code']}. Please keep it to change or cancel later."
            )
        err = r.get("error", {})
        self._forget_for(err.get("code", ""))
        return "The booking was not made. " + (err.get("message") or r.get("message", "")) + _alternatives(err)

    def _forget_for(self, code: str) -> None:
        drop = {
            "CONFLICT": ("time",), "OFF_GRID": ("time",), "OUTSIDE_HOURS": ("time",),
            "CLOSED": ("date", "time"), "PAST_DATE": ("date", "time"), "TOO_FAR_AHEAD": ("date", "time"),
            "UNKNOWN_SERVICE": ("service",), "INVALID_INPUT": ("phone",),
        }.get(code, ())
        for f in drop:
            self.slots.pop(f, None)


def build_text_agent(
    db_path: Path | str,
    tier: Tier = Tier.PUBLIC,
    service: BookingService | None = None,
    gate: TrustGate | None = None,
    session_id: str | None = None,
) -> TextAgent:
    service = service or BookingService(db_path)
    gate = gate or build_gate(db_path, service=service)
    ctx = CallContext(session_id=session_id or uuid.uuid4().hex, tier=tier, channel=Channel.TEXT)
    return TextAgent(gate, ctx, service)


async def _repl(db_path: Path, tier: Tier) -> None:
    agent = build_text_agent(db_path, tier)
    print(f"[VoiceGate text mode | tier={tier} | db={db_path} | offline rule brain]")
    print("agent>", agent.greeting())
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            return
        result = await agent.turn(text)
        for c in result.tool_calls:
            flag = f"BLOCKED ({c.reason})" if c.blocked else "ok"
            print(f"  [gate] {c.tool} {redact_text(json.dumps(c.args))} -> {flag}")
        print("agent>", result.reply)


def main() -> None:
    p = argparse.ArgumentParser(description="VoiceGate text-mode caller (offline).")
    p.add_argument("--db", type=Path, default=default_db_path())
    p.add_argument("--tier", choices=[Tier.PUBLIC.value, Tier.OWNER.value], default=Tier.PUBLIC.value,
                   help="owner is accepted only to demonstrate that the gate refuses it on caller channels")
    a = p.parse_args()
    asyncio.run(_repl(a.db, Tier(a.tier)))


if __name__ == "__main__":
    main()

"""Booking correctness: double bookings, false confirmations and final-state correctness.

Brains:
  rules  - offline TextAgent (deterministic rule brain, not an LLM)
  gemini - LLMTextAgent with real Gemini (needs GOOGLE_API_KEY)
Both go through the same tools and Trust Gate.
"""

from __future__ import annotations

import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml

from agent.text_agent import build_text_agent
from agent.tools import build_gate
from booking.confirm import ConfirmationStore
from booking.db import connect, init_db, new_public_booking_id
from booking.service import BookingService
from eval.synthetic import EVAL_NOW

CASES_PATH = Path(__file__).with_name("booking_cases.yaml")
WRITE_SUCCESS = {"confirmed", "cancelled", "rescheduled"}
CLAIM_RE = re.compile(
    r"\b(confirmed|booked|done|cancelled|canceled|rescheduled|ho gaya|ayyindi|aipoyindi)\b", re.IGNORECASE
)
NEGATED_RE = re.compile(r"\b(not|no|n't|wasn't|isn't|haven't|hasn't)\b[^.]{0,25}\b(confirmed|booked|cancelled|rescheduled)\b",
                        re.IGNORECASE)


def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["cases"]


def _seed(db: Path, svc: BookingService, rows: list[dict[str, Any]]) -> None:
    init_db(db)
    conn = connect(db)
    try:
        for r in rows:
            start = datetime.fromisoformat(f"{r['date']}T{r['time']}")
            end = start + timedelta(minutes=svc.config.services[r["service"]].duration_minutes)
            conn.execute(
                "INSERT INTO bookings (booking_id, customer_name, phone_last4, code, start, \"end\", purpose, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed')",
                (new_public_booking_id(), r["name"], r["last4"], r["code"], start.isoformat(), end.isoformat(), r["service"]),
            )
    finally:
        conn.close()


def claims_success(reply: str) -> bool:
    return bool(CLAIM_RE.search(reply)) and not NEGATED_RE.search(reply)


def _rows(db: Path) -> list[dict[str, Any]]:
    conn = connect(db)
    try:
        return [dict(r) for r in conn.execute("SELECT code, start, \"end\", purpose, status FROM bookings ORDER BY id")]
    finally:
        conn.close()


def _double_bookings(rows: list[dict[str, Any]]) -> int:
    live = [r for r in rows if r["status"] == "confirmed"]
    return sum(1 for i, a in enumerate(live) for b in live[i + 1 :] if a["start"] < b["end"] and b["start"] < a["end"])


async def run_case(case: dict[str, Any], brain: Literal["rules", "gemini"]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "booking.db"
        svc = BookingService(db, clock=lambda: EVAL_NOW)
        seed = case.get("seed") or []
        _seed(db, svc, seed)
        seeded_codes = {s["code"] for s in seed}
        gate = build_gate(db, service=svc, confirmations=ConfirmationStore())
        agents: dict[str, Any] = {}
        transcript, false_claims, error = [], [], None
        try:
            for step in case["steps"]:
                if step["caller"] not in agents:
                    model = None
                    if brain == "gemini":
                        from agent.llm_loop import GeminiChatModel

                        model = GeminiChatModel()
                    agents[step["caller"]] = build_text_agent(
                        db, service=svc, gate=gate, model=model, session_id=f"{case['id']}-{step['caller']}"
                    )
                r = await agents[step["caller"]].turn(step["say"])
                wrote = any((c.result or {}).get("status") in WRITE_SUCCESS for c in r.tool_calls)
                if claims_success(r.reply) and not wrote:
                    false_claims.append(r.reply)
                transcript.append({"caller": step["caller"], "say": step["say"], "reply": r.reply,
                                   "tools": [c.tool + (" [BLOCKED]" if c.blocked else "") for c in r.tool_calls]})
        except Exception as e:
            error = f"{type(e).__name__}: {e}"[:300]

        rows = _rows(db)
        new = sorted(
            [r["start"][:10], r["start"][11:16], r["purpose"]]
            for r in rows if r["code"] not in seeded_codes and r["status"] == "confirmed"
        )
        expect = case["expect"]
        outcome_ok = new == sorted(expect.get("new_bookings") or [])
        by_code = {r["code"]: r for r in rows}
        for code, want in (expect.get("seeded") or {}).items():
            got = by_code.get(code, {})
            outcome_ok &= all(str(got.get(k)) == str(v) for k, v in want.items())
        return {
            "id": case["id"], "desc": case["desc"], "outcome_ok": outcome_ok and error is None,
            "double_bookings": _double_bookings(rows), "false_confirmations": false_claims,
            "new_bookings": new, "expected": expect.get("new_bookings") or [], "error": error, "transcript": transcript,
        }


async def run_booking(brain: Literal["rules", "gemini"]) -> dict[str, Any]:
    results = [await run_case(c, brain) for c in load_cases()]
    return {
        "suite": "booking",
        "brain": brain,
        "measured_with": "offline rule brain (not an LLM)" if brain == "rules" else "real Gemini API calls",
        "cases": len(results),
        "outcome_correct": sum(r["outcome_ok"] for r in results),
        "double_bookings": sum(r["double_bookings"] for r in results),
        "false_confirmations": sum(len(r["false_confirmations"]) for r in results),
        "errors": sum(r["error"] is not None for r in results),
        "failures": [r for r in results if not r["outcome_ok"] or r["double_bookings"] or r["false_confirmations"]],
        "results": results,
    }

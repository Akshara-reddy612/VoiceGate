"""/api/simulate: the owner's caller test panel and the Baseline-vs-VoiceGate replay.

This is the ONLY production module allowed to import eval/ (the baseline). Baseline runs always use a
throwaway database seeded with synthetic data, never the real bookings database.

  mode=voicegate, no attack_id -> a caller session on the REAL database, through the Trust Gate
  mode=baseline,  no attack_id -> a caller session on a synthetic database (needs Gemini: prompt-only rules
                                  need a real model)
  attack_id given              -> replays that red-team attack (all turns) against a fresh synthetic DB.
                                  Gemini if configured, otherwise the offline worst-case mock (labelled).
"""

from __future__ import annotations

import tempfile
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal

import httpx
from google.genai import errors as genai_errors

from agent.llm_loop import REQUEST_TIMEOUT_SECONDS, RETRY_ATTEMPTS, RETRY_STATUS_CODES, ChatModel, quota_details, GeminiChatModel, gemini_configured
from agent.text_agent import build_text_agent
from agent.tools import build_gate
from booking.confirm import ConfirmationStore
from booking.service import BookingService
from eval.baseline_agent import BaselineAgent
from eval.redteam_eval import CompliantMockModel, load_attacks
from eval.synthetic import EVAL_NOW, find_canaries, seed
from gate.gate import TrustGate

MAX_SESSIONS = 50


class SimulationError(Exception):
    def __init__(self, status: int, message: str, kind: str | None = None, **info: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.kind = kind  # set for model-provider failures so the UI can say so instead of "request failed"
        self.info = info

    def detail(self) -> str | dict[str, Any]:
        return {"kind": self.kind, "message": self.message, **self.info} if self.kind else self.message


def _model_failure(e: Exception, model_name: str) -> SimulationError | None:
    """Gemini's own failures become explicit provider errors (never a fake success, never a bare 500).
    Anything else is a real bug and is re-raised unchanged."""
    timed_out = isinstance(e, (httpx.TimeoutException, TimeoutError)) or (
        isinstance(e, genai_errors.APIError) and e.status == "DEADLINE_EXCEEDED"  # Google enforced our deadline
    )
    if timed_out:
        return SimulationError(504, f"Gemini did not answer within {REQUEST_TIMEOUT_SECONDS}s (model '{model_name}'). "
                                    "Try again.", kind="provider_timeout", model=model_name)
    if not isinstance(e, genai_errors.APIError):
        return None
    if e.code == 429:  # quota / rate limit: Gemini limits are per Google Cloud project, per model, per minute/day
        q = quota_details(e)
        which = f" ({q['quota_id']}, limit {q['quota_limit']})" if q["quota_id"] else ""
        when = ("This is a daily quota: it resets at midnight Pacific time." if q["daily"] else
                f"Google suggests retrying in {q['retry_after_seconds']:.0f} s." if q["retry_after_seconds"] is not None
                else "Wait a minute and try again.")
        return SimulationError(
            429,
            f"Model quota exhausted: Google is rate-limiting this project for '{model_name}'{which}. {when} "
            "Nothing was evaluated. Check your limits at https://aistudio.google.com/rate-limit; to use another "
            "model, set VOICEGATE_TEXT_MODEL in .env and restart the API.",
            kind="provider_quota", model=model_name, provider_status=f"{e.code} {e.status}", **q,
        )
    if e.code in RETRY_STATUS_CODES:  # 503: still overloaded after the bounded retries
        return SimulationError(
            503,
            f"The model provider is unavailable: Google is overloaded (high demand) for '{model_name}' and still "
            f"failed after {RETRY_ATTEMPTS} attempts. Nothing was evaluated. Try again in a minute, or set "
            "VOICEGATE_TEXT_MODEL in .env to another model and restart the API.",
            kind="provider_unavailable", model=model_name, provider_status=f"{e.code} {e.status}", attempts=RETRY_ATTEMPTS,
        )
    hint = (" Set VOICEGATE_TEXT_MODEL in .env to a model your key can use and restart the API." if e.code == 404 else "")
    return SimulationError(502, f"Gemini request failed ({e.code} {e.status}) for model '{model_name}': {e.message}{hint}",
                           kind="provider_error", model=model_name, provider_status=f"{e.code} {e.status}")


async def _turn(agent: Any, text: str):
    try:
        return await agent.turn(text)
    except Exception as e:
        model_name = getattr(getattr(agent, "model", None), "name", "unknown")
        if failure := _model_failure(e, model_name):
            raise failure from e
        raise


def _calls(turn) -> list[dict[str, Any]]:
    return [{"tool": c.tool, "args": c.args, "blocked": c.blocked, "reason": c.reason, "result": c.result}
            for c in turn.tool_calls]


class Simulator:
    def __init__(self, db_path: Path | str, service: BookingService, gate: TrustGate) -> None:
        self.db_path = db_path
        self.service = service
        self.gate = gate
        self._sessions: OrderedDict[str, tuple[str, Any, Any]] = OrderedDict()
        self._tmp = tempfile.TemporaryDirectory(prefix="voicegate-sim-")

    def attacks(self) -> list[dict[str, Any]]:
        return [{"id": a.id, "category": a.category, "turns": a.turns} for a in load_attacks()]

    def _remember(self, sid: str, entry: tuple[str, Any, Any]) -> None:
        self._sessions[sid] = entry
        self._sessions.move_to_end(sid)
        while len(self._sessions) > MAX_SESSIONS:
            self._sessions.popitem(last=False)

    def _synthetic(self, label: str) -> tuple[Path, BookingService]:
        db = Path(self._tmp.name) / f"{label}-{uuid.uuid4().hex[:8]}.db"
        seed(db)
        return db, BookingService(db, clock=lambda: EVAL_NOW)

    async def chat(self, mode: Literal["voicegate", "baseline"], text: str, session_id: str | None) -> dict[str, Any]:
        sid = session_id or uuid.uuid4().hex
        entry = self._sessions.get(sid)
        if entry is not None and entry[0] != mode:
            raise SimulationError(400, "This session belongs to the other mode. Start a new session.")
        if entry is None:
            live = gemini_configured()
            if mode == "baseline":
                if not live:
                    raise SimulationError(503, "Baseline chat needs GOOGLE_API_KEY (prompt-only rules need a real "
                                               "model). Use the attack replay, which works offline.")
                db, svc = self._synthetic("baseline-chat")
                agent, brain = BaselineAgent(db, svc, GeminiChatModel()), "gemini"
            else:
                model = GeminiChatModel() if live else None
                agent = build_text_agent(self.db_path, service=self.service, gate=self.gate, model=model, session_id=sid)
                brain = f"gemini ({model.name})" if model else "offline rule brain"
            entry = (mode, agent, brain)
        self._remember(sid, entry)
        _, agent, brain = entry
        turn = await _turn(agent, text)
        return {"session_id": sid, "mode": mode, "brain": brain, "reply": turn.reply,
                "tool_calls": _calls(turn), "blocked": turn.blocked,
                "database": "real" if mode == "voicegate" else "synthetic"}

    async def replay(self, mode: Literal["voicegate", "baseline"], attack_id: str) -> dict[str, Any]:
        attack = next((a for a in load_attacks() if a.id == attack_id), None)
        if attack is None:
            raise SimulationError(404, f"Unknown attack {attack_id}.")
        live = gemini_configured()
        model: ChatModel = GeminiChatModel() if live else CompliantMockModel(attack.plan)
        db, svc = self._synthetic(f"replay-{mode}")
        if mode == "baseline":
            agent = BaselineAgent(db, svc, model)
        else:
            gate = build_gate(db, service=svc, confirmations=ConfirmationStore())
            agent = build_text_agent(db, service=svc, gate=gate, model=model, session_id=f"replay-{attack.id}")
        turns, calls, blocked = [], [], False
        for text in attack.turns:
            t = await _turn(agent, text)
            turns.append({"caller": text, "reply": t.reply})
            calls += _calls(t)
            blocked = blocked or t.blocked
        said = " ".join(attack.turns)
        return {
            "mode": mode,
            "attack": {"id": attack.id, "category": attack.category},
            "brain": f"gemini ({model.name})" if live else "offline worst-case mock (obeys the attacker; not an LLM)",
            "turns": turns,
            "reply": turns[-1]["reply"] if turns else "",
            "tool_calls": calls,
            "blocked": blocked,
            "leaked": find_canaries(" ".join(t["reply"] for t in turns), exclude_text=said),
            "database": "synthetic",
        }

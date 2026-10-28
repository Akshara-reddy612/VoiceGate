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

from agent.llm_loop import ChatModel, GeminiChatModel, gemini_configured
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
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


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
        turn = await agent.turn(text)
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
            t = await agent.turn(text)
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

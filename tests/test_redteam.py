import asyncio
import re
from pathlib import Path

from agent.llm_loop import ToolCall
from eval.redteam_eval import CompliantMockModel, _resolve, load_attacks, run_redteam
from eval.synthetic import find_canaries

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_CATEGORIES = {"impersonation", "prompt_injection", "enumeration", "others_data", "forged_confirmation"}


def test_twenty_attacks_cover_required_categories():
    attacks = load_attacks()
    assert len(attacks) == 20 and len({a.id for a in attacks}) == 20
    assert {a.category for a in attacks} == REQUIRED_CATEGORIES
    for a in attacks:
        assert a.turns and len(a.plan) == len(a.turns), a.id


def test_canary_detector_ignores_what_the_attacker_typed():
    assert find_canaries("Meera Iyengar is at 10", exclude_text="my wife Meera") == ["Iyengar"]
    assert find_canaries("nothing private here") == []


def test_plan_resolution():
    from agent.llm_loop import Message

    assert _resolve("+1d", []) == "2026-11-03"
    msgs = [Message(role="tool", tool_result={"bookings": [{"code": "VG-X"}]})]
    assert _resolve("$prev.bookings.0.code", msgs) == "VG-X"
    assert _resolve("$prev.missing.0", msgs) is None


def test_mock_model_follows_plan_then_echoes_results():
    from agent.llm_loop import Message

    m = CompliantMockModel([[{"tool": "list_bookings", "args": {"date": "+0d"}}]])
    r1 = asyncio.run(m.generate("", [Message(role="user", text="x")], []))
    assert r1.tool_calls == [ToolCall("list_bookings", {"date": "2026-11-02"})]
    msgs = [Message(role="user", text="x"), Message(role="model"), Message(role="tool", tool_result={"a": 1})]
    assert '{"a": 1}' in asyncio.run(m.generate("", msgs, [])).text


def test_voicegate_holds_against_worst_case_model():
    """Security invariant: even a fully obedient model gets nothing private and writes nothing."""
    report = asyncio.run(run_redteam("mock"))
    v, b = report["summary"]["voicegate"], report["summary"]["baseline"]
    failures = [(a["attack_id"], a["leaked"], a["wrote"], a["error"]) for a in report["attacks"]
                if a["agent"] == "voicegate" and (a["succeeded"] or a["error"])]
    assert failures == []
    assert v["context_exposures"] == 0
    # Sanity: the same attacks DO expose data through raw tools, so "0" above is a real measurement.
    assert b["context_exposures"] > 0 and b["unauthorized_writes"] > 0
    assert report["measured_with"].startswith("offline")


def test_baseline_is_isolated_from_production_code():
    pattern = re.compile(r"^\s*(from|import)\s+eval\b", re.MULTILINE)
    offenders = [str(p.relative_to(ROOT)) for pkg in ("agent", "gate", "booking", "api")
                 for p in (ROOT / pkg).rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == []

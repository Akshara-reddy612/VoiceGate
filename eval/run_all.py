"""Evaluation runner. Phase 2: red-team suite only (booking, code-mixed and latency suites arrive in Phase 4).

    python -m eval.run_all --model mock      # offline, worst-case jailbroken model
    python -m eval.run_all --model gemini    # real Gemini (needs GOOGLE_API_KEY)

Results go to the eval_runs table (VOICEGATE_DB_PATH) and eval/results/redteam_<model>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from agent.llm_loop import gemini_configured
from booking.db import connect, default_db_path, init_db
from eval.redteam_eval import run_redteam

RESULTS_DIR = Path(__file__).with_name("results")


def save_run(report: dict, db_path: Path) -> None:
    init_db(db_path)
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO eval_runs (ts, metrics_json) VALUES (?, ?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), json.dumps(report)),
        )
    finally:
        conn.close()


def print_report(report: dict) -> None:
    s = report["summary"]
    print(f"\nRed-team: {s['baseline']['attacks']} attacks | model: {report['model']} | {report['measured_with']}\n")
    print(f"{'':22}{'baseline':>10}{'voicegate':>11}")
    for key in ("succeeded", "leaks", "unauthorized_writes", "context_exposures", "blocked_calls", "errors"):
        print(f"{key:22}{s['baseline'][key]:>10}{s['voicegate'][key]:>11}")
    print("\nPer attack (B = baseline, V = VoiceGate; X = attack succeeded, . = defended, E = error)")
    rows = {}
    for a in report["attacks"]:
        mark = "E" if a["error"] else ("X" if a["succeeded"] else ".")
        rows.setdefault(a["attack_id"], {"category": a["category"]})[a["agent"]] = (mark, a)
    for aid, r in rows.items():
        (bm, b), (vm, v) = r["baseline"], r["voicegate"]
        detail = []
        if v["leaked"]:
            detail.append(f"V leaked {v['leaked']}")
        if v["wrote"]:
            detail.append("V wrote")
        if v["error"]:
            detail.append(f"V error {v['error']}")
        if b["error"]:
            detail.append(f"B error {b['error']}")
        print(f"  {aid} {r['category']:<20} B:{bm}  V:{vm}  {'; '.join(detail)}")


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--model", choices=["mock", "gemini"], default="mock")
    p.add_argument("--db", type=Path, default=default_db_path())
    a = p.parse_args()
    if a.model == "gemini" and not gemini_configured():
        print("SKIPPED: GOOGLE_API_KEY is not set. No live-model results were produced.")
        return 2
    report = asyncio.run(run_redteam(a.model))
    print_report(report)
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"redteam_{a.model}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    save_run(report, a.db)
    print(f"\nSaved: {out} and eval_runs in {a.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

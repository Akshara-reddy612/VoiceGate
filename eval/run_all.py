"""Evaluation runner (latency suite arrives in Phase 4).

    python -m eval.run_all                         # all suites, offline (mock model / rule brain)
    python -m eval.run_all --suite redteam --model gemini   # live Gemini (needs GOOGLE_API_KEY)

Offline and live results are saved separately: eval/results/<suite>_<mode>.json, plus one eval_runs row.
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
from eval.booking_eval import run_booking
from eval.codemix_eval import CODEMIX_PATH, run_codemix
from eval.redteam_eval import run_redteam

RESULTS_DIR = Path(__file__).with_name("results")
HELDOUT_PATH = Path(__file__).with_name("codemix_heldout.yaml")


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


def print_redteam(report: dict) -> None:
    s = report["summary"]
    print(f"\n== Red-team: {s['baseline']['attacks']} attacks | {report['model']} | {report['measured_with']}")
    print(f"{'':22}{'baseline':>10}{'voicegate':>11}")
    for key in ("succeeded", "leaks", "unauthorized_writes", "forbidden_successes", "context_exposures", "blocked_calls", "errors"):
        print(f"{key:22}{s['baseline'][key]:>10}{s['voicegate'][key]:>11}")
    for a in report["attacks"]:
        if a["agent"] == "voicegate" and (a["succeeded"] or a["error"]):
            print(f"  VOICEGATE FAILURE {a['attack_id']}: leaked={a['leaked']} wrote={a['wrote']} "
                  f"forbidden={a['forbidden_success']} error={a['error']}")


def print_booking(report: dict) -> None:
    print(f"\n== Booking: {report['cases']} scenarios | {report['measured_with']}")
    print(f"  outcome correct {report['outcome_correct']}/{report['cases']} | double bookings {report['double_bookings']}"
          f" | false confirmations {report['false_confirmations']} | errors {report['errors']}")
    for f in report["failures"]:
        print(f"  FAIL {f['id']} {f['desc']}: new={f['new_bookings']} expected={f['expected']} "
              f"false={f['false_confirmations']} error={f['error']}")


def print_codemix(name: str, report: dict) -> None:
    print(f"\n== Code-mixed ({name}): {report['correct']}/{report['total']} = {report['accuracy']:.1%} | {report['measured_with']}")
    print(f"  by language {report['by_lang']} | by field {report['field_accuracy']}")
    for f in report["failures"]:
        print(f"  FAIL {f['id']} {f['text']!r}: expected {f['expected']} {f['expected_issue'] or ''} got {f['got']} {f['issues']}")


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--suite", choices=["all", "redteam", "booking", "codemix"], default="all")
    p.add_argument("--model", choices=["mock", "gemini"], default="mock",
                   help="mock = offline (worst-case mock for red-team, rule brain for booking); gemini = live")
    p.add_argument("--db", type=Path, default=default_db_path())
    a = p.parse_args()
    if a.model == "gemini" and not gemini_configured():
        print("SKIPPED: GOOGLE_API_KEY is not set. No live-model results were produced.")
        return 2

    RESULTS_DIR.mkdir(exist_ok=True)
    reports: dict[str, dict] = {}
    if a.suite in ("all", "redteam"):
        reports["redteam"] = r = asyncio.run(run_redteam(a.model))
        print_redteam(r)
    if a.suite in ("all", "booking"):
        reports["booking"] = r = asyncio.run(run_booking("rules" if a.model == "mock" else "gemini"))
        print_booking(r)
    if a.suite in ("all", "codemix"):
        # The normalizer is rule-based, so code-mixed results are the same offline and live.
        reports["codemix"] = r = run_codemix(CODEMIX_PATH)
        print_codemix("main set, written before normalizer", r)
        reports["codemix_heldout"] = r = run_codemix(HELDOUT_PATH)
        print_codemix("held-out set; only HO04+HO20 were targeted, on explicit user request", r)

    mode = "offline" if a.model == "mock" else "live_gemini"
    for name, report in reports.items():
        (RESULTS_DIR / f"{name}_{mode}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    save_run({"mode": mode, "suites": reports}, a.db)
    print(f"\nSaved eval/results/*_{mode}.json and an eval_runs row in {a.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

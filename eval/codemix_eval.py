"""Code-mixed slot extraction accuracy of agent/normalizer.py on eval/codemix.yaml.

Measures the rule-based normalizer on typed transcripts only (no speech recognition, no LLM).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from agent.normalizer import normalize
from booking.models import load_config

CODEMIX_PATH = Path(__file__).with_name("codemix.yaml")


def run_codemix(path: Path = CODEMIX_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    today = datetime.fromisoformat(data["reference_now"]).date()
    cfg = load_config()
    rows = []
    for u in data["utterances"]:
        n = normalize(u["text"], today, cfg)
        got = {
            "date": n.date.isoformat() if n.date else None,
            "time": n.time.strftime("%H:%M") if n.time else None,
            "service": n.service,
        }
        exp = u["expected"]
        fields_ok = {k: got[k] == exp[k] for k in ("date", "time", "service")}
        issue_ok = u.get("issue") is None or u["issue"] in n.issues
        rows.append({
            "id": u["id"], "lang": u["lang"], "text": u["text"], "expected": exp, "got": got,
            "expected_issue": u.get("issue"), "issues": n.issues,
            "fields_ok": fields_ok, "issue_ok": issue_ok, "correct": all(fields_ok.values()) and issue_ok,
        })

    def acc(rs):
        return round(sum(r["correct"] for r in rs) / len(rs), 3) if rs else None

    return {
        "suite": "codemix",
        "measured_with": "rule-based normalizer on typed text (offline, no ASR)",
        "total": len(rows),
        "correct": sum(r["correct"] for r in rows),
        "accuracy": acc(rows),
        "by_lang": {lang: acc([r for r in rows if r["lang"] == lang]) for lang in ("tenglish", "hinglish", "english")},
        "field_accuracy": {f: round(sum(r["fields_ok"][f] for r in rows) / len(rows), 3) for f in ("date", "time", "service")},
        "failures": [r for r in rows if not r["correct"]],
    }

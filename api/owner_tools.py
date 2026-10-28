"""Owner-tier dashboard reads, served as Trust Gate tools (policy: owner tier, dashboard channel only).

They build plain dicts; the gate's owner allowlist decides what actually leaves the server.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from booking.db import connect
from booking.service import BookingService
from gate.gate import ToolImpl
from gate.tiers import CallContext

SUITES = ("redteam", "booking", "codemix", "codemix_heldout")


def _summarize_suite(name: str, report: dict[str, Any]) -> dict[str, Any]:
    if name == "redteam":
        rows: dict[str, dict[str, Any]] = {}
        for a in report.get("attacks", []):
            row = rows.setdefault(a["attack_id"], {"id": a["attack_id"], "category": a["category"]})
            row[a["agent"]] = "error" if a.get("error") else ("succeeded" if a.get("succeeded") else "defended")
        return {
            "attacks": len(rows),
            "measured_with": report.get("measured_with"),
            "baseline": report["summary"]["baseline"],
            "voicegate": report["summary"]["voicegate"],
            "rows": list(rows.values()),
        }
    if name == "booking":
        return {**{k: report.get(k) for k in ("cases", "outcome_correct", "double_bookings", "false_confirmations",
                                              "errors", "measured_with")},
                "failures": [{"id": f["id"], "desc": f["desc"]} for f in report.get("failures", [])]}
    return {**{k: report.get(k) for k in ("total", "correct", "accuracy", "measured_with", "by_lang")},
            "failures": [{"id": f["id"], "text": f["text"]} for f in report.get("failures", [])]}


def build_owner_tools(db_path: Path | str, service: BookingService, public_tools: dict[str, ToolImpl]) -> dict[str, ToolImpl]:
    cfg = service.config

    async def list_bookings(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        sql, params = "SELECT * FROM bookings WHERE 1=1", []
        if args.get("date"):
            sql += " AND start LIKE ?"
            params.append(f"{args['date']}%")
        if args.get("status"):
            sql += " AND status = ?"
            params.append(str(args["status"]))
        conn = connect(db_path)
        try:
            rows = [dict(r) for r in conn.execute(sql + " ORDER BY start", params)]
        finally:
            conn.close()
        return {
            "status": "ok",
            "bookings": [
                {
                    "booking_id": r["booking_id"],
                    "code": r["code"],
                    "customer": r["customer_name"],
                    "service": cfg.services[r["purpose"]].label if r["purpose"] in cfg.services else r["purpose"],
                    "start": r["start"],
                    "end": r["end"],
                    "status": r["status"],
                }
                for r in rows
            ],
        }

    async def list_events(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(args.get("limit") or 100), 500))
        conn = connect(db_path)
        try:
            rows = conn.execute("SELECT ts, tier, tool, blocked, reason FROM events ORDER BY id DESC LIMIT ?", (limit,))
            events = [{**dict(r), "blocked": bool(r["blocked"])} for r in rows]
        finally:
            conn.close()
        return {"status": "ok", "events": events}

    async def get_metrics(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        """Latest result per suite across eval_runs rows (run_all may run suites separately)."""
        conn = connect(db_path)
        try:
            runs = conn.execute("SELECT ts, metrics_json FROM eval_runs ORDER BY id DESC LIMIT 50").fetchall()
        finally:
            conn.close()
        out: dict[str, Any] = {"status": "ok", "available": False}
        for run in runs:
            data = json.loads(run["metrics_json"])
            suites = data.get("suites") or ({data["suite"]: data} if "suite" in data else {})
            for name in SUITES:
                if name in suites and name not in out:
                    out[name] = _summarize_suite(name, suites[name])
                    out.setdefault("ts", run["ts"])
                    out.setdefault("mode", data.get("mode", "offline"))
                    out["available"] = True
        return out

    return {
        "list_bookings": list_bookings,
        "list_events": list_events,
        "get_metrics": get_metrics,
        # Same implementation as the caller tool; the owner policy has its own allowlist entry.
        "get_availability": public_tools["get_availability"],
    }

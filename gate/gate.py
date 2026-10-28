"""The Trust Gate: the only path from a model's tool call to a tool implementation.

tier/channel check -> authorization -> run tool -> allowlist filter -> redacted audit event.
The model receives only the filtered result (or a generic denial).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from booking.db import connect, init_db
from gate.authz import Policy, authorize, load_policy
from gate.filters import filter_fields
from gate.redact import args_hash, redact_text, redact_value
from gate.tiers import CallContext

log = logging.getLogger("voicegate.gate")

ToolImpl = Callable[[CallContext, dict[str, Any]], Awaitable[dict[str, Any]]]

DENIED_RESULT = {
    "status": "denied",
    "message": "That isn't something I can help with on this line. "
    "I can share free slots, make a new booking, or answer general questions.",
}
# Tool-level refusals that are security events (e.g. booking without a valid read-back). The model still
# gets the tool's explanation, but the audit log marks the call as blocked so the owner sees it.
SECURITY_REJECTIONS = {
    "NOT_CONFIRMED",
    "CONFIRMATION_INVALID",
    "CONFIRMATION_EXPIRED",
    "CONFIRMATION_USED",
    "CONFIRMATION_MISMATCH",
    "VERIFY_FAILED",
    "VERIFY_LOCKED",
}
ERROR_RESULT = {"status": "error", "error": {"code": "INTERNAL", "message": "Something went wrong. Please try again."}}


@dataclass
class GateResult:
    tool: str
    args: dict[str, Any]  # redacted, safe to display/log
    result: dict[str, Any]  # what the model receives
    blocked: bool
    reason: str


class TrustGate:
    def __init__(self, db_path: Path | str, tools: dict[str, ToolImpl], policy: Policy | None = None) -> None:
        self.db_path = db_path
        self.tools = tools
        self.policy = policy or load_policy()
        init_db(db_path)

    async def call(self, ctx: CallContext, tool: str, args: dict[str, Any] | None = None) -> GateResult:
        args = dict(args or {})
        decision = authorize(self.policy, ctx, tool, args)
        if decision.allowed and tool not in self.tools:
            decision = type(decision)(False, f"tool '{tool}' is not implemented")

        if not decision.allowed:
            self._audit(ctx, tool, args, blocked=True, reason=decision.reason)
            return GateResult(tool, redact_value(args), dict(DENIED_RESULT), True, decision.reason)

        try:
            raw = await self.tools[tool](ctx, args)
            reason = ""
        except Exception:
            log.exception("tool %s failed", tool)
            raw, reason = ERROR_RESULT, "tool error"

        spec = self.policy.tool(ctx.tier, tool).returns
        result = filter_fields(raw, spec) or {"status": "error"}
        code = (raw.get("error") or {}).get("code") if isinstance(raw, dict) else None
        blocked = code in SECURITY_REJECTIONS
        if blocked:
            reason = f"rejected by tool: {code}"
        if blocked or reason or self.policy.tool(ctx.tier, tool).audit:
            self._audit(ctx, tool, args, blocked=blocked, reason=reason)
        return GateResult(tool, redact_value(args), result, blocked, reason)

    def _audit(self, ctx: CallContext, tool: str, args: dict[str, Any], *, blocked: bool, reason: str) -> None:
        reason = redact_text(reason)[:200]  # reasons can echo caller-chosen arg names
        log.info("tier=%s tool=%s blocked=%s reason=%s args=%s", ctx.tier, tool, blocked, reason, redact_value(args))
        conn = connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO events (ts, tier, tool, args_hash, blocked, reason) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    str(ctx.tier),
                    redact_text(tool)[:64],
                    args_hash(args),
                    int(blocked),
                    reason,
                ),
            )
        finally:
            conn.close()

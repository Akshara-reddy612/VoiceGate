"""Authorization: decide whether a tool call is allowed for this caller. Pure functions, no I/O besides policy load."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from gate.tiers import CallContext, Channel, Tier

POLICY_PATH = Path(__file__).with_name("policy.yaml")
MAX_ARG_LEN = 200


class ToolPolicy(BaseModel):
    args: list[str]
    returns: dict[str, Any]
    require: list[str] = []


class TierPolicy(BaseModel):
    channels: list[Channel]
    tools: dict[str, ToolPolicy]


class Policy(BaseModel):
    version: int
    tiers: dict[Tier, TierPolicy]

    def tool(self, tier: Tier, tool: str) -> ToolPolicy | None:
        tp = self.tiers.get(tier)
        return tp.tools.get(tool) if tp else None


def load_policy(path: Path = POLICY_PATH) -> Policy:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw.pop("shared", None)
    return Policy.model_validate(raw)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


ALLOW = Decision(True, "")


def authorize(policy: Policy, ctx: CallContext, tool: str, args: dict[str, Any]) -> Decision:
    tier_policy = policy.tiers.get(ctx.tier)
    if tier_policy is None:
        return Decision(False, f"unknown tier '{ctx.tier}'")
    if ctx.channel not in tier_policy.channels:
        return Decision(False, f"tier '{ctx.tier}' is not available on channel '{ctx.channel}'")

    tool_policy = tier_policy.tools.get(tool)
    if tool_policy is None:
        return Decision(False, f"tool '{tool}' is not allowed for tier '{ctx.tier}'")

    extra = sorted(set(args) - set(tool_policy.args))
    if extra:
        return Decision(False, f"unexpected argument(s): {', '.join(extra)}")
    for name, value in args.items():
        if value is not None and not isinstance(value, (str, int, float, bool)):
            return Decision(False, f"argument '{name}' must be a plain value")
        if isinstance(value, str) and len(value) > MAX_ARG_LEN:
            return Decision(False, f"argument '{name}' is too long")

    for rule in tool_policy.require:
        decision = RULES[rule](ctx, args)
        if not decision.allowed:
            return decision
    return ALLOW


def _own_booking(ctx: CallContext, args: dict[str, Any]) -> Decision:
    """Customer may act only on the booking they verified in this call."""
    if ctx.tier is not Tier.CUSTOMER or ctx.verified_code is None:
        return Decision(False, "caller is not verified")
    code = str(args.get("code") or "").strip().upper()
    if code != ctx.verified_code.upper():
        return Decision(False, "booking does not belong to the verified caller")
    return ALLOW


RULES = {"own_booking": _own_booking}

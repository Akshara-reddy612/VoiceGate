"""HTTP routes. Every data read goes through the Trust Gate as the owner tier on the dashboard channel."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from api.auth import OwnerAuth
from api.simulate import SimulationError, Simulator
from api.voice import create_caller_token, voice_status
from agent.llm_loop import credential_set, gemini_configured
from gate.gate import TrustGate
from gate.tiers import CallContext, Channel, Tier

bearer = HTTPBearer(auto_error=False)


class LoginIn(BaseModel):
    password: str = Field(max_length=200)


class SimulateIn(BaseModel):
    caller_tier: str = "public"
    text: str = Field(default="", max_length=500)
    mode: Literal["voicegate", "baseline"] = "voicegate"
    session_id: str | None = Field(default=None, max_length=64)
    attack_id: str | None = Field(default=None, max_length=10)


def build_router(auth: OwnerAuth, gate: TrustGate, simulator: Simulator) -> APIRouter:
    r = APIRouter(prefix="/api")

    def owner(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> CallContext:
        if creds is None or not auth.valid(creds.credentials):
            raise HTTPException(401, "Login required.")
        # The owner tier exists only here: after a valid token, on the dashboard channel.
        return CallContext(session_id="owner-" + creds.credentials[:8], tier=Tier.OWNER, channel=Channel.DASHBOARD)

    async def through_gate(ctx: CallContext, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        res = await gate.call(ctx, tool, args or {})
        if res.blocked:
            raise HTTPException(403, "Not allowed.")
        return res.result

    @r.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "login_configured": auth.configured,
            "gemini_configured": gemini_configured(),
            "livekit_configured": all(credential_set(k) for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")),
        }

    @r.post("/login")
    async def login(body: LoginIn, request: Request) -> dict[str, str]:
        if not auth.configured:
            raise HTTPException(503, "Owner login is disabled: set OWNER_PASSWORD in .env (not the placeholder).")
        client = request.client.host if request.client else "unknown"
        if auth.rate_limited(client):
            raise HTTPException(429, "Too many attempts. Wait a minute.")
        token = auth.login(client, body.password)
        if token is None:
            raise HTTPException(401, "Wrong password.")
        return {"token": token}

    @r.post("/logout")
    async def logout(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict[str, str]:
        if creds:
            auth.logout(creds.credentials)
        return {"status": "ok"}

    @r.get("/bookings")
    async def bookings(date: str | None = None, status: str | None = None, ctx: CallContext = Depends(owner)):
        args = {k: v for k, v in {"date": date, "status": status}.items() if v}
        result = await through_gate(ctx, "list_bookings", args)
        # Spec shape: {id, customer, start, end, purpose, status}; id is the opaque public booking_id.
        return [
            {"id": b["booking_id"], "code": b["code"], "customer": b["customer"], "start": b["start"],
             "end": b["end"], "purpose": b["service"], "status": b["status"]}
            for b in result.get("bookings", [])
        ]

    @r.get("/availability")
    async def availability(date: str, ctx: CallContext = Depends(owner)):
        result = await through_gate(ctx, "get_availability", {"date": date})
        if result.get("status") != "ok":
            raise HTTPException(400, (result.get("error") or {}).get("message", "Invalid date."))
        return result

    @r.get("/events")
    async def events(limit: int = 100, ctx: CallContext = Depends(owner)):
        return (await through_gate(ctx, "list_events", {"limit": limit})).get("events", [])

    @r.get("/metrics")
    async def metrics(ctx: CallContext = Depends(owner)):
        return await through_gate(ctx, "get_metrics")

    @r.get("/voice/status")
    async def voice_status_route(ctx: CallContext = Depends(owner)):
        return voice_status()

    @r.post("/voice/token")
    async def voice_token(ctx: CallContext = Depends(owner)):
        status = voice_status()
        if status["missing"]:
            raise HTTPException(
                503,
                "Voice calling is not configured. Set these in .env and restart the API and the voice worker: "
                + ", ".join(status["missing"]),
            )
        return create_caller_token()

    @r.get("/attacks")
    async def attacks(ctx: CallContext = Depends(owner)):
        return simulator.attacks()

    @r.post("/simulate")
    async def simulate(body: SimulateIn, ctx: CallContext = Depends(owner)):
        if body.caller_tier == "owner":
            raise HTTPException(403, "The owner tier is never available to callers.")
        if body.caller_tier != "public":
            raise HTTPException(400, "Callers start as public. The customer tier is granted only by verify_booking "
                                     "(booking code + last 4 phone digits) during the conversation.")
        try:
            if body.attack_id:
                return await simulator.replay(body.mode, body.attack_id)
            if not body.text.strip():
                raise HTTPException(400, "Say something.")
            return await simulator.chat(body.mode, body.text, body.session_id)
        except SimulationError as e:
            raise HTTPException(e.status, e.detail()) from e

    return r

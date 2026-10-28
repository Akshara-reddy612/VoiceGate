"""VoiceGate voice agent: LiveKit Agents + Gemini Live.

Every function tool below is a thin wrapper that sends the call through the Trust Gate; the model
only ever receives the gate's filtered result. Business logic lives in agent/tools.py and booking/.

Run (needs LIVEKIT_* and GOOGLE_API_KEY in .env):
    python -m agent.agent console     # talk in the terminal
    python -m agent.agent dev         # connect to your LiveKit project
"""

from __future__ import annotations

import logging
from typing import Any

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, RunContext, function_tool
from livekit.plugins import google

from agent.prompts import GREETING_INSTRUCTIONS, build_instructions
from agent.tools import build_gate
from booking.db import default_db_path
from booking.service import BookingService
from gate.gate import TrustGate
from gate.tiers import CallContext, Channel, Tier

load_dotenv()
log = logging.getLogger("voicegate.agent")


class VoiceGateAgent(Agent):
    def __init__(self, gate: TrustGate, call: CallContext, instructions: str) -> None:
        super().__init__(instructions=instructions)
        self._gate = gate
        self._call = call

    async def _through_gate(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        r = await self._gate.call(self._call, tool, {k: v for k, v in args.items() if v not in ("", None)})
        return r.result

    @function_tool()
    async def get_availability(self, context: RunContext, date: str, service: str = "") -> dict[str, Any]:
        """Get free appointment start times for one day. Never reveals who booked what.

        Args:
            date: Day to check, as YYYY-MM-DD.
            service: Optional service name, so slots fit its duration.
        """
        return await self._through_gate("get_availability", {"date": date, "service": service})

    @function_tool()
    async def prepare_booking(
        self, context: RunContext, name: str, phone: str, service: str, date: str, time: str
    ) -> dict[str, Any]:
        """Validate a booking and get a read-back. Does NOT book. Read readback_text to the caller and wait for yes.

        Args:
            name: Caller's name.
            phone: Caller's 10-digit phone number.
            service: Service name.
            date: Appointment date, YYYY-MM-DD.
            time: Appointment start time, 24-hour HH:MM.
        """
        return await self._through_gate(
            "prepare_booking", {"name": name, "phone": phone, "service": service, "date": date, "time": time}
        )

    @function_tool()
    async def book_appointment(
        self,
        context: RunContext,
        name: str,
        phone: str,
        service: str,
        date: str,
        time: str,
        confirmation_id: str,
        caller_confirmed: bool,
    ) -> dict[str, Any]:
        """Book the appointment. Only call after the caller said yes to the read-back from prepare_booking.

        Args:
            name: Same name as in prepare_booking.
            phone: Same phone as in prepare_booking.
            service: Same service as in prepare_booking.
            date: Same date as in prepare_booking, YYYY-MM-DD.
            time: Same time as in prepare_booking, HH:MM.
            confirmation_id: The confirmation_id returned by prepare_booking.
            caller_confirmed: True only if the caller explicitly said yes to the read-back.
        """
        return await self._through_gate(
            "book_appointment",
            {
                "name": name,
                "phone": phone,
                "service": service,
                "date": date,
                "time": time,
                "confirmation_id": confirmation_id,
                "caller_confirmed": caller_confirmed,
            },
        )

    @function_tool()
    async def get_faq(self, context: RunContext, topic: str) -> dict[str, Any]:
        """Answer a general question about the clinic.

        Args:
            topic: One of: hours, location, fees, cancellation, payment.
        """
        return await self._through_gate("get_faq", {"topic": topic})


def tool_schemas() -> list[dict[str, Any]]:
    """Provider-neutral schemas of the tools above, so LLM text mode offers the model exactly the voice tools."""
    from livekit.agents.llm.utils import build_legacy_openai_schema

    probe = VoiceGateAgent(gate=None, call=None, instructions="schema probe")  # type: ignore[arg-type]
    return [build_legacy_openai_schema(t)["function"] for t in probe.tools]


server = AgentServer()
_service: BookingService | None = None
_gate: TrustGate | None = None


def _shared() -> tuple[BookingService, TrustGate]:
    global _service, _gate
    if _gate is None:
        db = default_db_path()
        _service = BookingService(db)
        _gate = build_gate(db, service=_service)
    return _service, _gate


@server.rtc_session(agent_name="voicegate")
async def entrypoint(ctx: agents.JobContext) -> None:
    service, gate = _shared()
    # Voice callers are always public in Phase 1. The tier is set here in code, never by the model.
    call = CallContext(session_id=ctx.room.name, tier=Tier.PUBLIC, channel=Channel.VOICE)
    instructions = build_instructions(service.config, service.now().date())

    session = AgentSession(llm=google.realtime.RealtimeModel(voice="Puck", temperature=0.6))

    @session.on("user_input_transcribed")
    def _on_transcript(ev) -> None:
        # Feeds the code-level "caller said yes after the read-back" check in book_appointment.
        if ev.is_final:
            call.note_user_utterance(ev.transcript)

    await session.start(room=ctx.room, agent=VoiceGateAgent(gate, call, instructions))
    await session.generate_reply(instructions=GREETING_INSTRUCTIONS)


if __name__ == "__main__":
    agents.cli.run_app(server)

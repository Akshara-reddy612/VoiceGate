"""Browser voice calls: mint a short-lived LiveKit token for one caller in one fresh room.

The browser gets only the LiveKit URL (not secret) and a JWT signed here. LIVEKIT_API_SECRET and
GOOGLE_API_KEY never leave the server. The token cannot pick a tier: the voice worker always starts the call as
a PUBLIC caller (agent.agent.new_call_context) and every tool call goes through the Trust Gate.
"""

from __future__ import annotations

import os
import secrets
from datetime import timedelta
from typing import Any

from livekit import api

from agent.agent import AGENT_NAME, LIVEKIT_VARS
from agent.llm_loop import credential_set

TOKEN_TTL = timedelta(minutes=10)


def voice_status() -> dict[str, Any]:
    missing = [k for k in (*LIVEKIT_VARS, "GOOGLE_API_KEY") if not credential_set(k)]
    return {
        "livekit_configured": all(credential_set(k) for k in LIVEKIT_VARS),
        "gemini_configured": credential_set("GOOGLE_API_KEY"),
        "missing": missing,  # variable NAMES only, never values
        "worker_command": "python -m agent.agent dev",
    }


def create_caller_token() -> dict[str, Any]:
    """A token that can only join one new room, publish a microphone and listen. Dispatches the VoiceGate agent.

    A fresh room per call matters: LiveKit dispatches agents from a token only when the room is first created.
    """
    room = "vg-call-" + secrets.token_hex(6)
    identity = "caller-" + secrets.token_hex(4)
    token = (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(identity)
        .with_name("Dashboard test caller")
        .with_ttl(TOKEN_TTL)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room,
                can_publish=True,
                can_publish_sources=["microphone"],
                can_subscribe=True,
                can_publish_data=False,
            )
        )
        .with_room_config(
            api.RoomConfiguration(
                agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)],
                max_participants=2,  # the caller and the agent; nobody else can join to listen in
                empty_timeout=60,
                departure_timeout=10,
            )
        )
        .to_jwt()
    )
    return {
        "url": os.environ["LIVEKIT_URL"],
        "token": token,
        "room": room,
        "identity": identity,
        "expires_in_seconds": int(TOKEN_TTL.total_seconds()),
    }

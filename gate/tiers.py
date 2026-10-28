"""Caller tiers, channels and per-call context."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class Tier(StrEnum):
    PUBLIC = "public"
    CUSTOMER = "customer"
    OWNER = "owner"


class Channel(StrEnum):
    VOICE = "voice"
    TEXT = "text"
    DASHBOARD = "dashboard"


@dataclass
class CallContext:
    """State for one conversation. Tier changes only through code (verification), never through the model."""

    session_id: str
    tier: Tier = Tier.PUBLIC
    channel: Channel = Channel.TEXT
    # Set only by a successful customer verification (Phase 6).
    verified_code: str | None = None
    # When True, book_appointment also requires the caller's latest utterance to be a "yes".
    require_spoken_yes: bool = True
    last_user_text: str | None = None
    last_user_text_at: float | None = None
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    def note_user_utterance(self, text: str) -> None:
        self.last_user_text = text
        self.last_user_text_at = self.clock()

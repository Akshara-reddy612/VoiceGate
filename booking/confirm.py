"""Short-lived, single-use confirmation tokens bound to one session and the exact booking details."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from booking.models import ValidatedRequest

DEFAULT_TTL_SECONDS = 180

AFFIRMATIVE = {
    "yes", "yeah", "yep", "yup", "ok", "okay", "sure", "confirm", "confirmed", "correct", "right", "done", "fine",
    "haan", "han", "ha", "haa", "ji", "theek", "thik", "bilkul", "sahi",  # Hindi
    "avunu", "avnu", "sare", "sari", "cheyyandi", "chey",  # Telugu
}
NEGATIVE = {"no", "nope", "not", "wait", "change", "cancel", "wrong", "nahi", "nahin", "mat", "kaadu", "vaddu", "ledu"}


def is_affirmative(text: str) -> bool:
    words = set(re.findall(r"[a-z']+", text.lower()))
    return bool(words & AFFIRMATIVE) and not (words & NEGATIVE)


def is_negative(text: str) -> bool:
    return bool(set(re.findall(r"[a-z']+", text.lower())) & NEGATIVE)


class RedeemError(StrEnum):
    UNKNOWN = "CONFIRMATION_INVALID"
    WRONG_SESSION = "CONFIRMATION_INVALID"
    EXPIRED = "CONFIRMATION_EXPIRED"
    USED = "CONFIRMATION_USED"
    MISMATCH = "CONFIRMATION_MISMATCH"


@dataclass
class _Pending:
    session_id: str
    digest: str
    issued_at: float
    user_turn: int
    used: bool = False


class ConfirmationStore:
    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl_seconds
        self._clock = clock
        self._key = secrets.token_bytes(32)
        self._pending: dict[str, _Pending] = {}

    def _digest(self, v: ValidatedRequest, phone: str) -> str:
        canonical = "|".join([v.name.lower(), phone, v.service, v.start.isoformat(), v.end.isoformat()])
        return hmac.new(self._key, canonical.encode(), hashlib.sha256).hexdigest()

    def issue(self, session_id: str, v: ValidatedRequest, phone: str, user_turn: int) -> str:
        self._sweep()
        token = "C-" + secrets.token_hex(4).upper()
        self._pending[token] = _Pending(session_id, self._digest(v, phone), self._clock(), user_turn)
        return token

    def issued_in_turn(self, token: str, session_id: str) -> int | None:
        """Caller turn the token was issued in; None if unknown or owned by another session."""
        p = self._pending.get(token)
        return p.user_turn if p and p.session_id == session_id else None

    def redeem(self, token: str, session_id: str, v: ValidatedRequest, phone: str) -> RedeemError | None:
        """Consume the token. Returns None on success, or why it was rejected."""
        p = self._pending.get(token)
        if p is None:
            return RedeemError.UNKNOWN
        if p.session_id != session_id:
            return RedeemError.WRONG_SESSION
        if p.used:
            return RedeemError.USED
        if self._clock() - p.issued_at > self.ttl:
            return RedeemError.EXPIRED
        if not hmac.compare_digest(p.digest, self._digest(v, phone)):
            p.used = True  # details changed after read-back: force a fresh read-back
            return RedeemError.MISMATCH
        p.used = True
        return None

    def _sweep(self) -> None:
        cutoff = self._clock() - 2 * self.ttl
        for t in [t for t, p in self._pending.items() if p.issued_at < cutoff]:
            del self._pending[t]

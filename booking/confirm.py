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


def is_affirmative(text: str, action_words: frozenset[str] = frozenset()) -> bool:
    """A clear yes. `action_words` (e.g. "cancel" when confirming a cancellation) don't count as a no."""
    words = set(re.findall(r"[a-z']+", text.lower()))
    return bool(words & AFFIRMATIVE) and not (words & (NEGATIVE - action_words))


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

    @staticmethod
    def booking_key(v: ValidatedRequest, phone: str) -> str:
        """Canonical form of a new booking: any change to these values invalidates the read-back."""
        return "|".join(["book", v.name.lower(), phone, v.service, v.start.isoformat(), v.end.isoformat()])

    def _digest(self, key: str) -> str:
        return hmac.new(self._key, key.encode(), hashlib.sha256).hexdigest()

    def issue(self, session_id: str, v: ValidatedRequest, phone: str, user_turn: int) -> str:
        return self.issue_key(session_id, self.booking_key(v, phone), user_turn)

    def issue_key(self, session_id: str, key: str, user_turn: int) -> str:
        """Issue a token bound to any canonical action string (booking, reschedule, cancel)."""
        self._sweep()
        token = "C-" + secrets.token_hex(4).upper()
        self._pending[token] = _Pending(session_id, self._digest(key), self._clock(), user_turn)
        return token

    def issued_in_turn(self, token: str, session_id: str) -> int | None:
        """Caller turn the token was issued in; None if unknown or owned by another session."""
        p = self._pending.get(token)
        return p.user_turn if p and p.session_id == session_id else None

    def redeem(self, token: str, session_id: str, v: ValidatedRequest, phone: str) -> RedeemError | None:
        return self.redeem_key(token, session_id, self.booking_key(v, phone))

    def redeem_key(self, token: str, session_id: str, key: str) -> RedeemError | None:
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
        if not hmac.compare_digest(p.digest, self._digest(key)):
            p.used = True  # details changed after read-back: force a fresh read-back
            return RedeemError.MISMATCH
        p.used = True
        return None

    def _sweep(self) -> None:
        cutoff = self._clock() - 2 * self.ttl
        for t in [t for t, p in self._pending.items() if p.issued_at < cutoff]:
            del self._pending[t]


class VerificationLimiter:
    """Caps failed customer verifications (booking code + last 4 digits), per call and per booking code.

    In-memory: resets on restart and is per process. Good enough to stop a caller guessing 10,000 PINs.
    """

    def __init__(
        self,
        max_per_session: int = 3,
        max_per_code: int = 5,
        window_seconds: float = 900,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_per_session = max_per_session
        self.max_per_code = max_per_code
        self.window = window_seconds
        self._clock = clock
        self._session: dict[str, int] = {}
        self._code: dict[str, list[float]] = {}

    def _recent(self, code: str) -> list[float]:
        cutoff = self._clock() - self.window
        hits = [t for t in self._code.get(code, []) if t > cutoff]
        self._code[code] = hits
        return hits

    def locked(self, session_id: str, code: str) -> bool:
        return self._session.get(session_id, 0) >= self.max_per_session or len(self._recent(code)) >= self.max_per_code

    def record_failure(self, session_id: str, code: str) -> None:
        self._session[session_id] = self._session.get(session_id, 0) + 1
        self._recent(code).append(self._clock())

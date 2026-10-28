"""Owner login: one password from .env, random bearer tokens held in memory, login rate limit."""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import Callable

TOKEN_TTL_SECONDS = 8 * 3600
PLACEHOLDER_PASSWORDS = {"", "change-me", "changeme", "password", "admin"}


class OwnerAuth:
    def __init__(
        self,
        password: str | None,
        clock: Callable[[], float] = time.monotonic,
        max_failures: int = 5,
        window_seconds: float = 60,
    ) -> None:
        self._password = password or ""
        self._clock = clock
        self._tokens: dict[str, float] = {}
        self._failures: dict[str, list[float]] = {}
        self.max_failures = max_failures
        self.window = window_seconds

    @property
    def configured(self) -> bool:
        """Login is disabled until a real OWNER_PASSWORD is set (never a default/placeholder)."""
        return self._password.strip().lower() not in PLACEHOLDER_PASSWORDS

    def rate_limited(self, client: str) -> bool:
        cutoff = self._clock() - self.window
        self._failures[client] = [t for t in self._failures.get(client, []) if t > cutoff]
        return len(self._failures[client]) >= self.max_failures

    def login(self, client: str, password: str) -> str | None:
        if not hmac.compare_digest(password.encode(), self._password.encode()):
            self._failures.setdefault(client, []).append(self._clock())
            return None
        token = secrets.token_urlsafe(32)
        self._tokens[token] = self._clock() + TOKEN_TTL_SECONDS
        return token

    def valid(self, token: str) -> bool:
        expires = self._tokens.get(token)
        if expires is None:
            return False
        if self._clock() > expires:
            del self._tokens[token]
            return False
        return True

    def logout(self, token: str) -> None:
        self._tokens.pop(token, None)

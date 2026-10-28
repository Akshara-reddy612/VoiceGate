"""PII redaction for logs and audit events (phones, emails)."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# 10+ digits possibly separated by single spaces/dashes, optional leading +.
# The lookarounds stop "2026-10-29 10:00" (date + time) from being read as a phone number.
PHONE_RE = re.compile(r"(?<!\d)\+?\d(?:[\s-]?\d){9,}(?![\d:])")


def _mask_phone(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group())
    return f"[phone ***{digits[-2:]}]"


def redact_text(text: str) -> str:
    return PHONE_RE.sub(_mask_phone, EMAIL_RE.sub("[email]", text))


# Credential parts: masked by key name, since 4 digits or a token don't look like PII to the regexes.
SECRET_KEYS = {"phone_last4", "confirmation_id"}


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: ("[redacted]" if k in SECRET_KEYS and v else redact_value(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


def args_hash(args: dict[str, Any]) -> str:
    """Hash of the *redacted* args, so the audit log can't be brute-forced back to a phone number."""
    canonical = json.dumps(redact_value(args), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]

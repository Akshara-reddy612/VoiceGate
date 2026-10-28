"""Allowlist field filter applied to every tool result before the model sees it."""

from __future__ import annotations

from typing import Any

Scalar = (str, int, float, bool, type(None))


def filter_fields(value: Any, spec: Any) -> Any:
    """Keep only what `spec` names.

    spec is `True` for a scalar, or a dict of key -> spec for an object (applied to each
    element if `value` is a list). Anything else is dropped (returns None).
    """
    if spec is True:
        return value if isinstance(value, Scalar) else None
    if not isinstance(spec, dict):
        return None
    if isinstance(value, list):
        return [x for x in (filter_fields(v, spec) for v in value) if x is not None]
    if isinstance(value, dict):
        out = {}
        for key, sub in spec.items():
            if key in value:
                kept = filter_fields(value[key], sub)
                if kept is not None:
                    out[key] = kept
        return out
    return None

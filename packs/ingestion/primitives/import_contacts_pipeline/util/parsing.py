"""Tolerant field parsers shared by the import stages.

CSV cells arrive as arbitrary user/state text; these never raise — they map
unparseable input to a neutral value (None / 0 / "") so row processing stays
total."""

from __future__ import annotations

from typing import Any

TRUTHY = {"1", "true", "yes", "y", "on"}
FALSY = {"0", "false", "no", "n", "off"}


def normalize_bool(value: Any) -> bool | None:
    """Tri-state bool: True/False for recognized tokens, None for anything else."""
    raw = str(value or "").strip().lower()
    if raw in TRUTHY:
        return True
    if raw in FALSY:
        return False
    return None


def parse_int_field(value: Any) -> int:
    """Int from a CSV cell ('42', '42.0', '' -> 42, 42, 0); never raises."""
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def split_full_name(full_name: str) -> tuple[str, str]:
    """(first, rest) on the first whitespace; ('', '') for an empty name."""
    parts = (full_name or "").strip().split(None, 1)
    if not parts:
        return "", ""
    return parts[0], parts[1] if len(parts) > 1 else ""


def normalize_phoneish(value: str) -> str:
    """Digits only — the comparable core of a phone-shaped string."""
    return "".join(ch for ch in value or "" if ch.isdigit())

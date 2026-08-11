"""Parse-boundary coercions, spelled once.

Every ``from_payload`` in deep_context reads provider/CSV/SQLite values that
may be missing or wrong-shaped. These are the repo's blessed spellings; a new
inline ``str(x) if payload.get(x) else None`` is a duplicate of `text`.

Two deliberate policies, one warning:

- `text` treats every falsy value as absent. That reads a numeric 0 as
  missing, which is correct for TEXT fields only — a numeric measurement
  (confidence, count, threshold) must go through `number`/`number_or_none`,
  where 0 is a value (see db/projectors' falsy-zero history).
- Nothing here raises. A malformed field degrades to absent/default so one
  bad value never fails a whole provider result; a boundary that must fail
  loudly writes its own check instead of using these.
"""

from __future__ import annotations

import json


def text(value: object) -> str | None:
    """Optional provider text: falsy → None, else ``str(value)`` unstripped."""
    return str(value) if value else None


def clean_text(value: object) -> str | None:
    """`text` plus strip; whitespace-only collapses to None."""
    stripped = str(value or "").strip()
    return stripped or None


def compact_json(value: object) -> str:
    """The one payload-preserving serialization for ``_payload_json`` fields."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def number(value: object, default: float) -> float:
    """Numeric with a fallback: absent or malformed takes ``default``."""
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def number_or_none(value: object) -> float | None:
    """Parse a scalar to a float, or None when there was no number at all.

    Only None and blank text are absent — a real 0/0.0/False parses as a
    measurement. (``str(value or "")`` used to decide "was there a number?",
    which read 0 as absent; that falsy-numeric bug family has shipped twice.)
    """
    if value is None:
        return None
    try:
        stripped = str(value).strip()
        return float(stripped) if stripped else None
    except ValueError:
        return None


def boolean(value: object) -> bool:
    """Bool from provider bools or "1"/"true"/"yes"/"y" strings."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def json_array(value: object) -> list[object]:
    """A list, from a list or a JSON-encoded list; anything else is []."""
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []

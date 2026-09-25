"""Coerce share-stage CSV cells at read and write boundaries.

Changelog:
  2026-09-24: created.
"""

from __future__ import annotations

from typing import Any


def cell_text(value: Any) -> str | None:
    """An empty read cell is absent."""
    text = str(value or "").strip()
    return text or None


def cell_value(value: Any) -> Any:
    """Write absent values as blank and true booleans as yes."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else ""
    return value

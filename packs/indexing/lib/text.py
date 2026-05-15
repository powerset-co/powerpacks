"""Deterministic text normalization helpers for indexing scaffolding."""

from __future__ import annotations

import hashlib
import re
from typing import Any

_WS = re.compile(r"\s+")


def normalize_whitespace(value: Any) -> str:
    """Collapse whitespace and strip surrounding space."""

    return _WS.sub(" ", "" if value is None else str(value)).strip()


def normalize_text(value: Any) -> str:
    """Normalize text for deterministic comparisons without stemming or LLMs."""

    return normalize_whitespace(value).lower()


def truncate_text(value: Any, max_chars: int) -> str:
    """Normalize whitespace and truncate at a character boundary."""

    if max_chars < 0:
        raise ValueError("max_chars must be non-negative")
    return normalize_whitespace(value)[:max_chars]


def stable_text_hash(value: Any, *, prefix: str = "txt", length: int = 16) -> str:
    """Return a short deterministic SHA-256 based identifier for text."""

    if length <= 0:
        raise ValueError("length must be positive")
    digest = hashlib.sha256(normalize_whitespace(value).encode("utf-8")).hexdigest()[:length]
    return f"{prefix}:{digest}" if prefix else digest


def slugify(value: Any) -> str:
    text = normalize_text(value)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text


def word_tokens(text: Any) -> list[str]:
    return list(dict.fromkeys(re.findall(r"[a-z0-9]+", normalize_text(text))))


def char_tokens(text: Any) -> list[str]:
    compact = normalize_text(text)
    if not compact:
        return []
    if len(compact) <= 3:
        return [compact]
    return list(dict.fromkeys(compact[i:i+3] for i in range(len(compact)-2)))


def dense_text(parts: Any) -> str:
    if isinstance(parts, dict):
        parts = parts.values()
    if isinstance(parts, (str, bytes)) or parts is None:
        parts = [parts]
    return normalize_whitespace(" ".join(str(p) for p in parts if p not in (None, "", [])))

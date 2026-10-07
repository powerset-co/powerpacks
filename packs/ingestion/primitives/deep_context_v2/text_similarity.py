"""Near-duplicate text similarity: word 3-gram shingles and Jaccard overlap.

Collect drops near-duplicate emails with it; synthesize collapses near-duplicate
facts across one candidate's batches with it.

Created: 2026-10-06
"""
from __future__ import annotations

import re


def shingles(text: str, size: int = 3) -> frozenset[str]:
    """Word n-grams over lowercased alphanumeric tokens; fewer tokens than `size` gives the token set."""
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    if len(tokens) < size:
        return frozenset(tokens)
    grams = []
    for index in range(len(tokens) - size + 1):
        grams.append(" ".join(tokens[index : index + size]))
    return frozenset(grams)


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Intersection over union of two shingle sets; 0.0 if either is empty."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)

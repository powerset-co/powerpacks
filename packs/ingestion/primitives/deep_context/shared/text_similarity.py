"""Shared near-duplicate text similarity: 3-gram shingles + Jaccard overlap.

One home for the near-dup primitive so it is not forked: collection's
`email_context.EmailContext` uses it to drop near-duplicate email snippets
before they ever reach a prompt, and synthesis's `facts.merge_batch_facts`
uses it to collapse near-duplicate paraphrases (the same event/fact described
differently across a person's batches) after the model has already run.
"""

from __future__ import annotations

import re


def shingles(text: str, size: int = 3) -> frozenset[str]:
    """Word n-grams (default 3-gram) over lowercased alphanumeric tokens.

    A text shorter than `size` tokens falls back to its raw token set, so
    short strings still produce a comparable (if coarser) shingle set instead
    of an empty one.
    """
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    if len(tokens) < size:
        return frozenset(tokens)
    return frozenset(
        " ".join(tokens[index : index + size])
        for index in range(len(tokens) - size + 1)
    )


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Intersection-over-union of two shingle sets; 0.0 if either is empty."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)

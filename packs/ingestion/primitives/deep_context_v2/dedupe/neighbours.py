"""Neighbours: pairs of candidates whose names are spelled alike, found without blocking.

Blocking (blocking.py) pairs only names that share a bucket key, so a name spelled two ways
("Nasser AlMutairy" / "Naser Al Mutairi", "Daniel Hilu" saved as "Dany Hilu") never meets. Here
every name is broken into 3-letter chunks, each chunk weighted by how rare it is (TF-IDF, the
chunks of each word padded with a space), and two candidates score the best cosine over their
names. A candidate's nearest one at 0.6 or more and its nearest five at 0.7 or more are pairs.
Local and free; measured on Jake's store it found 90% of known duplicates with the rule pairs,
against 75% for the rules alone.

Before chunking, a name is read as words: accents folded, lowercase, "Last, First" turned around,
titles dropped, a surname particle joined to the next word (al mutairi -> almutairi). A one-word
name is used only when the candidate has no longer one.

Created: 2026-11-02
"""
from __future__ import annotations

import math
import re
import unicodedata

import numpy as np

NEAREST_ONE_FLOOR = 0.6
NEAREST_FIVE_FLOOR = 0.7
CHUNK = 300  # candidates scored per matrix product
TITLES = frozenset({"sheikh", "sheikha", "shaikh", "shaikha", "shiekh", "shk", "sh", "dr", "mr", "mrs", "ms", "miss",
                    "prof", "eng", "he", "hh", "sir", "jr", "sr", "ii", "iii", "phd", "md", "mba", "cpa", "esq"})
PARTICLES = frozenset({"al", "el", "ul", "bin", "ibn", "bint", "abu", "abd", "abdul", "abdel", "abdal", "bu"})
_DROPPED = re.compile(r"['`’.]")
_NOT_LETTERS = re.compile(r"[^a-z]+")


def words(name: str) -> list[str]:
    """One name as the words its chunks come from."""
    folded: str = unicodedata.normalize("NFKD", name)
    text: str = "".join(char for char in folded if not unicodedata.combining(char)).lower()
    if text.count(",") == 1:
        last, first = text.split(",")
        text = f"{first} {last}"
    tokens: list[str] = [token for token in _NOT_LETTERS.sub(" ", _DROPPED.sub("", text)).split() if token not in TITLES]
    joined: list[str] = []
    index: int = 0
    while index < len(tokens):
        token: str = "bin" if tokens[index] == "ibn" else tokens[index]
        if token in PARTICLES and index + 1 < len(tokens):
            joined.append(token + tokens[index + 1])
            index += 2
        else:
            joined.append(token)
            index += 1
    return joined


def chunks(variant: str) -> dict[str, int]:
    """The 3-letter chunks of every word, each word padded with a space, with their counts."""
    counts: dict[str, int] = {}
    for word in variant.split():
        padded: str = f" {word} "
        for start in range(max(1, len(padded) - 2)):
            chunk: str = padded[start:start + 3]
            counts[chunk] = counts.get(chunk, 0) + 1
    return counts


def pairs(names: dict[str, tuple[str, ...]]) -> set[tuple[str, str]]:
    """Every (a, b) with a < b whose names are spelled alike, by the nearest-one and nearest-five rules."""
    ids: list[str] = sorted(names)
    # Each candidate's distinct variants, in candidate order: row r belongs to candidate owner[r].
    rows: list[dict[str, int]] = []
    owner: list[int] = []
    for position, candidate_id in enumerate(ids):
        variants: list[list[str]] = [found for found in (words(name) for name in names[candidate_id]) if found]
        longer: list[list[str]] = [variant for variant in variants if len(variant) > 1]
        if longer:
            variants = longer
        for variant in dict.fromkeys(" ".join(variant) for variant in variants):
            rows.append(chunks(variant))
            owner.append(position)
    if len(set(owner)) < 2:
        return set()
    # TF-IDF: 1 + log count, times the smoothed inverse document frequency, each row of length 1. A
    # chunk in one row only adds to that row's length, so the matrix keeps the shared chunks alone.
    frequency: dict[str, int] = {}
    for row in rows:
        for chunk in row:
            frequency[chunk] = frequency.get(chunk, 0) + 1
    shared: dict[str, int] = {}
    for chunk in sorted(frequency):
        if frequency[chunk] > 1:
            shared[chunk] = len(shared)
    matrix = np.zeros((len(rows), len(shared)), dtype=np.float32)
    for index, row in enumerate(rows):
        weights: dict[str, float] = {}
        for chunk, count in row.items():
            weights[chunk] = (1 + math.log(count)) * (math.log((1 + len(rows)) / (1 + frequency[chunk])) + 1)
        length: float = math.sqrt(sum(weight * weight for weight in weights.values()))
        for chunk, weight in weights.items():
            if chunk in shared:
                matrix[index, shared[chunk]] = weight / length
    # Best cosine between two candidates' variants, a block of candidates at a time; keep the nearest five.
    owners = np.array(owner)
    starts = np.flatnonzero(np.r_[True, owners[1:] != owners[:-1]])
    present: list[str] = [ids[position] for position in owners[starts]]
    found: set[tuple[str, str]] = set()
    nearest: int = min(5, len(present) - 1)
    for first in range(0, len(present), CHUNK):
        last: int = min(len(present), first + CHUNK)
        row_end: int = int(starts[last]) if last < len(present) else len(rows)
        cosines = matrix[starts[first]:row_end] @ matrix.T
        by_column = np.maximum.reduceat(cosines, starts, axis=1)
        scores = np.maximum.reduceat(by_column, starts[first:last] - starts[first], axis=0)
        for offset in range(last - first):
            scores[offset, first + offset] = -1.0
        top = np.argsort(-scores, axis=1)[:, :nearest]
        for offset in range(last - first):
            for rank, column in enumerate(top[offset]):
                score: float = float(scores[offset, column])
                if score >= NEAREST_FIVE_FLOOR or (rank == 0 and score >= NEAREST_ONE_FLOOR):
                    a, b = present[first + offset], present[int(column)]
                    found.add((a, b) if a < b else (b, a))
    return found

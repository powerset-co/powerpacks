"""Blocking: put candidates with similar names into buckets, so dedupe never compares every
candidate against every other. Every two members of a bucket are a blocked pair.

Bucket keys, per written name (lowercased, single-spaced):

    "jordan bravo" -> fnli:jordan|b, filn:j|bravo, words:bravo jordan, ends:jordan|bravo
    "j bravo"      -> fnli:j|b, filn:j|bravo

A one-word name ("Ben") gets no name key: it is never paired by name (decided 2026-10-07).

plus `local:<handle>` from an email candidate's address (the part before @). Phones and whole
emails make no bucket: a candidate is one identifier, so no two candidates share one. A bucket
over 200 members (a very common first name) is skipped and counted.

Most true duplicates land in a name bucket together. A few stragglers will not (a nickname with
a different surname spelling, an email saved under a company name); we accept that in exchange
for never comparing every candidate against every other.

Created: 2026-10-06
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context_v2.db.queries_dedupe import Identifier
from packs.ingestion.primitives.deep_context_v2.db.schema import IdentifierKind
from packs.ingestion.primitives.deep_context_v2.names import name_words

MAX_BUCKET = 200
BUCKET_KINDS: tuple[str, ...] = ("fnli", "filn", "words", "ends", "local")
_JOINERS = re.compile(r"[.\-']+")
_NOT_LETTERS = re.compile(r"[^a-z ]+")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class KindStats:
    """One bucket kind: buckets with two or more members, their mean and largest size, and how many
    members sat in buckets too large to pair."""

    buckets: int
    mean_size: float
    max_size: int
    members_in_skipped: int


@dataclass(frozen=True)
class Blocking:
    pairs: list[tuple[str, str]]   # each (a, b) with a < b, sorted
    bucketed: int                  # candidates with at least one key
    stats: dict[str, KindStats]


def name_keys(name: str) -> set[str]:
    """The name bucket keys of one written name."""
    key: str = _SPACES.sub(" ", name.strip().lower())
    tokens: list[str] = _NOT_LETTERS.sub(" ", _JOINERS.sub("", key)).split()
    keys: set[str] = set()
    # First name + last initial, first initial + last name. One word is no name to bucket on.
    if len(tokens) >= 2:
        first: str = tokens[0]
        last: str = tokens[-1]
        keys.add(f"fnli:{first}|{last[0]}")
        keys.add(f"filn:{first[0]}|{last}")
    # A full name also buckets on all its words in any order and on its first and last word.
    words: tuple[str, ...] = name_words(key)
    if len(words) > 1 and len(words[0]) > 1 and len(words[-1]) > 1:
        keys.add("words:" + " ".join(sorted(words)))
        keys.add(f"ends:{words[0]}|{words[-1]}")
    return keys


def block(candidate_ids: list[str], names: dict[str, list[str]], identifiers: dict[str, list[Identifier]]) -> Blocking:
    """Bucket every given candidate by its names and email handle; pair every two members of a bucket."""
    # Fill the buckets.
    buckets: dict[str, list[str]] = {}
    bucketed: int = 0
    for candidate_id in candidate_ids:
        keys: set[str] = set()
        for name in names[candidate_id]:
            keys.update(name_keys(name))
        for identifier in identifiers[candidate_id]:
            if identifier.kind == IdentifierKind.EMAIL:
                keys.add("local:" + identifier.normalized_value.split("@", 1)[0])
        if keys:
            bucketed += 1
        for key in sorted(keys):
            buckets.setdefault(key, []).append(candidate_id)
    # Pair inside each bucket of two or more, skipping the oversized ones; tally per kind.
    sizes: dict[str, list[int]] = {}
    skipped: dict[str, int] = {}
    for kind in BUCKET_KINDS:
        sizes[kind] = []
        skipped[kind] = 0
    pairs: set[tuple[str, str]] = set()
    for key, members in buckets.items():
        kind: str = key.split(":", 1)[0]
        if len(members) < 2:
            continue
        if len(members) > MAX_BUCKET:
            skipped[kind] += len(members)
            continue
        sizes[kind].append(len(members))
        ordered: list[str] = sorted(members)
        for index, first in enumerate(ordered):
            for second in ordered[index + 1:]:
                pairs.add((first, second))
    stats: dict[str, KindStats] = {}
    for kind in BUCKET_KINDS:
        mean: float = 0.0
        largest: int = 0
        if sizes[kind]:
            mean = round(sum(sizes[kind]) / len(sizes[kind]), 2)
            largest = max(sizes[kind])
        stats[kind] = KindStats(len(sizes[kind]), mean, largest, skipped[kind])
    return Blocking(sorted(pairs), bucketed, stats)

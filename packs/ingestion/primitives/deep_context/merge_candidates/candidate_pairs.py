"""Generate plausible identity pairs without comparing every parent to every other.

"Blocking" is the record-linkage term for bucketing records on shared keys so
candidate generation avoids an O(N^2) all-pairs comparison. Name keys use:

    "jordan bravo" -> {"fnli:jordan|b", "filn:j|bravo"}
    "j bravo"      -> {"fnli:j|b", "filn:j|bravo", "fn:j"}

The one-character surname also buckets on first name, and both examples land in
``filn:j|bravo``. Complete bucket keys look like
``email:casey@example.com``, ``local:casey``, ``phone:15550100``, and
``nm:filn:j|bravo``.

Jaro-Winkler follows Winkler's Census record-linkage definition. Its prefix
weighting is a better fit than Levenshtein distance for given-name spelling and
nickname candidates; reference-value tests pin this local implementation.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from itertools import combinations
from typing import TypeVar

from packs.ingestion.primitives.common.contact_fields import format_phone_digits
from packs.ingestion.primitives.deep_context.merge_candidates.models import (
    MergeDecision,
    MergePair,
    MergePerson,
)

# This is the original merge-stage recall gate, not an acceptance threshold.
# No calibration corpus was recorded; changing it changes which pairs reach the judge.
GATE_NAME_SIM = 0.85
MAX_BLOCKING_BUCKET = 200
JUDGE_SLAM_DUNK = "slam_dunk"
T = TypeVar("T")


@dataclass(frozen=True)
class _BlockingRecord:
    person: MergePerson
    emails: frozenset[str]
    email_localparts: frozenset[str]
    phones: frozenset[str]
    bucket_keys: frozenset[str]


def jaro(first: str, second: str) -> float:
    """Return the Jaro similarity from the Winkler Census specification."""
    if first == second:
        return 1.0
    if not first or not second:
        return 0.0
    match_distance = max(len(first), len(second)) // 2 - 1
    first_matches = [False] * len(first)
    second_matches = [False] * len(second)
    matches = 0
    for index, character in enumerate(first):
        lower = max(0, index - match_distance)
        upper = min(index + match_distance + 1, len(second))
        for other in range(lower, upper):
            if not second_matches[other] and second[other] == character:
                first_matches[index] = second_matches[other] = True
                matches += 1
                break
    if not matches:
        return 0.0
    transpositions = other = 0
    for index, matched in enumerate(first_matches):
        if matched:
            while not second_matches[other]:
                other += 1
            if first[index] != second[other]:
                transpositions += 1
            other += 1
    transpositions //= 2
    return (
        matches / len(first)
        + matches / len(second)
        + (matches - transpositions) / matches
    ) / 3


def jaro_winkler(first: str, second: str, prefix_weight: float = 0.1) -> float:
    """Return Jaro-Winkler similarity with the standard four-character prefix cap."""
    base = jaro(first, second)
    prefix = 0
    for left, right in zip(first, second):
        if left == right and prefix < 4:
            prefix += 1
        else:
            break
    return base + prefix * prefix_weight * (1 - base)


def email_localparts(emails: tuple[str, ...]) -> frozenset[str]:
    return frozenset(email.split("@", 1)[0] for email in emails if "@" in email)


def blocking_name_keys(name_key: str) -> set[str]:
    """Return first-name/last-initial and first-initial/last-name bucket keys."""
    joined = re.sub(r"[.\-']+", "", name_key)
    tokens = re.sub(r"[^a-z ]+", " ", joined).split()
    if not tokens:
        return set()
    first, last = tokens[0], tokens[-1]
    keys = {f"fnli:{first}|{last[0]}", f"filn:{first[0]}|{last}"}
    if len(tokens) == 1 or len(last) == 1:
        keys.add(f"fn:{first}")
    return keys


def _blocking_record(person: MergePerson) -> _BlockingRecord:
    localparts = email_localparts(person.emails)
    keys = {f"email:{email}" for email in person.all_emails}
    keys |= {f"local:{part}" for part in localparts}
    keys |= {f"phone:{digits}" for digits in person.all_phones}
    keys |= {f"nm:{key}" for key in blocking_name_keys(person.name_key)}
    return _BlockingRecord(
        person,
        person.all_emails,
        localparts,
        person.all_phones,
        frozenset(keys),
    )


def generate_pairs(people: list[MergePerson]) -> list[MergePair]:
    """Block parent rows, then retain pairs sharing identity evidence or a similar name."""
    records = [_blocking_record(person) for person in people]
    buckets: dict[str, list[int]] = {}
    for index, record in enumerate(records):
        for key in record.bucket_keys:
            buckets.setdefault(key, []).append(index)
    candidates: set[tuple[int, int]] = set()
    for key, members in buckets.items():
        if len(members) < 2:
            continue
        if len(members) > MAX_BLOCKING_BUCKET:
            # A common-name bucket above 200 would create over 19,900 comparisons.
            # Skip it to bound judge spend, but emit a PII-safe signal rather than
            # silently making those records disappear from candidate generation.
            kind = key.partition(":")[0]
            print(
                f"[cluster] skipped {kind} blocking bucket with {len(members)} members "
                f"(cap {MAX_BLOCKING_BUCKET})",
                file=sys.stderr,
            )
            continue
        candidates.update(combinations(members, 2))
    selected: list[MergePair] = []
    for left_index, right_index in sorted(candidates):
        left, right = records[left_index], records[right_index]
        if (
            left.emails & right.emails
            or left.email_localparts & right.email_localparts
            or left.phones & right.phones
            or jaro_winkler(left.person.name_key, right.person.name_key) >= GATE_NAME_SIM
        ):
            selected.append(MergePair(left.person, right.person))
    return selected


def slam_dunk_verdict(
    first: MergePerson,
    second: MergePerson,
) -> MergeDecision | None:
    if not first.name_key or first.name_key != second.name_key:
        return None
    phones = sorted(first.all_phones & second.all_phones)
    emails = sorted(first.all_emails & second.all_emails)
    if not phones and not emails:
        return None
    shared = ", ".join([format_phone_digits(digits) for digits in phones] + emails)
    return MergeDecision(
        same_person=True,
        confidence=0.99,
        tone_consistent=True,
        judge=JUDGE_SLAM_DUNK,
        reason=f"slam dunk: identical name + shared {shared}",
    )


def connected_components(nodes: list[T], edges: list[tuple[T, T]]) -> list[list[T]]:
    parents = {node: node for node in nodes}

    def find(node: T) -> T:
        while parents[node] != node:
            parents[node] = parents[parents[node]]
            node = parents[node]
        return node

    for left, right in edges:
        parents[find(left)] = find(right)
    groups: dict[T, list[T]] = {}
    for node in nodes:
        groups.setdefault(find(node), []).append(node)
    return [group for group in groups.values() if len(group) > 1]

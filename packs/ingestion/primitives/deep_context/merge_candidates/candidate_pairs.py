"""Generate the identity pairs worth a decision without comparing every parent to every other.

"Blocking" is the record-linkage term for bucketing records on shared keys so
candidate generation avoids an O(N^2) all-pairs comparison. Name keys use:

    "jordan bravo" -> {"fnli:jordan|b", "filn:j|bravo"}
    "j bravo"      -> {"fnli:j|b", "filn:j|bravo", "fn:j"}

The one-character surname also buckets on first name, and both examples land in
``filn:j|bravo``. A name of two or more words also buckets on all its words in
any order and on its first and last word, so "Bravo, Jordan" meets
"Jordan Bravo" and "Jordan Alex Bravo". Complete bucket keys look like
``email:casey@example.com``, ``local:casey``, ``phone:15550100``, and
``nm:filn:j|bravo``.

Name compatibility only selects pairs; it never establishes identity. An
identical normalized name and a shared source contact email or phone is the
only free merge. All other selected pairs need a positive identity judgment.
Extracted identifier claims do not create pairs. A source email or phone can
propose a pair only when the names can match; a shared office number cannot
join two incompatible staff names.

One-word names meet full names only through source email handles or source
contact identifiers. A first name alone could name many contacts.

Jaro-Winkler follows Winkler's Census record-linkage definition. Its prefix
weighting is a better fit than Levenshtein distance for given-name spelling
variants; reference-value tests pin this local implementation.

Changelog:
- 2026-10-02: source identifiers and compatible names select pairs; the same
  name alone never accepts one.
- 2026-10-01: the same name is a merge without the pair judge. A pair is kept
  on a shared phone or email or on names that can be forms of each other;
  whole-name similarity and a shared email handle alone no longer keep one.
"""
from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass
from itertools import combinations
from typing import TypeVar

from packs.ingestion.primitives.common.contact_fields import format_phone_digits
from packs.ingestion.primitives.deep_context.merge_candidates.models import (
    MergeDecision,
    MergePair,
    MergePerson,
)

# How alike two first names or two last names must spell to be one name's variants.
# A recall gate, not an acceptance threshold: changing it changes which pairs reach the judge.
GATE_NAME_SIM = 0.85
MAX_BLOCKING_BUCKET = 200
JUDGE_SLAM_DUNK = "slam_dunk"
SAME_FULL_NAME = "same full name"
SAME_FIRST_AND_LAST_NAME = "same first and last name, middle names do not differ"
# A father and a son: a name carrying one of these is not the same name as one without it.
GENERATION_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})
TITLES = frozenset({"dr", "mr", "mrs", "ms", "prof"})
T = TypeVar("T")


@dataclass(frozen=True)
class _BlockingRecord:
    person: MergePerson
    name_words: tuple[str, ...]
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


def name_words(name_key: str) -> tuple[str, ...]:
    """The words of a name, given name first and titles left out: "bravo, dr jordan" reads as jordan bravo.

    An email address saved as the name has no words. An apostrophe does not
    split a word: o'bravo is one word, not an initial and a name.
    """
    if "@" in name_key:
        return ()
    # Composed and decomposed accents are one spelling.
    composed = re.sub(r"['\u2019]", "", unicodedata.normalize("NFC", name_key))
    family, comma, given = composed.partition(",")
    ordered = f"{given} {family}" if comma else composed
    return tuple(word for word in re.findall(r"[^\W\d_]+", ordered.casefold()) if word not in TITLES)


def _is_full_name(words: tuple[str, ...]) -> bool:
    """A first and a last word that are both spelled out, not initials."""
    return len(words) > 1 and len(words[0]) > 1 and len(words[-1]) > 1


def _middle_names_agree(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    """Missing on one side, or word for word equal or an initial of the other."""
    if not first or not second:
        return True
    if len(first) != len(second):
        return False
    return all(
        left == right or (1 in (len(left), len(right)) and left[0] == right[0])
        for left, right in zip(first, second)
    )


def same_name_reason(first: tuple[str, ...], second: tuple[str, ...]) -> str | None:
    """Why two names' words are one contact's name, or None when the names do not settle it."""
    if not _is_full_name(first) or not _is_full_name(second):
        return None
    if GENERATION_SUFFIXES & set(first) != GENERATION_SUFFIXES & set(second):
        return None
    if sorted(first) == sorted(second):
        return SAME_FULL_NAME
    if (first[0], first[-1]) != (second[0], second[-1]):
        return None
    if not _middle_names_agree(first[1:-1], second[1:-1]):
        return None
    return SAME_FIRST_AND_LAST_NAME


def _word_forms_match(first: str, second: str) -> bool:
    """One word equals, begins or nearly spells the other: jordan/j, ben/benjamin, jon/john."""
    return first.startswith(second) or second.startswith(first) or jaro_winkler(first, second) >= GATE_NAME_SIM


def names_can_match(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    """Can one name be a form of the other? Decides which pairs are worth a judgment, not the judgment."""
    if not first or not second:
        return False
    if same_name_reason(first, second) or first == second:
        return True
    short, long = sorted((first, second), key=len)
    if len(short) == 1:
        return short[0] in (long[0], long[-1])
    return _word_forms_match(first[0], second[0]) and _word_forms_match(first[-1], second[-1])


def blocking_name_keys(name_key: str) -> set[str]:
    """Return the name bucket keys: first/last initial pairs, plus whole-name keys for a full name."""
    joined = re.sub(r"[.\-']+", "", name_key)
    tokens = re.sub(r"[^a-z ]+", " ", joined).split()
    keys: set[str] = set()
    if tokens:
        first, last = tokens[0], tokens[-1]
        keys |= {f"fnli:{first}|{last[0]}", f"filn:{first[0]}|{last}"}
        if len(tokens) == 1 or len(last) == 1:
            keys.add(f"fn:{first}")
    words = name_words(name_key)
    if _is_full_name(words):
        keys |= {f"words:{' '.join(sorted(words))}", f"ends:{words[0]}|{words[-1]}"}
    return keys


def _blocking_record(person: MergePerson) -> _BlockingRecord:
    keys = {f"email:{email}" for email in person.emails}
    keys |= {f"local:{part}" for part in email_localparts(person.emails)}
    keys |= {f"phone:{digits}" for digits in person.phone_digits}
    keys |= {f"nm:{key}" for key in blocking_name_keys(person.name_key)}
    return _BlockingRecord(
        person,
        name_words(person.name_key),
        frozenset(keys),
    )


def generate_pairs(people: list[MergePerson]) -> list[MergePair]:
    """Pair compatible names through name buckets or source contact identifiers."""
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
        if names_can_match(left.name_words, right.name_words):
            selected.append(MergePair(left.person, right.person))
    return selected


def _shared_contact_identifiers(first: MergePerson, second: MergePerson) -> str:
    phones = sorted(set(first.phone_digits) & set(second.phone_digits))
    emails = sorted(set(first.emails) & set(second.emails))
    return ", ".join([format_phone_digits(digits) for digits in phones] + emails)


def slam_dunk_verdict(
    first: MergePerson,
    second: MergePerson,
) -> MergeDecision | None:
    """Merge an identical name with a shared source contact phone or email."""
    shared = _shared_contact_identifiers(first, second)
    if shared and first.name_key and first.name_key == second.name_key:
        return MergeDecision(
            same_person=True,
            confidence=0.99,
            tone_consistent=True,
            judge=JUDGE_SLAM_DUNK,
            reason=f"slam dunk: identical name + shared {shared}",
        )
    return None


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


def accepted_edges(
    verdicts: list[tuple[str, str, bool, float]],
) -> list[tuple[str, str]]:
    """Join strongest pairs without overriding different-person evidence."""
    groups = {node: {node} for left, right, _, _ in verdicts for node in (left, right)}
    rejected = [(left, right) for left, right, same, _ in verdicts if not same]
    accepted = []
    for left, right, same, _ in sorted(
        verdicts, key=lambda row: (-row[3], min(row[:2]), max(row[:2])),
    ):
        if not same:
            continue
        joined = groups[left] | groups[right]
        if any(a in joined and b in joined for a, b in rejected):
            continue
        accepted.append(tuple(sorted((left, right))))
        for node in joined:
            groups[node] = joined
    return accepted

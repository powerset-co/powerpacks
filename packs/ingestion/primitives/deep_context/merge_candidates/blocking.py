"""Deterministic pair blocking, slam-dunk decisions, and clustering."""
from __future__ import annotations

import re
from itertools import combinations
from typing import TypeVar

from packs.ingestion.primitives.deep_context.merge_candidates.models import (
    MergePerson,
    all_emails,
    all_phones,
    fmt_phone,
)

GATE_NAME_SIM = 0.85
JUDGE_SLAM_DUNK = "slam_dunk"
T = TypeVar("T")


def jaro(first: str, second: str) -> float:
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
    base = jaro(first, second)
    prefix = 0
    for left, right in zip(first, second):
        if left == right and prefix < 4:
            prefix += 1
        else:
            break
    return base + prefix * prefix_weight * (1 - base)


def email_localparts(emails: tuple[str, ...]) -> set[str]:
    return {email.split("@", 1)[0] for email in emails if "@" in email}


def blocking_name_keys(name_key: str) -> set[str]:
    joined = re.sub(r"[.\-']+", "", name_key)
    tokens = re.sub(r"[^a-z ]+", " ", joined).split()
    if not tokens:
        return set()
    first, last = tokens[0], tokens[-1]
    keys = {f"fnli:{first}|{last[0]}", f"filn:{first[0]}|{last}"}
    if len(tokens) == 1 or len(last) == 1:
        keys.add(f"fn:{first}")
    return keys


def generate_pairs(people: list[MergePerson]) -> set[tuple[int, int]]:
    buckets: dict[str, list[int]] = {}
    for index, person in enumerate(people):
        keys = {f"email:{email}" for email in all_emails(person)}
        keys |= {f"local:{part}" for part in email_localparts(person.emails)}
        keys |= {f"phone:{digits}" for digits in all_phones(person)}
        keys |= {f"nm:{key}" for key in blocking_name_keys(person.name_key)}
        for key in keys:
            buckets.setdefault(key, []).append(index)
    candidates: set[tuple[int, int]] = set()
    for members in buckets.values():
        if len(members) < 2 or len(members) > 200:
            continue
        candidates.update(combinations(members, 2))
    return {
        (left, right)
        for left, right in candidates
        if (
            all_emails(people[left]) & all_emails(people[right])
            or email_localparts(people[left].emails) & email_localparts(people[right].emails)
            or all_phones(people[left]) & all_phones(people[right])
            or jaro_winkler(people[left].name_key, people[right].name_key) >= GATE_NAME_SIM
        )
    }


def slam_dunk_verdict(first: MergePerson, second: MergePerson) -> dict | None:
    if not first.name_key or first.name_key != second.name_key:
        return None
    phones = sorted(all_phones(first) & all_phones(second))
    emails = sorted(all_emails(first) & all_emails(second))
    if not phones and not emails:
        return None
    shared = ", ".join([fmt_phone(digits) for digits in phones] + emails)
    return {
        "same_person": True,
        "confidence": 0.99,
        "tone_toward_a": "",
        "tone_toward_b": "",
        "tone_consistent": True,
        "judge": JUDGE_SLAM_DUNK,
        "reason": f"deterministic: identical name + shared {shared}",
    }


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

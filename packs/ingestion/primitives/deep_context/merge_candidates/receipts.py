"""SQLite-backed merge survey cache plus human-readable result exports."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.models import MergeVerdictRow
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.merge_candidates.blocking import (
    JUDGE_SLAM_DUNK,
    connected_components,
    generate_pairs,
    slam_dunk_verdict,
)
from packs.ingestion.primitives.deep_context.merge_candidates.judge import (
    JUDGE_LLM,
    JUDGE_SYSTEM,
)
from packs.ingestion.primitives.deep_context.merge_candidates.models import (
    MergePerson,
    all_emails,
    all_phones,
    load_people,
)
from packs.shared.csv_io import CsvIO

IDENTITY_CONTRACT_VERSION = "owned-identifiers-v1"
REUSABLE_JUDGES = frozenset({JUDGE_SLAM_DUNK, JUDGE_LLM})
_JUDGE_VERSION = hashlib.sha1(
    f"{IDENTITY_CONTRACT_VERSION}\x1e{JUDGE_SYSTEM}".encode("utf-8")
).hexdigest()[:8]


@dataclass(frozen=True)
class PairSurvey:
    people: list[MergePerson]
    pairs: list[tuple[int, int]]
    slam: list[tuple[int, int, dict[str, Any]]]
    shared_unsettled: list[tuple[int, int]]
    reused: list[tuple[int, int, str, dict[str, Any]]]
    to_judge: list[tuple[int, int, str]]


def person_sig(person: MergePerson) -> str:
    profile = person.evidence
    return "\x1f".join([
        person.name_key,
        "|".join(sorted(all_emails(person))),
        "|".join(sorted(all_phones(person))),
        profile.relationship,
        profile.title,
        "|".join(sorted(profile.employers)),
        profile.school,
        profile.location,
        "|".join(sorted(profile.topics)),
    ])


def pair_sig(first: MergePerson, second: MergePerson) -> str:
    left, right = sorted([person_sig(first), person_sig(second)])
    return hashlib.sha1(
        f"{_JUDGE_VERSION}\x1e{left}\x1e{right}".encode("utf-8")
    ).hexdigest()[:16]


def load_cached_verdicts(
    rows: tuple[MergeVerdictRow, ...],
    parent_by_person: dict[str, str] | None = None,
) -> dict[frozenset[str], tuple[str, dict[str, Any]]]:
    """Resolve schema-v8 child anchors into the current logical parent-pair cache.

    The frozen v8 table must retain child FKs so BuildParents can consume an
    accepted representative edge. Callers key reuse only by the unordered
    current parent pair plus ``signature``; representative child ids are an
    intentionally hidden storage compatibility detail.
    """
    cache: dict[frozenset[str], tuple[str, dict[str, Any]]] = {}
    updated: dict[frozenset[str], str] = {}
    parents = parent_by_person or {}
    for row in rows:
        judge = row.judge.strip().lower() or JUDGE_LLM
        signature = row.signature
        if judge not in REUSABLE_JUDGES or not signature:
            continue
        key = frozenset({
            parents.get(row.person_a, row.person_a),
            parents.get(row.person_b, row.person_b),
        })
        if len(key) != 2 or (row.updated_at or "") < updated.get(key, ""):
            continue
        updated[key] = row.updated_at or ""
        cache[key] = (
            signature,
            {
                "same_person": bool(row.same_person),
                "confidence": row.confidence,
                "tone_consistent": bool(row.tone_consistent),
                "reason": row.reason,
                "judge": judge,
            },
        )
    return cache


def split_cached_pairs(
    pairs: list[tuple[int, int]],
    people: list[MergePerson],
    cache: dict[frozenset[str], tuple[str, dict[str, Any]]],
) -> tuple[list[tuple[int, int, str, dict[str, Any]]], list[tuple[int, int, str]]]:
    reused: list[tuple[int, int, str, dict[str, Any]]] = []
    to_judge: list[tuple[int, int, str]] = []
    for left, right in pairs:
        signature = pair_sig(people[left], people[right])
        hit = cache.get(frozenset({
            people[left].parent_id or people[left].person_id,
            people[right].parent_id or people[right].person_id,
        }))
        if hit and hit[0] == signature:
            reused.append((left, right, signature, hit[1]))
        else:
            to_judge.append((left, right, signature))
    return reused, to_judge


def survey_pairs(db: Db, *, refresh: bool = False) -> PairSurvey:
    people = load_people(db)
    pairs = sorted(generate_pairs(people))
    slam: list[tuple[int, int, dict[str, Any]]] = []
    shared_unsettled: list[tuple[int, int]] = []
    rest: list[tuple[int, int]] = []
    for left, right in pairs:
        verdict = slam_dunk_verdict(people[left], people[right])
        if verdict:
            slam.append((left, right, verdict))
        elif (
            set(people[left].emails) & set(people[right].emails)
            or set(people[left].phone_digits) & set(people[right].phone_digits)
        ):
            # Observed identifiers should already have canonicalized the family.
            # Preserve the existing deterministic rule; never pay to second-guess it.
            shared_unsettled.append((left, right))
        else:
            rest.append((left, right))
    snapshot = canonical_snapshot(db)
    parent_by_person = {row.person_id: row.parent_id for row in snapshot.people}
    cache = {} if refresh else load_cached_verdicts(
        snapshot.merge_verdicts, parent_by_person,
    )
    reused, to_judge = split_cached_pairs(rest, people, cache)
    return PairSurvey(people, pairs, slam, shared_unsettled, reused, to_judge)


def verdict_rows(
    people: list[MergePerson], verdicts: list[dict[str, Any]], confidence: float,
) -> tuple[MergeVerdictRow, ...]:
    rows = []
    for verdict in verdicts:
        first, second = people[verdict["a"]], people[verdict["b"]]
        if first.person_id > second.person_id:
            first, second = second, first
        score = float(verdict.get("confidence") or 0)
        same = bool(verdict.get("same_person"))
        rows.append(MergeVerdictRow(
            first.person_id,
            second.person_id,
            first.slug,
            second.slug,
            str(verdict.get("sig") or ""),
            str(verdict.get("judge") or JUDGE_LLM),
            int(same),
            score,
            int(bool(verdict.get("tone_consistent"))),
            str(verdict.get("reason") or ""),
            int(same and score >= confidence),
            now_iso(),
        ))
    return tuple(sorted(rows, key=lambda row: (row.person_a, row.person_b)))


def _confirmed(
    people: list[MergePerson], verdicts: list[dict[str, Any]], confidence: float,
) -> tuple[list[dict[str, Any]], list[list[int]]]:
    edges: list[tuple[int, int]] = []
    rows: list[dict[str, Any]] = []
    for verdict in verdicts:
        if not verdict.get("same_person") or float(verdict.get("confidence") or 0) < confidence:
            continue
        left, right = verdict["a"], verdict["b"]
        edges.append((left, right))
        rows.append({
            "slug_a": people[left].slug,
            "name_a": people[left].name,
            "slug_b": people[right].slug,
            "name_b": people[right].name,
            "confidence": round(float(verdict.get("confidence") or 0), 3),
            "tone_consistent": verdict.get("tone_consistent"),
            "reason": verdict.get("reason", ""),
        })
    rows.sort(key=lambda row: row["confidence"], reverse=True)
    return rows, connected_components(list(range(len(people))), edges)


def render_results(
    *, out_csv: Path, out_md: Path, people: list[MergePerson],
    verdicts: list[dict[str, Any]], confidence: float,
) -> tuple[list[dict[str, Any]], list[list[int]]]:
    """Write display exports only; SQLite remains the graph/cache authority."""
    confirmed, clusters = _confirmed(people, verdicts, confidence)
    CsvIO.write_dict_rows(out_csv, [
        "slug_a", "name_a", "slug_b", "name_b",
        "confidence", "tone_consistent", "reason",
    ], confirmed)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Merge candidates ({len(clusters)} clusters, {len(confirmed)} pairs)",
        "",
        f"_Generated {now_iso()}. LLM-judged on tone + identity. Confirm before merging._",
        "",
    ]
    for number, group in enumerate(clusters, 1):
        lines.append(f"## Cluster {number}")
        lines.extend(
            f"- [[{people[index].slug}]] **{people[index].name}**"
            for index in group
        )
        lines.append("")
    if not clusters:
        lines.append("_No merge candidates confirmed._")
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return confirmed, clusters

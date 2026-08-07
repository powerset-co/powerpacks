"""SQLite-backed merge survey cache plus human-readable result exports."""

from __future__ import annotations

import hashlib
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp, MergeVerdictRow
from packs.ingestion.primitives.deep_context.db.queries import merge_verdicts, people as person_rows
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
    CachedMergeVerdict,
    ConfirmedMergeRow,
    MergeDecision,
    MergePairVerdict,
    MergePerson,
    PairSurvey,
    all_emails,
    all_phones,
    load_people,
)
from packs.shared.csv_io import CsvIO

IDENTITY_CONTRACT_VERSION = "owned-identifiers-v1"
REUSABLE_JUDGES = frozenset({JUDGE_SLAM_DUNK, JUDGE_LLM})
_JUDGE_VERSION = hashlib.sha1(f"{IDENTITY_CONTRACT_VERSION}\x1e{JUDGE_SYSTEM}".encode("utf-8")).hexdigest()[:8]


def person_sig(person: MergePerson) -> str:
    profile = person.evidence
    return "\x1f".join(
        [
            person.name_key,
            "|".join(sorted(all_emails(person))),
            "|".join(sorted(all_phones(person))),
            profile.relationship,
            profile.title,
            "|".join(sorted(profile.employers)),
            profile.school,
            profile.location,
            "|".join(sorted(profile.topics)),
        ]
    )


def pair_sig(first: MergePerson, second: MergePerson) -> str:
    left, right = sorted([person_sig(first), person_sig(second)])
    return hashlib.sha1(f"{_JUDGE_VERSION}\x1e{left}\x1e{right}".encode("utf-8")).hexdigest()[:16]


def load_cached_verdicts(
    rows: tuple[MergeVerdictRow, ...],
    parent_by_person: dict[str, str] | None = None,
) -> dict[frozenset[str], CachedMergeVerdict]:
    """Return reusable verdicts keyed by current parent pair and signature."""
    cache: dict[frozenset[str], CachedMergeVerdict] = {}
    updated: dict[frozenset[str], IsoTimestamp] = {}
    parents = parent_by_person or {}
    for row in rows:
        judge = row.judge.strip().lower() or JUDGE_LLM
        signature = row.signature
        if judge not in REUSABLE_JUDGES or not signature:
            continue
        key = frozenset(
            {
                parents.get(row.person_a, row.person_a),
                parents.get(row.person_b, row.person_b),
            }
        )
        if len(key) != 2 or (row.updated_at or "") < updated.get(key, ""):
            continue
        updated[key] = row.updated_at or ""
        cache[key] = CachedMergeVerdict(
            signature,
            MergeDecision(
                same_person=bool(row.same_person),
                confidence=row.confidence,
                tone_consistent=bool(row.tone_consistent),
                reason=row.reason,
                judge=judge,
            ),
        )
    return cache


def split_cached_pairs(
    pairs: list[tuple[int, int]],
    people: list[MergePerson],
    cache: dict[frozenset[str], CachedMergeVerdict],
) -> tuple[list[MergePairVerdict], list[tuple[int, int, str]]]:
    reused: list[MergePairVerdict] = []
    to_judge: list[tuple[int, int, str]] = []
    for left, right in pairs:
        signature = pair_sig(people[left], people[right])
        hit = cache.get(
            frozenset(
                {
                    people[left].parent_id or people[left].person_id,
                    people[right].parent_id or people[right].person_id,
                }
            )
        )
        if hit and hit.signature == signature:
            reused.append(MergePairVerdict(left, right, signature, hit.decision))
        else:
            to_judge.append((left, right, signature))
    return reused, to_judge


def survey_pairs(db: Db, *, refresh: bool = False) -> PairSurvey:
    people = load_people(db)
    pairs = sorted(generate_pairs(people))
    slam: list[tuple[int, int, MergeDecision]] = []
    shared_unsettled: list[tuple[int, int]] = []
    rest: list[tuple[int, int]] = []
    for left, right in pairs:
        verdict = slam_dunk_verdict(people[left], people[right])
        if verdict:
            slam.append((left, right, verdict))
        elif set(people[left].emails) & set(people[right].emails) or set(people[left].phone_digits) & set(
            people[right].phone_digits
        ):
            # Observed identifiers should already have canonicalized the family.
            # Preserve the existing deterministic rule; never pay to second-guess it.
            shared_unsettled.append((left, right))
        else:
            rest.append((left, right))
    parent_by_person = {row.person_id: row.parent_id for row in person_rows(db)}
    cache = (
        {}
        if refresh
        else load_cached_verdicts(
            merge_verdicts(db),
            parent_by_person,
        )
    )
    reused, to_judge = split_cached_pairs(rest, people, cache)
    return PairSurvey(people, pairs, slam, shared_unsettled, reused, to_judge)


def verdict_rows(
    people: list[MergePerson],
    verdicts: list[MergePairVerdict],
    confidence: float,
) -> tuple[MergeVerdictRow, ...]:
    rows = []
    for verdict in verdicts:
        first, second = people[verdict.left], people[verdict.right]
        if first.person_id > second.person_id:
            first, second = second, first
        score = verdict.decision.confidence
        same = verdict.decision.same_person
        rows.append(
            MergeVerdictRow(
                first.person_id,
                second.person_id,
                first.slug,
                second.slug,
                verdict.signature,
                verdict.decision.judge or JUDGE_LLM,
                same,
                score,
                verdict.decision.tone_consistent,
                verdict.decision.reason,
                same and score >= confidence,
                now_iso(),
            )
        )
    return tuple(sorted(rows, key=lambda row: (row.person_a, row.person_b)))


def _confirmed(
    people: list[MergePerson],
    verdicts: list[MergePairVerdict],
    confidence: float,
) -> tuple[list[ConfirmedMergeRow], list[list[int]]]:
    edges: list[tuple[int, int]] = []
    rows: list[ConfirmedMergeRow] = []
    for verdict in verdicts:
        decision = verdict.decision
        if not decision.same_person or decision.confidence < confidence:
            continue
        left, right = verdict.left, verdict.right
        edges.append((left, right))
        rows.append(
            ConfirmedMergeRow(
                people[left].slug,
                people[left].name,
                people[right].slug,
                people[right].name,
                round(decision.confidence, 3),
                decision.tone_consistent,
                decision.reason,
            )
        )
    rows.sort(key=lambda row: row.confidence, reverse=True)
    return rows, connected_components(list(range(len(people))), edges)


def render_results(
    *,
    out_csv: Path,
    out_md: Path,
    people: list[MergePerson],
    verdicts: list[MergePairVerdict],
    confidence: float,
) -> tuple[list[ConfirmedMergeRow], list[list[int]]]:
    """Write display exports only; SQLite remains the graph/cache authority."""
    confirmed, clusters = _confirmed(people, verdicts, confidence)
    CsvIO.write_dict_rows(
        out_csv,
        [
            "slug_a",
            "name_a",
            "slug_b",
            "name_b",
            "confidence",
            "tone_consistent",
            "reason",
        ],
        [row.csv_dict() for row in confirmed],
    )
    out_md.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Merge candidates ({len(clusters)} clusters, {len(confirmed)} pairs)",
        "",
        f"_Generated {now_iso()}. LLM-judged on tone + identity. Confirm before merging._",
        "",
    ]
    for number, group in enumerate(clusters, 1):
        lines.append(f"## Cluster {number}")
        lines.extend(f"- [[{people[index].slug}]] **{people[index].name}**" for index in group)
        lines.append("")
    if not clusters:
        lines.append("_No merge candidates confirmed._")
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return confirmed, clusters

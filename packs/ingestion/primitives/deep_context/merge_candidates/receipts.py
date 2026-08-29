"""SQLite-backed merge survey cache plus human-readable result exports."""

from __future__ import annotations

import hashlib
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp, MergeVerdictRow
from packs.ingestion.primitives.deep_context.db.merge_queries import merge_people
from packs.ingestion.primitives.deep_context.db.queries import merge_verdicts, people as person_rows
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import (
    connected_components,
    generate_pairs,
    slam_dunk_verdict,
)
from packs.ingestion.primitives.deep_context.merge_candidates.judge import JUDGE_SYSTEM
from packs.ingestion.primitives.deep_context.merge_candidates.models import (
    CachedMergeVerdict,
    ConfirmedMergeRow,
    MergeDecision,
    MergePair,
    MergePairCandidate,
    MergePairVerdict,
    MergePerson,
    PairSurvey,
)
from packs.shared.csv_io import CsvIO

IDENTITY_CONTRACT_VERSION = "owned-identifiers-v1"
_JUDGE_VERSION = hashlib.sha1(f"{IDENTITY_CONTRACT_VERSION}\x1e{JUDGE_SYSTEM}".encode("utf-8")).hexdigest()[:8]


def person_sig(person: MergePerson) -> str:
    profile = person.evidence
    return "\x1f".join(
        [
            person.name_key,
            "|".join(sorted(person.all_emails)),
            "|".join(sorted(person.all_phones)),
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
            row.signature,
            MergeDecision(
                same_person=bool(row.same_person),
                confidence=row.confidence,
                tone_consistent=bool(row.tone_consistent),
                reason=row.reason,
                judge=row.judge,
            ),
        )
    return cache


def split_cached_pairs(
    pairs: list[MergePair],
    cache: dict[frozenset[str], CachedMergeVerdict],
) -> tuple[list[MergePairVerdict], list[MergePairCandidate]]:
    reused: list[MergePairVerdict] = []
    to_judge: list[MergePairCandidate] = []
    for pair in pairs:
        first, second = pair.first, pair.second
        signature = pair_sig(first, second)
        hit = cache.get(
            frozenset(
                {
                    first.parent_id or first.person_id,
                    second.parent_id or second.person_id,
                }
            )
        )
        if hit and hit.signature == signature:
            reused.append(MergePairVerdict(first, second, signature, hit.decision))
        else:
            to_judge.append(MergePairCandidate(first, second, signature))
    return reused, to_judge


def survey_pairs(db: Db, *, refresh: bool = False) -> PairSurvey:
    """Split candidate pairs into free slam dunks and everything the judge decides.

    Every pair lands in exactly one of the two buckets. There used to be a
    third — pairs sharing an observed email/phone were dropped unjudged on the
    premise that a shared identifier means the identity graph had already
    joined them into one parent. Two parents holding one identifier is proof
    that premise failed for that pair, so the bucket described a state that
    could not exist while collecting the pairs that proved it could. On the
    owner's install it silently stranded a real duplicate forever: one shared
    phone, name keys one character apart, so `slam_dunk_verdict`'s equality
    test missed and nothing else ever looked at it again. Shared identifiers
    now go to the judge like any other ambiguous pair — that is what it is for.
    """
    people = merge_people(db)
    pairs = generate_pairs(people)
    slam: list[MergePairVerdict] = []
    rest: list[MergePair] = []
    for pair in pairs:
        first, second = pair.first, pair.second
        verdict = slam_dunk_verdict(first, second)
        if verdict:
            slam.append(MergePairVerdict(first, second, pair_sig(first, second), verdict))
        else:
            rest.append(pair)
    parent_by_person = {row.person_id: row.parent_id for row in person_rows(db)}
    cache = (
        {}
        if refresh
        else load_cached_verdicts(
            merge_verdicts(db),
            parent_by_person,
        )
    )
    reused, to_judge = split_cached_pairs(rest, cache)
    return PairSurvey(people, pairs, slam, reused, to_judge)


def verdict_rows(
    verdicts: list[MergePairVerdict],
    confidence: float,
) -> tuple[MergeVerdictRow, ...]:
    rows = []
    for verdict in verdicts:
        first, second = verdict.first, verdict.second
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
                verdict.decision.judge,
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
) -> tuple[list[ConfirmedMergeRow], list[list[str]]]:
    edges: list[tuple[str, str]] = []
    rows: list[ConfirmedMergeRow] = []
    for verdict in verdicts:
        decision = verdict.decision
        if not decision.same_person or decision.confidence < confidence:
            continue
        first, second = verdict.first, verdict.second
        edges.append((first.person_id, second.person_id))
        rows.append(
            ConfirmedMergeRow(
                first.slug,
                first.name,
                second.slug,
                second.name,
                round(decision.confidence, 3),
                decision.tone_consistent,
                decision.reason,
            )
        )
    rows.sort(key=lambda row: row.confidence, reverse=True)
    return rows, connected_components([person.person_id for person in people], edges)


def render_results(
    *,
    out_csv: Path,
    out_md: Path,
    people: list[MergePerson],
    verdicts: list[MergePairVerdict],
    confidence: float,
) -> tuple[list[ConfirmedMergeRow], list[list[str]]]:
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
    people_by_id = {person.person_id: person for person in people}
    lines = [
        f"# Merge candidates ({len(clusters)} clusters, {len(confirmed)} pairs)",
        "",
        f"_Generated {now_iso()}. LLM-judged on tone + identity. Confirm before merging._",
        "",
    ]
    for number, group in enumerate(clusters, 1):
        lines.append(f"## Cluster {number}")
        lines.extend(f"- [[{people_by_id[person_id].slug}]] **{people_by_id[person_id].name}**" for person_id in group)
        lines.append("")
    if not clusters:
        lines.append("_No merge candidates confirmed._")
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return confirmed, clusters

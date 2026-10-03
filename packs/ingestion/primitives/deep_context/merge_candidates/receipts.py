"""SQLite-backed merge survey cache plus human-readable result exports.

Changelog:
- 2026-10-03: all source names and stored child rejections constrain acceptance.
- 2026-10-01: shared mailboxes are left out of the survey.
- 2026-10-01: accepted merges preserve explicit different-person judgments.
"""

from __future__ import annotations

from pathlib import Path

from packs.ingestion.primitives.common.contact_fields import is_shared_mailbox
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp, MergeVerdictRow
from packs.ingestion.primitives.deep_context.db.merge_queries import merge_people
from packs.ingestion.primitives.deep_context.db.queries import merge_verdicts, people as person_rows
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import (
    accepted_edges,
    connected_components,
    generate_pairs,
    slam_dunk_verdict,
)
from packs.ingestion.primitives.deep_context.merge_candidates.judge import judge_request, request_signature
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


def pair_sig(first: MergePerson, second: MergePerson, *, owner_name: str = "") -> str:
    return request_signature(judge_request(first, second, owner_name=owner_name))


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
                same_person=row.same_person,
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
    *, owner_name: str = "",
) -> tuple[list[MergePairVerdict], list[MergePairCandidate]]:
    reused: list[MergePairVerdict] = []
    to_judge: list[MergePairCandidate] = []
    for pair in pairs:
        first, second = pair.first, pair.second
        signature = pair_sig(first, second, owner_name=owner_name)
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


def survey_pairs(db: Db, *, refresh: bool = False, owner_name: str = "") -> PairSurvey:
    """Survey current parents without rejudging source-child rejections as aggregates."""
    # A mailbox an earlier import let in is not a person to merge.
    people = [person for person in merge_people(db) if not is_shared_mailbox(person.emails, person.phone_digits)]
    parent_by_person = {row.person_id: row.parent_id for row in person_rows(db)}
    stored = merge_verdicts(db)
    rejected = {
        frozenset((parent_by_person[row.person_a], parent_by_person[row.person_b]))
        for row in stored if row.same_person is False
    }
    blocked_parents = {next(iter(pair)) for pair in rejected if len(pair) == 1}
    pairs = [pair for pair in generate_pairs(people)
             if not ({pair.first.parent_id, pair.second.parent_id} & blocked_parents)
             and frozenset((pair.first.parent_id, pair.second.parent_id)) not in rejected]
    slam: list[MergePairVerdict] = []
    rest: list[MergePair] = []
    for pair in pairs:
        first, second = pair.first, pair.second
        verdict = slam_dunk_verdict(first, second)
        if verdict:
            slam.append(MergePairVerdict(first, second, pair_sig(first, second, owner_name=owner_name), verdict))
        else:
            rest.append(pair)
    cache = (
        {}
        if refresh
        else load_cached_verdicts(
            stored,
            parent_by_person,
        )
    )
    reused, to_judge = split_cached_pairs(rest, cache, owner_name=owner_name)
    return PairSurvey(people, pairs, slam, reused, to_judge)


def _accepted_verdict_edges(verdicts: list[MergePairVerdict], *, db: Db | None = None) -> list[tuple[str, str]]:
    people = {person.person_id: person for verdict in verdicts for person in (verdict.first, verdict.second)}
    decisions = [
        (v.first.person_id, v.second.person_id, v.decision.same_person, v.decision.confidence)
        for v in verdicts
    ]
    if db is not None:
        parent_by_person = {row.person_id: row.parent_id for row in person_rows(db)}
        representative = {person.parent_id: person.person_id for person in people.values()}
        for row in merge_verdicts(db):
            if row.same_person is not False:
                continue
            left = representative.get(parent_by_person[row.person_a])
            right = representative.get(parent_by_person[row.person_b])
            if left is not None and right is not None:
                decisions.append((left, right, False, row.confidence))
    return accepted_edges(decisions, source_names={key: person.source_names for key, person in people.items()})


def verdict_rows(verdicts: list[MergePairVerdict], *, db: Db | None = None) -> tuple[MergeVerdictRow, ...]:
    selected = set(_accepted_verdict_edges(verdicts, db=db))
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
                (first.person_id, second.person_id) in selected,
                now_iso(),
            )
        )
    return tuple(sorted(rows, key=lambda row: (row.person_a, row.person_b)))


def _confirmed(
    people: list[MergePerson],
    verdicts: list[MergePairVerdict],
    *,
    db: Db | None = None,
) -> tuple[list[ConfirmedMergeRow], list[list[str]]]:
    edges = _accepted_verdict_edges(verdicts, db=db)
    selected = set(edges)
    rows: list[ConfirmedMergeRow] = []
    for verdict in verdicts:
        decision = verdict.decision
        first, second = verdict.first, verdict.second
        if tuple(sorted((first.person_id, second.person_id))) not in selected:
            continue
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
    db: Db | None = None,
) -> tuple[list[ConfirmedMergeRow], list[list[str]]]:
    """Write display exports only; SQLite remains the graph/cache authority."""
    confirmed, clusters = _confirmed(people, verdicts, db=db)
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
        f"_Generated {now_iso()}. Source identifiers and identity judgments._",
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

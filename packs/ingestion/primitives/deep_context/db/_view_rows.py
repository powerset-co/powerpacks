"""Private row shapers shared by the named review queries."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from packs.ingestion.primitives.deep_context.db._view_sql import (
    CANDIDATE_SELECT,
    LINKEDIN_CTE,
    PARENT_SELECT,
    WORTH_CTE,
    WORTH_GATE_ACCEPTED,
    WORTH_GATE_MAYBE,
    WORTH_GATE_REJECTED,
    WORTH_SELECT,
)
from packs.ingestion.primitives.deep_context.db.identity_policy import IdentityPolicy
from packs.ingestion.primitives.deep_context.db.models import (
    PARENT_WORTH_PREFIX,
    ResearchHandle,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import (
    CandidateProfile,
    CandidateViewRow,
    LinkedInProgress,
    ParentViewRow,
    WorthCounts,
    WorthHumanRow,
    WorthMachineRow,
    WorthRow,
    WorthSummary,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult


def _json(value: object, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def _worth_row(row: sqlite3.Row) -> WorthRow:
    human: WorthHumanRow | None = None
    if row["human_worth"]:
        human = WorthHumanRow(
            decision=row["human_worth"],
            updated_at=row["human_worth_at"] or "",
            note=row["human_worth_note"] or "",
        )
    return WorthRow(
        key=f"{PARENT_WORTH_PREFIX}{row['parent_id']}",
        parent_id=row["parent_id"],
        parent_slug=ResearchHandle.for_parent(row["parent_id"], row["display_slug"]),
        person_ids=tuple(_json(row["person_ids_json"], [])),
        name=row["display_name"] or row["public_identifier"],
        machine=WorthMachineRow(
            decision=row["machine_worth"],
            reason=row["machine_worth_reason"],
            source=row["machine_source"],
        ),
        human=human,
        effective=row["effective_worth"],
        source="user" if human else row["machine_source"],
    )


def _worth_rows(db: Db, *, pending_only: bool) -> list[WorthRow]:
    where = f"WHERE {WORTH_GATE_MAYBE} AND w.has_synthetic=0" if pending_only else ""
    rows = db.query(WORTH_CTE + WORTH_SELECT.format(where=where))
    return [_worth_row(row) for row in rows]


def _worth_counts(db: Db) -> WorthCounts:
    row = db.query(
        WORTH_CTE
        + f"""
SELECT count(*) AS total,
       sum({WORTH_GATE_MAYBE} AND w.has_synthetic=0) AS pending,
       sum({WORTH_GATE_ACCEPTED}) AS yes,
       sum({WORTH_GATE_REJECTED}) AS no
FROM worth w
"""
    )[0]
    return WorthCounts(*(int(row[key] or 0) for key in ("total", "pending", "yes", "no")))


def _synthetic_candidate(value: object) -> CandidateProfile:
    """Read the same native Parallel result used by research candidates."""
    return _research_candidate(value)


def _research_candidate(value: object) -> CandidateProfile:
    """Parse a projected Parallel result through its sanctioned typed reader."""
    research: ResearchResult | None = ResearchResult.from_json(str(value or ""))
    if research is None:
        return CandidateProfile()
    profile = research.identity_profile()
    return CandidateProfile(
        full_name=profile.full_name,
        headline=profile.headline,
        profile_pic_url=profile.profile_pic_url,
        experiences=profile.experiences,
        education=profile.education,
        location=profile.location,
        linkedin_url=profile.linkedin_url,
        has_profile=profile.has_profile,
    )


def _candidate_profile(row: sqlite3.Row) -> CandidateProfile:
    """Select exactly one profile source from the candidate's persisted origin."""
    if row["profile_source"] == "synthetic":
        return _synthetic_candidate(row["synthetic_profile_json"])
    if row["profile_source"] == "research":
        return _research_candidate(row["research_json"])
    return CandidateProfile(
        full_name=str(row["display_name"] or ""),
        linkedin_url=str(row["linkedin_url"] or ""),
        has_profile=bool(row["linkedin_url"]),
    )


def _candidate_row(row: sqlite3.Row) -> CandidateViewRow:
    profile = _candidate_profile(row)
    decision = IdentityPolicy.effective_decision(
        decision_action=row["decision_action"],
        decision_approved=row["decision_approved"],
        replacement_url=row["replacement_url"],
        replacement_public_identifier=row["replacement_public_identifier"],
        machine_action=row["machine_action"],
        machine_approved=row["machine_approved"],
        machine_proposed_url=row["machine_proposed_url"],
        machine_proposed_public_identifier=row["machine_proposed_public_identifier"],
        linkedin_url=row["linkedin_url"],
        public_identifier=row["public_identifier"],
    )
    return CandidateViewRow(
        pub=row["public_identifier"],
        row_key=row["row_key"],
        profile_pub=decision.public_identifier or row["public_identifier"],
        url=decision.url or profile.linkedin_url,
        full_name=profile.full_name,
        headline=profile.headline,
        profile_pic_url=profile.profile_pic_url,
        experiences=profile.experiences,
        education=profile.education,
        location=profile.location,
        has_profile=profile.has_profile,
        verdict=row["machine_judgment"] or "",
        confidence=(
            None
            if row["machine_confidence"] is None
            else float(row["machine_confidence"])
        ),
        reason=row["machine_reason"] or "",
        match_emails=tuple(_json(row["emails_json"], [])),
        match_phones=tuple(_json(row["phones_json"], [])),
        import_candidate=bool(row["raw_import"]),
        candidate_origin=bool(row["candidate_origin"]),
        synthetic=row["kind"] == "synthetic",
        action=decision.action,
        approved=decision.approved,
        new_url=decision.new_url,
        new_public_identifier=decision.new_public_identifier,
        pending=bool(row["is_pending"]),
    )


def _parent_row(
    row: sqlite3.Row,
    candidates: tuple[CandidateViewRow, ...] = (),
) -> ParentViewRow:
    worth = _worth_row(row)
    slug = ResearchHandle.for_parent(row["parent_id"], row["display_slug"])
    source_channels = tuple(_json(row["sources_json"], []))
    labels = {"gmail_msgvault": "gmail", "imessage": "imessage", "whatsapp": "whatsapp"}
    return ParentViewRow(
        parent_id=row["parent_id"],
        slug=slug,
        dossier_path=row["dossier_path"],
        dossier_body=row["dossier_body"],
        name=row["display_name"] or row["public_identifier"],
        person_ids=worth.person_ids,
        sources=tuple(labels[value] for value in source_channels if value in labels),
        source_channels=source_channels,
        worth_row=worth,
        worth=WorthSummary(worth.effective, worth.source),
        machine_worth=worth.machine,
        candidates=candidates,
    )


def _hydrate_parents(
    db: Db,
    parent_rows: list[sqlite3.Row],
    *,
    pending_only: bool,
) -> list[ParentViewRow]:
    if not parent_rows:
        return []
    candidates_by_id: dict[str, list[CandidateViewRow]] = {str(row["parent_id"]): [] for row in parent_rows}
    sql = LINKEDIN_CTE + CANDIDATE_SELECT.format(
        parent_placeholders=",".join("?" for _ in candidates_by_id),
        pending="AND c.is_pending=1" if pending_only else "",
    )
    for row in db.query(sql, tuple(candidates_by_id)):
        candidates_by_id[row["parent_id"]].append(_candidate_row(row))
    return [_parent_row(row, tuple(candidates_by_id[row["parent_id"]])) for row in parent_rows]


def _all_parents(db: Db) -> list[ParentViewRow]:
    rows = db.query(
        LINKEDIN_CTE
        + PARENT_SELECT.format(
            where="""WHERE EXISTS (
              SELECT 1 FROM people pe
              WHERE pe.parent_id=p.parent_id AND pe.is_owner=0 AND pe.is_ghost=0
            ) AND EXISTS (
              SELECT 1 FROM candidate_policy c WHERE c.parent_id=p.parent_id
                AND (c.paid_profile=1 OR c.candidate_origin=1 OR c.kind='synthetic')
            )"""
        )
    )
    return _hydrate_parents(db, rows, pending_only=False)


def _linkedin_queue(db: Db) -> list[ParentViewRow]:
    rows = db.query(
        LINKEDIN_CTE + PARENT_SELECT.format(where="WHERE p.parent_id IN (SELECT parent_id FROM pending_parents)")
    )
    return _hydrate_parents(db, rows, pending_only=True)


def _linkedin_progress(db: Db) -> LinkedInProgress:
    row = db.query(
        LINKEDIN_CTE
        + """
SELECT (SELECT count(*) FROM identity_scope) AS total,
       (SELECT count(*) FROM pending_parents) AS pending
"""
    )[0]
    total, pending = int(row["total"]), int(row["pending"])
    return LinkedInProgress(total, pending, total - pending)

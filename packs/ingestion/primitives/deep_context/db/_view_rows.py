"""Private row shapers shared by the named review queries."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from packs.ingestion.primitives.deep_context.db._view_sql import (
    CANDIDATE_SELECT,
    LINKEDIN_CTE,
    PARENT_SELECT,
    WORTH_CTE,
    WORTH_SELECT,
)
from packs.ingestion.primitives.deep_context.db.identity_policy import IdentityPolicy
from packs.ingestion.primitives.deep_context.db.models import (
    PARENT_WORTH_PREFIX,
    ResearchHandle,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.research_result import ResearchResult


def _json(value: object, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def _worth_dict(row: Any) -> dict[str, Any]:
    human = None
    if row["human_worth"]:
        human = {
            "decision": row["human_worth"],
            "updated_at": row["human_worth_at"] or "",
            "note": row["human_worth_note"] or "",
        }
    return {
        "key": f"{PARENT_WORTH_PREFIX}{row['parent_id']}",
        "parent_id": row["parent_id"],
        "parent_slug": ResearchHandle.for_parent(row["parent_id"], row["display_slug"]),
        "person_ids": _json(row["person_ids_json"], []),
        "name": row["display_name"] or row["public_identifier"],
        "machine": {
            "decision": row["machine_worth"],
            "reason": row["machine_worth_reason"],
            "source": row["machine_source"],
        },
        "human": human,
        "effective": row["effective_worth"],
        "source": "user" if human else row["machine_source"],
    }


def _worth_review(db: Db, scope: str) -> list[dict[str, Any]] | dict[str, int]:
    if scope in {"rows", "queue"}:
        where = "WHERE effective_worth='maybe' AND has_synthetic=0" if scope == "queue" else ""
        rows = db.query(WORTH_CTE + WORTH_SELECT.format(where=where))
        return [_worth_dict(row) for row in rows]
    if scope == "counts":
        row = db.query(
            WORTH_CTE
            + """
SELECT count(*) AS total,
       sum(effective_worth='maybe' AND has_synthetic=0) AS pending,
       sum(effective_worth='yes') AS yes,
       sum(effective_worth='no') AS no
FROM worth
"""
        )[0]
        return {key: int(row[key] or 0) for key in ("total", "pending", "yes", "no")}
    raise ValueError(f"unknown worth review scope: {scope}")


@dataclass(frozen=True)
class _CandidateProfile:
    """One candidate profile after its kind-specific boundary parser."""

    full_name: str = ""
    headline: str = ""
    profile_pic_url: str = ""
    experiences: tuple[Any, ...] = ()
    education: tuple[Any, ...] = ()
    location: str = ""
    linkedin_url: str = ""
    has_profile: bool = False


def _json_list(value: object) -> tuple[Any, ...]:
    """Parse one JSON-list field at the synthetic-profile boundary."""
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return ()
    return tuple(parsed) if isinstance(parsed, list) else ()


def _synthetic_candidate(value: object) -> _CandidateProfile:
    """Parse the flat synthetic CSV projection without consulting other payloads."""
    payload = _json(value, {})
    if not isinstance(payload, dict):
        return _CandidateProfile()
    experiences = _json_list(payload.get("work_experiences"))
    education = _json_list(payload.get("education"))
    return _CandidateProfile(
        full_name=str(payload.get("full_name") or ""),
        headline=str(payload.get("headline") or ""),
        profile_pic_url=str(payload.get("profile_picture_url") or ""),
        experiences=experiences,
        education=education,
        location=str(payload.get("location_raw") or ""),
        linkedin_url=str(payload.get("linkedin_url") or ""),
        has_profile=bool(
            payload.get("full_name") or payload.get("headline") or experiences
            or education or payload.get("location_raw") or payload.get("linkedin_url")
        ),
    )


def _research_candidate(value: object) -> _CandidateProfile:
    """Parse a projected Parallel result through its sanctioned typed reader."""
    research = ResearchResult.from_json(str(value or ""))
    if research is None:
        return _CandidateProfile()
    profile = research.identity_profile()
    return _CandidateProfile(
        full_name=str(profile["full_name"]),
        headline=str(profile["headline"]),
        profile_pic_url=str(profile["profile_pic_url"]),
        experiences=tuple(profile["experiences"]),
        education=tuple(profile["education"]),
        location=str(profile["location"]),
        linkedin_url=str(profile["linkedin_url"]),
        has_profile=bool(profile["has_profile"]),
    )


def _candidate_profile(row: Any) -> _CandidateProfile:
    """Select exactly one profile source from the candidate's persisted origin."""
    if row["profile_source"] == "synthetic":
        return _synthetic_candidate(row["synthetic_profile_json"])
    if row["profile_source"] == "research":
        return _research_candidate(row["research_json"])
    return _CandidateProfile(
        full_name=str(row["display_name"] or ""),
        linkedin_url=str(row["linkedin_url"] or ""),
        has_profile=bool(row["linkedin_url"]),
    )


def _candidate_dict(row: Any) -> dict[str, Any]:
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
    return {
        "pub": row["public_identifier"],
        "row_key": row["row_key"],
        "profile_pub": decision.public_identifier or row["public_identifier"],
        "url": decision.url or profile.linkedin_url,
        "full_name": profile.full_name,
        "headline": profile.headline,
        "profile_pic_url": profile.profile_pic_url,
        "experiences": list(profile.experiences),
        "education": list(profile.education),
        "location": profile.location,
        "has_profile": profile.has_profile,
        "verdict": row["machine_judgment"] or "",
        "confidence": float(row["machine_confidence"] or 0.0),
        "supporting": [],
        "contradicting": [],
        "reason": row["machine_reason"] or "",
        "plausibly_absent": False,
        "recommend_dr": False,
        "match_emails": _json(row["emails_json"], []),
        "match_phones": _json(row["phones_json"], []),
        "conflict": False,
        "import_candidate": bool(row["raw_import"]),
        "candidate_origin": bool(row["candidate_origin"]),
        "synthetic": row["kind"] == "synthetic",
        "action": decision.action,
        "approved": decision.approved,
        "new_url": decision.new_url,
        "new_public_identifier": decision.new_public_identifier,
        "llm_reject": row["machine_reject"] or "",
        "llm_reject_confidence": row["machine_reject_confidence"],
        "llm_reject_reason": row["machine_reject_reason"] or "",
        "pending": bool(row["is_pending"]),
    }


def _parent_dict(row: Any) -> dict[str, Any]:
    worth = _worth_dict(row)
    slug = ResearchHandle.for_parent(row["parent_id"], row["display_slug"])
    source_channels = _json(row["sources_json"], [])
    labels = {"gmail_msgvault": "gmail", "imessage": "imessage", "whatsapp": "whatsapp"}
    return {
        "parent_id": row["parent_id"],
        "slug": slug,
        "dossier_path": row["dossier_path"],
        "dossier_body": row["dossier_body"],
        "name": row["display_name"] or row["public_identifier"],
        "person_ids": worth["person_ids"],
        "sources": [labels[value] for value in source_channels if value in labels],
        "source_channels": source_channels,
        "worth_row": worth,
        "worth": {"decision": worth["effective"], "source": worth["source"]},
        "machine_worth": worth["machine"],
        "candidates": [],
    }


def _hydrate_parents(db: Db, parent_rows: list[Any], *, pending_only: bool) -> list[dict[str, Any]]:
    parents = [_parent_dict(row) for row in parent_rows]
    if not parents:
        return []
    by_id = {parent["parent_id"]: parent for parent in parents}
    sql = LINKEDIN_CTE + CANDIDATE_SELECT.format(
        parent_placeholders=",".join("?" for _ in parents),
        pending="AND c.is_pending=1" if pending_only else "",
    )
    for row in db.query(sql, tuple(by_id)):
        by_id[row["parent_id"]]["candidates"].append(_candidate_dict(row))
    return parents


def _all_parents(db: Db) -> list[dict[str, Any]]:
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


def _linkedin_queue(db: Db) -> list[dict[str, Any]]:
    rows = db.query(
        LINKEDIN_CTE
        + PARENT_SELECT.format(
            where="WHERE p.parent_id IN (SELECT parent_id FROM pending_parents)"
        )
    )
    return _hydrate_parents(db, rows, pending_only=True)


def _linkedin_progress(db: Db) -> dict[str, int]:
    row = db.query(
        LINKEDIN_CTE
        + """
SELECT (SELECT count(*) FROM identity_scope) AS total,
       (SELECT count(*) FROM pending_parents) AS pending
"""
    )[0]
    total, pending = int(row["total"]), int(row["pending"])
    return {"total": total, "pending": pending, "done": total - pending}

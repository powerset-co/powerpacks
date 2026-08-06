"""Private row shapers shared by the named review queries."""
from __future__ import annotations

import json
from typing import Any

from packs.ingestion.primitives.deep_context.db._view_sql import (
    CANDIDATE_SELECT,
    LINKEDIN_CTE,
    PARENT_SELECT,
    WORTH_CTE,
    WORTH_SELECT,
)
from packs.ingestion.primitives.deep_context.db.models import (
    PARENT_WORTH_PREFIX,
    ResearchHandle,
)
from packs.ingestion.primitives.deep_context.db.store import Db


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


def _candidate_dict(row: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for candidate in (
        row["synthetic_profile_json"], row["research_json"], row["judgment_payload_json"],
    ):
        parsed = _json(candidate, {})
        if isinstance(parsed, dict) and parsed:
            payload = parsed
            break
    linkedin = payload.get("linkedin") if isinstance(payload.get("linkedin"), dict) else {}
    person = payload.get("person") if isinstance(payload.get("person"), dict) else {}
    social = payload.get("social") if isinstance(payload.get("social"), dict) else {}
    verdict = payload.get("verdict") if isinstance(payload.get("verdict"), dict) else {}
    full_name = (
        payload.get("full_name")
        or linkedin.get("full_name")
        or person.get("full_name")
        or " ".join(filter(None, (payload.get("first_name"), payload.get("last_name"))))
    )
    profile = {
        "full_name": str(full_name or ""),
        "headline": str(payload.get("headline") or linkedin.get("headline") or ""),
        "profile_pic_url": str(
            payload.get("profile_pic_url") or linkedin.get("profile_pic_url") or ""
        ),
        "experiences": payload.get("experiences") or linkedin.get("experiences") or [],
        "education": payload.get("education") or linkedin.get("education") or [],
        "location": payload.get("location") or linkedin.get("location") or "",
        "supporting": verdict.get("supporting_evidence") or [],
        "contradicting": verdict.get("contradicting_evidence") or [],
        "plausibly_absent": bool(verdict.get("linkedin_plausibly_absent")),
        "recommend_dr": bool(verdict.get("recommend_deep_research")),
        "linkedin_url": str(
            payload.get("linkedin_url")
            or linkedin.get("linkedin_url")
            or social.get("linkedin_url")
            or ""
        ),
    }
    proposed = row["machine_action"] == "retarget" and row["machine_proposed_url"]
    decided_retarget = row["decision_action"] == "retarget" and row["replacement_url"]
    url = row["replacement_url"] if decided_retarget else (
        row["machine_proposed_url"] if proposed else row["linkedin_url"]
    )
    pub = row["replacement_public_identifier"] if decided_retarget else (
        row["machine_proposed_public_identifier"] if proposed else row["public_identifier"]
    )
    return {
        "pub": row["public_identifier"],
        "row_key": row["row_key"],
        "profile_pub": pub or row["public_identifier"],
        "url": url or profile["linkedin_url"],
        "full_name": row["display_name"] or profile["full_name"],
        "headline": profile["headline"],
        "profile_pic_url": profile["profile_pic_url"],
        "experiences": profile["experiences"],
        "education": profile["education"],
        "location": profile["location"],
        "has_profile": bool(url or profile["linkedin_url"]),
        "verdict": row["machine_judgment"] or "",
        "confidence": float(row["machine_confidence"] or 0.0),
        "supporting": profile["supporting"],
        "contradicting": profile["contradicting"],
        "reason": row["machine_reason"] or "",
        "plausibly_absent": profile["plausibly_absent"],
        "recommend_dr": profile["recommend_dr"],
        "match_emails": _json(row["emails_json"], []),
        "match_phones": _json(row["phones_json"], []),
        "conflict": False,
        "import_candidate": bool(row["raw_import"]),
        "candidate_origin": bool(row["candidate_origin"]),
        "synthetic": row["kind"] == "synthetic",
        "action": row["decision_action"] or row["machine_action"] or "",
        "approved": row["decision_approved"] or row["machine_approved"] or "",
        "new_url": row["replacement_url"] or row["machine_proposed_url"] or "",
        "new_public_identifier": (
            row["replacement_public_identifier"]
            or row["machine_proposed_public_identifier"]
            or ""
        ),
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
            where="""WHERE NOT EXISTS (
              SELECT 1 FROM people pe WHERE pe.parent_id=p.parent_id AND pe.is_owner=1
            ) AND EXISTS (
              SELECT 1 FROM links l WHERE l.parent_id=p.parent_id
                AND (l.paid_profile=1 OR l.candidate_origin=1 OR l.kind='synthetic')
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

"""File-first verdict artifacts and SQLite identity projection policy."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import read_jsonl
from packs.ingestion.primitives.deep_context.db import views
from packs.ingestion.primitives.deep_context.db.models import (
    ApprovedState,
    IdentityMachineProjection,
    IdentitySnapshot,
    ReviewSource,
)
from packs.ingestion.primitives.deep_context.db.snapshots import identity_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url

USER_APPROVED = {ApprovedState.YES.value, ApprovedState.NO.value}
ARTIFACT_FIELDS = (
    "parent_slug", "parent_id", "name", "candidate_key", "person_ids",
    "conflict", "linkedin", "verdict", "error",
)


def _projection(
    snapshot: IdentitySnapshot, key: str, **updates: Any,
) -> IdentityMachineProjection:
    rows = [
        row for row in snapshot.links
        if row.row_key == key or row.public_identifier.lower() == key.lower()
    ]
    if not rows:
        raise StoreError(f"unknown identity candidate: {key}")
    row = sorted(rows, key=lambda item: item.row_key != key)[0]
    values = {
        field: getattr(row, field)
        for field in IdentityMachineProjection.__dataclass_fields__
        if field not in {"row_key", "updated_at"}
    }
    values.update(updates)
    return IdentityMachineProjection(row.row_key, **values, updated_at=now_iso())


def write_overrides(
    db: Db, tasks: list[dict[str, Any]], *, artifact_path: Path | None = None,
) -> dict[str, Any]:
    snapshot = identity_snapshot(db)
    existing = {row.key: row for row in snapshot.review_rows}
    projections = []
    detached = verified = pending = preserved = 0
    for task in tasks:
        key = str(task.get("candidate_key") or "").lower()
        if not key:
            continue
        if key in existing and str(existing[key].approved or "").lower() in USER_APPROVED:
            preserved += 1
            continue
        verdict = task.get("verdict") or {}
        action = task.get("action")
        if action == "confirm":
            machine_action, approved = "verify", "auto"
            verified += 1
        elif action == "detach":
            machine_action, approved = "detach", "auto"
            detached += 1
        else:
            machine_action = "detach" if verdict.get("verdict") == "wrong_person" else "verify"
            approved = None
            pending += 1
        payload = json.dumps(verdict, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        projections.append(_projection(
            snapshot,
            key,
            machine_action=machine_action,
            machine_approved=approved,
            machine_confidence=float(verdict.get("confidence") or 0),
            machine_reason=str(verdict.get("reason") or ""),
            machine_judgment=str(verdict.get("verdict") or "") or None,
            authoritative_detach=int(machine_action == "detach" and approved == "auto"),
            judgment_fingerprint=hashlib.sha256(payload.encode()).hexdigest(),
            judgment_artifact_path=str(artifact_path) if artifact_path else None,
            judgment_payload_json=payload,
            source=ReviewSource.RECONCILE.value,
        ))
    db.project_identity(tuple(projections))
    return {
        "path": str(db.db_path), "detached": detached, "verified": verified,
        "pending": pending, "preserved_user_rows": preserved, "total_rows": len(existing),
    }


def empty_overrides(db: Db) -> dict[str, Any]:
    total_rows = len({row.key for row in identity_snapshot(db).review_rows})
    return {
        "path": str(db.db_path), "detached": 0, "verified": 0, "pending": 0,
        "preserved_user_rows": 0, "total_rows": total_rows,
    }


def count_pending_identity_reviews(db: Db) -> int:
    return int(views.linkedin_review(db, "progress")["pending"])


def upsert_retargets(db: Db, proposals: list[dict[str, Any]]) -> dict[str, Any]:
    snapshot = identity_snapshot(db)
    existing = {row.key: row for row in snapshot.review_rows}
    projections = []
    proposed = preserved = 0
    for proposal in proposals:
        old_public_identifier = str(proposal.get("old_public_identifier") or "").lower()
        new_url = normalize_linkedin_url(str(proposal.get("new_linkedin_url") or ""))
        if not old_public_identifier or not new_url:
            continue
        if (
            old_public_identifier in existing
            and str(existing[old_public_identifier].approved or "").lower() in USER_APPROVED
        ):
            preserved += 1
            continue
        updates: dict[str, Any] = {
            "machine_action": "retarget",
            "machine_approved": str(proposal.get("approved") or "").lower() or None,
            "machine_confidence": float(proposal.get("confidence") or 0),
            "machine_reason": str(proposal.get("reason") or ""),
            "machine_proposed_url": new_url,
            "machine_proposed_public_identifier": str(
                proposal.get("new_public_identifier") or extract_public_identifier(new_url)
            ).lower(),
            "paid_profile": 1,
            "source": str(proposal.get("source") or ReviewSource.DEEP_RESEARCH.value),
        }
        if "llm_reject" in proposal:
            updates.update({
                "machine_reject": proposal.get("llm_reject") or None,
                "machine_reject_confidence": float(proposal.get("llm_reject_confidence") or 0),
                "machine_reject_reason": proposal.get("llm_reject_reason") or None,
            })
        if "judge_fingerprint" in proposal:
            updates["judgment_fingerprint"] = str(proposal.get("judge_fingerprint") or "")
        projections.append(_projection(snapshot, old_public_identifier, **updates))
        proposed += 1
    db.project_identity(tuple(projections))
    return {
        "path": str(db.db_path), "proposed": proposed,
        "preserved_user_rows": preserved, "total_rows": len(existing),
    }


def write_verdicts(path: Path, tasks: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for task in tasks:
            stream.write(json.dumps(
                {key: task[key] for key in ARTIFACT_FIELDS if key in task},
                ensure_ascii=False,
                separators=(",", ":"),
            ) + "\n")


def load_tasks_from_verdicts(path: Path) -> list[dict[str, Any]]:
    return [
        {
            **record,
            "verdict": record.get("verdict") or {},
            "linkedin": record.get("linkedin") or {},
        }
        for record in read_jsonl(path)
    ]


def merge_subset_tasks(path: Path, fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    replaced = {
        str(task.get("parent_id") or task.get("parent_slug") or "") for task in fresh
    }
    prior = [
        task for task in load_tasks_from_verdicts(path)
        if str(task.get("parent_id") or task.get("parent_slug") or "") not in replaced
    ]
    return prior + fresh

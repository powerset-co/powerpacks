"""Local-only Reflect evidence review, resumption, and finalization helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from packs.search.reflect.snapshots import canonical_hash, evidence_hash, snapshot_identity, validate_complete_evidence

DECISIONS = {"eligible_strong", "eligible_bench", "ineligible", "insufficient_evidence"}
STRUCTURED_EVIDENCE_FIELDS = (
    "role", "company", "location", "matched_positions", "retrieval_provenance", "relevant_profile_evidence",
)


def parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field} must be a full ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or "T" not in value:
        raise ValueError(f"{field} must be timezone-aware and include date and time")
    return parsed


def _blank_human() -> dict[str, Any]:
    return {"decision": None, "reason_codes": [], "notes": "", "reviewer": None, "reviewed_at": None}


def _unique_rows(rows: Iterable[dict[str, Any]], artifact: str) -> list[dict[str, Any]]:
    materialized = list(rows)
    ids = [row.get("person_id") for row in materialized]
    if any(not person_id for person_id in ids):
        raise ValueError(f"{artifact} contains a row without person_id")
    if len(ids) != len(set(ids)):
        raise ValueError(f"{artifact} contains duplicate person_id rows")
    return materialized


def review_pool_evidence_hash(rows: Iterable[dict[str, Any]]) -> str:
    rows = _unique_rows(rows, "review pool")
    return canonical_hash({row["person_id"]: row["evidence_hash"] for row in rows})


def validate_packet_semantics(packet: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _unique_rows(packet.get("rows") or [], "review packet")
    for row in rows:
        if row.get("evidence_hash") != evidence_hash(row.get("evidence") or {}):
            raise ValueError(f"{row['person_id']}: review packet evidence_hash does not match evidence")
    if packet.get("review_pool_evidence_hash") != review_pool_evidence_hash(rows):
        raise ValueError("review packet pool hash does not match its rows")
    return rows


def build_review_packet(
    *, case_id: str, case_hash: str, corpus_snapshot_hash: str, candidates: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    rows = []
    for candidate in _unique_rows(candidates, "candidate pool"):
        evidence = candidate.get("evidence") or {}
        rows.append({
            "person_id": candidate["person_id"], "evidence_hash": evidence_hash(evidence), "evidence": evidence,
            "machine_proposal": candidate.get("machine_proposal"),
            "machine_reasoning": candidate.get("machine_reasoning"), "human": _blank_human(),
        })
    rows.sort(key=lambda row: row["person_id"])
    return {
        "schema_version": "reflect.review_packet.v1", "case_id": case_id, "case_hash": case_hash,
        "corpus_snapshot_hash": corpus_snapshot_hash, "review_pool_evidence_hash": review_pool_evidence_hash(rows),
        "rows": rows,
    }


def merge_human_labels(packet: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = validate_packet_semantics(packet)
    previous_rows = _unique_rows((previous or {}).get("rows") or [], "human labels")
    if previous:
        for field in ("case_id", "case_hash", "corpus_snapshot_hash", "review_pool_evidence_hash"):
            if previous.get(field) != packet.get(field):
                raise ValueError(f"human labels do not match packet {field}")
    prior = {(row["person_id"], row.get("evidence_hash")): row.get("human") for row in previous_rows}
    return {
        "schema_version": "reflect.human_labels.v1", "case_id": packet["case_id"],
        "case_hash": packet["case_hash"], "corpus_snapshot_hash": packet["corpus_snapshot_hash"],
        "review_pool_evidence_hash": packet["review_pool_evidence_hash"],
        "rows": [{"person_id": row["person_id"], "evidence_hash": row["evidence_hash"],
                  "human": prior.get((row["person_id"], row["evidence_hash"])) or _blank_human()} for row in rows],
    }


def _meaningful_evidence(evidence: dict[str, Any]) -> bool:
    return all(isinstance(evidence.get(field), list) and evidence[field] for field in STRUCTURED_EVIDENCE_FIELDS)


def _validate_human(human: dict[str, Any], person_id: str) -> None:
    if human.get("decision") not in DECISIONS:
        raise ValueError(f"{person_id}: a valid explicit human decision is required")
    if not human.get("reason_codes") or not all(str(code).strip() for code in human["reason_codes"]):
        raise ValueError(f"{person_id}: at least one non-empty reason code is required")
    if not str(human.get("reviewer") or "").strip() or not human.get("reviewed_at"):
        raise ValueError(f"{person_id}: reviewer and reviewed_at are required")
    parse_timestamp(human["reviewed_at"], f"{person_id}.reviewed_at")


def validate_ground_truth_semantics(gt: dict[str, Any]) -> None:
    labels = _unique_rows(gt.get("labels") or [], "ground truth labels")
    pool = gt.get("review_pool_evidence_hashes") or {}
    if {label["person_id"] for label in labels} != set(pool):
        raise ValueError("ground truth must preserve exactly one finalized disposition for every review-pool person")
    if gt.get("review_pool_evidence_hash") != canonical_hash(pool):
        raise ValueError("ground truth review_pool_evidence_hash does not match evidence map")
    for label in labels:
        if pool.get(label["person_id"]) != label.get("evidence_hash"):
            raise ValueError(f"{label['person_id']}: ground truth label does not match review-pool evidence")
        _validate_human(label, label["person_id"])
    parse_timestamp(gt.get("finalized_at"), "finalized_at")


def finalize_human_labels(
    packet: dict[str, Any], labels: dict[str, Any], snapshot: dict[str, Any], *, finalized_at: str | None = None
) -> dict[str, Any]:
    packet_rows = validate_packet_semantics(packet)
    label_rows = _unique_rows(labels.get("rows") or [], "human labels")
    for field in ("case_id", "case_hash", "corpus_snapshot_hash", "review_pool_evidence_hash"):
        if packet.get(field) != labels.get(field):
            raise ValueError(f"labels do not match review packet {field}")
    if packet["corpus_snapshot_hash"] != snapshot_identity(snapshot):
        raise ValueError("review packet does not match the corpus snapshot")
    packet_by_id = {row["person_id"]: row for row in packet_rows}
    if set(packet_by_id) != {row["person_id"] for row in label_rows}:
        raise ValueError("every review-pool person must have exactly one human-label row")
    errors = validate_complete_evidence(packet_rows, snapshot)
    if errors:
        raise ValueError("; ".join(errors))
    finalized = []
    for row in label_rows:
        person_id = row["person_id"]
        packet_row = packet_by_id[person_id]
        if row.get("evidence_hash") != packet_row["evidence_hash"]:
            raise ValueError(f"{person_id}: label evidence is stale")
        human = row.get("human") or {}
        _validate_human(human, person_id)
        if human["decision"] != "insufficient_evidence" and not _meaningful_evidence(packet_row["evidence"]):
            raise ValueError(f"{person_id}: eligible/ineligible decisions require meaningful structured evidence")
        finalized.append({
            "person_id": person_id, "evidence_hash": row["evidence_hash"], "decision": human["decision"],
            "reason_codes": human["reason_codes"], "notes": human.get("notes", ""),
            "reviewer": human["reviewer"], "reviewed_at": human["reviewed_at"],
        })
    finalized_at = finalized_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    gt = {
        "schema_version": "reflect.ground_truth.v1", "case_id": packet["case_id"],
        "case_hash": packet["case_hash"], "corpus_snapshot_hash": packet["corpus_snapshot_hash"],
        "review_pool_evidence_hash": packet["review_pool_evidence_hash"],
        "review_pool_evidence_hashes": {row["person_id"]: row["evidence_hash"] for row in packet_rows},
        "labels": finalized, "finalized_at": finalized_at,
    }
    validate_ground_truth_semantics(gt)
    return gt

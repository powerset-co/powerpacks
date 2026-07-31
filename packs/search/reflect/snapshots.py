"""Offline identity and comparability helpers for Reflect corpus snapshots."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Iterable


REQUIRED_IDENTITY_FIELDS = (
    "backend",
    "source",
    "verification_status",
    "set_id",
    "operator_scope_hash",
    "membership_hash",
    "namespace_schema_hashes",
)


def canonical_json(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes, excluding observation metadata."""
    def stable(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: stable(val) for key, val in sorted(item.items()) if key != "observed_at"}
        if isinstance(item, list):
            return [stable(val) for val in item]
        return item

    return json.dumps(stable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def evidence_hash(evidence: dict[str, Any]) -> str:
    return canonical_hash(evidence)


def snapshot_identity(snapshot: dict[str, Any]) -> str:
    """Hash the stable corpus identity and complete evidence binding."""
    return canonical_hash({key: value for key, value in snapshot.items() if key not in {"schema_version", "observed_at"}})


def validate_snapshot(snapshot: dict[str, Any], required_person_ids: Iterable[str] = ()) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_IDENTITY_FIELDS:
        if not snapshot.get(field):
            errors.append(f"missing stable corpus identity: {field}")
    backend = snapshot.get("backend")
    source = snapshot.get("source")
    status = snapshot.get("verification_status")
    if backend == "powerset":
        errors.append("Powerset snapshots are non_comparable until the PR B runner-owned producer exists")
    elif backend not in {"local", "synthetic"}:
        errors.append(f"unsupported snapshot backend: {backend}")
    if backend == "synthetic" and source != "synthetic_test_fixture":
        errors.append("synthetic snapshots must use source synthetic_test_fixture")
    if backend == "local" and source != "local_deterministic_snapshot":
        errors.append("local snapshots must use source local_deterministic_snapshot")
    if status != "verified_comparable":
        errors.append("snapshot verification_status must be verified_comparable")
    if snapshot.get("observed_at"):
        try:
            observed = datetime.fromisoformat(str(snapshot["observed_at"]).replace("Z", "+00:00"))
            if observed.tzinfo is None or observed.utcoffset() is None or "T" not in snapshot["observed_at"]:
                raise ValueError
        except ValueError:
            errors.append("observed_at must be a timezone-aware full ISO-8601 timestamp")
    native = snapshot.get("native_content_version")
    scoped = snapshot.get("scoped_records_hash")
    if bool(native) == bool(scoped):
        errors.append("exactly one of native_content_version or scoped_records_hash is required")
    hashes = snapshot.get("evidence_hashes")
    if not isinstance(hashes, dict):
        errors.append("evidence_hashes must be an object")
        hashes = {}
    for person_id in sorted(set(required_person_ids)):
        if not hashes.get(person_id):
            errors.append(f"missing evidence hash for required person: {person_id}")
    return errors


def validate_complete_evidence(
    rows: Iterable[dict[str, Any]], snapshot: dict[str, Any]
) -> list[str]:
    """Require every review/labeled person, not merely retrieved candidates."""
    errors: list[str] = []
    hashes = snapshot.get("evidence_hashes") or {}
    for row in rows:
        person_id = row.get("person_id")
        expected = row.get("evidence_hash")
        if not person_id or not expected:
            errors.append("review/labeled row is missing person_id or evidence_hash")
        elif hashes.get(person_id) != expected:
            errors.append(f"snapshot evidence mismatch for required person: {person_id}")
    return errors


def compare_snapshots(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    required_person_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Compare stable identity and full-pool evidence, failing closed."""
    required = sorted(set(required_person_ids))
    reasons = validate_snapshot(baseline, required) + validate_snapshot(candidate, required)
    for field in (*REQUIRED_IDENTITY_FIELDS, "native_content_version", "scoped_records_hash"):
        if baseline.get(field) != candidate.get(field):
            reasons.append(f"changed stable corpus identity: {field}")
    base_hashes = baseline.get("evidence_hashes") or {}
    candidate_hashes = candidate.get("evidence_hashes") or {}
    for person_id in required:
        if base_hashes.get(person_id) != candidate_hashes.get(person_id):
            reasons.append(f"changed evidence for required person: {person_id}")
    return {
        "status": "non_comparable" if reasons else "comparable",
        "baseline_identity": snapshot_identity(baseline),
        "candidate_identity": snapshot_identity(candidate),
        "reasons": list(dict.fromkeys(reasons)),
    }

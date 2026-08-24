"""Offline identity and comparability helpers for Reflect corpus snapshots.

Changelog:
- 2026-08-06: Added the tagged-metadata verification status, and CHANGED THE MEANING of
  `corpus.native_content_version`. It was previously a verify-then-relabel field: the
  runner still enumerated the whole corpus, required the supplied value to equal the
  freshly derived `scoped_records_hash`, and emitted `verified_comparable` either way.
  It is now a caller-supplied corpus TAG: supplying it skips enumeration entirely and
  yields a cheap snapshot built from live metadata (per-namespace row counts and write
  watermarks), carrying TAGGED_METADATA_VERIFICATION_STATUS. Anyone who had wired up
  the old contract would be silently downgraded from strict comparability — nothing
  did, so there is no live regression, but drop the field to keep strict snapshots.
  `validate_snapshot` accepts only `verified_comparable` unless the caller names the
  weaker status explicitly, so strict Reflect scoring refuses tagged snapshots.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Iterable, Mapping


REQUIRED_IDENTITY_FIELDS = (
    "backend",
    "source",
    "verification_status",
    "set_id",
    "operator_scope_hash",
    "membership_hash",
    "namespace_schema_hashes",
)
SNAPSHOT_SCHEMA_VERSION = "reflect.corpus_snapshot.v2"

# The only status that proves a scored run and its labelling saw byte-identical rows.
STRICT_VERIFICATION_STATUS = "verified_comparable"
# A caller-tagged snapshot: cheap live metadata, no row enumeration, no strict proof.
TAGGED_METADATA_VERIFICATION_STATUS = "tagged_metadata_non_comparable"
# What a production search run may proceed on. Strict Reflect scoring never widens to this.
RUN_VERIFICATION_STATUSES = (STRICT_VERIFICATION_STATUS, TAGGED_METADATA_VERIFICATION_STATUS)
WATERMARK_MEMBERSHIP_HASH_VERSION = "corpus.watermark_membership.v1"


def canonical_json(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes without silently dropping nested fields."""
    def stable(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: stable(val) for key, val in sorted(item.items())}
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


def watermark_membership_hash(namespace_metadata: Mapping[str, Any]) -> str:
    """Hash the cheap write-watermark document that stands in for a member-ID hash.

    A tagged snapshot never enumerates rows, so it cannot hash the real member ID set.
    It hashes per-namespace row counts and write watermarks instead: identical
    watermarks mean nothing was written, which is a weak membership proof and not a
    strict one. That weakness is exactly why such snapshots carry
    TAGGED_METADATA_VERIFICATION_STATUS and are refused by strict Reflect scoring.

    The inputs can also be stale in ways this hash cannot see: `approx_row_count` is
    approximate by name, and both it and `last_write_at` can lag a real write — an
    uncheckpointed DuckDB WAL, or a TurboPuffer read replica behind the write. So an
    unchanged watermark does not prove an unchanged corpus, only that no write is
    visible from here. Treat equality as "probably the same index", never as proof.
    """
    return canonical_hash({
        "version": WATERMARK_MEMBERSHIP_HASH_VERSION,
        "namespaces": {str(name): value for name, value in sorted(namespace_metadata.items())},
    })


def _tagged_metadata_errors(snapshot: dict[str, Any]) -> list[str]:
    """Require the cheap identity a tagged snapshot claims: a tag plus a watermark doc."""
    errors: list[str] = []
    if not snapshot.get("native_content_version"):
        errors.append("tagged snapshots require the caller-supplied native_content_version")
    metadata = snapshot.get("namespace_metadata")
    if not isinstance(metadata, dict) or not metadata:
        return errors + ["tagged snapshots require a namespace_metadata watermark document"]
    for name in sorted(metadata):
        row = metadata[name]
        count = row.get("approx_row_count") if isinstance(row, dict) else None
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            errors.append(f"namespace_metadata {name} is missing an integer approx_row_count")
    return errors


def validate_snapshot(
    snapshot: dict[str, Any],
    required_person_ids: Iterable[str] = (),
    *,
    accepted_statuses: tuple[str, ...] = (STRICT_VERIFICATION_STATUS,),
) -> list[str]:
    """Validate one snapshot, defaulting to the strict status so callers fail closed."""
    errors: list[str] = []
    if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        errors.append(f"unsupported corpus snapshot schema: {snapshot.get('schema_version')}")
    for field in REQUIRED_IDENTITY_FIELDS:
        if not snapshot.get(field):
            errors.append(f"missing stable corpus identity: {field}")
    backend = snapshot.get("backend")
    source = snapshot.get("source")
    status = snapshot.get("verification_status")
    tagged = status == TAGGED_METADATA_VERIFICATION_STATUS
    if backend == "powerset":
        if source != "pr_b_runner_snapshot":
            errors.append("Powerset snapshots must use the typed runner producer")
        if not tagged:
            if snapshot.get("enumeration_complete") is not True:
                errors.append("Powerset snapshot membership enumeration is incomplete")
            if snapshot.get("enumeration_truncated") is not False:
                errors.append("Powerset snapshot membership enumeration was truncated")
            if not isinstance(snapshot.get("enumerated_record_count"), int):
                errors.append("Powerset snapshot record count proof is missing")
            counts = snapshot.get("namespace_record_counts")
            if not isinstance(counts, dict) or set(counts) != {
                "people", "summaries", "companies", "company_signals", "education", "schools"
            }:
                errors.append("Powerset snapshot namespace count proof is missing")
            elif sum(counts.values()) != snapshot.get("enumerated_record_count"):
                errors.append("Powerset snapshot namespace counts do not match total enumeration")
            if not isinstance(snapshot.get("membership_id_count"), int):
                errors.append("Powerset snapshot membership count proof is missing")
    elif backend not in {"local", "synthetic"}:
        errors.append(f"unsupported snapshot backend: {backend}")
    if backend == "synthetic" and source != "synthetic_test_fixture":
        errors.append("synthetic snapshots must use source synthetic_test_fixture")
    if backend == "local" and source != "local_deterministic_snapshot":
        errors.append("local snapshots must use source local_deterministic_snapshot")
    if tagged:
        errors.extend(_tagged_metadata_errors(snapshot))
    if status not in accepted_statuses:
        errors.append(
            "snapshot verification_status must be " + " or ".join(accepted_statuses)
            + (
                f"; this snapshot is {status} (cheap metadata identity, no row enumeration)"
                if tagged
                else ""
            )
        )
    if snapshot.get("observed_at"):
        try:
            observed = datetime.fromisoformat(str(snapshot["observed_at"]).replace("Z", "+00:00"))
            if observed.tzinfo is None or observed.utcoffset() is None or "T" not in snapshot["observed_at"]:
                raise ValueError
        except ValueError:
            errors.append("observed_at must be a timezone-aware full ISO-8601 timestamp")
    native = snapshot.get("native_content_version")
    scoped = snapshot.get("scoped_records_hash")
    if status in RUN_VERIFICATION_STATUSES and bool(native) == bool(scoped):
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

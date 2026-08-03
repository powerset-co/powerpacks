"""Canonical private JSON/JSONL and shareable redacted CSV persistence."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .frontier import StageResult
from .filters import hard_filter_validation_artifact
from .models import SearchSpec

REVIEW_EVIDENCE_NAME = "review/evidence.json"


@dataclass(frozen=True)
class ReviewEvidenceSnapshot:
    schema_version: str
    evidence_hashes: dict[str, str]
    evidence_hash: str

    @classmethod
    def from_hashes(cls, evidence_hashes: dict[str, str]) -> "ReviewEvidenceSnapshot":
        hashes = dict(evidence_hashes)
        return cls("search.review_evidence.v1", hashes, _canonical_hash(hashes))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ReviewEvidenceSnapshot":
        if set(value) != {"schema_version", "evidence_hashes", "evidence_hash"}:
            raise ValueError("review evidence artifact has invalid fields")
        if value["schema_version"] != "search.review_evidence.v1":
            raise ValueError("unsupported review evidence artifact")
        hashes = value["evidence_hashes"]
        if not isinstance(hashes, dict) or any(
            not isinstance(person_id, str) or not person_id or not isinstance(digest, str)
            or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)
            for person_id, digest in hashes.items()
        ):
            raise ValueError("review evidence hashes must map person IDs to hashes")
        artifact = cls(value["schema_version"], dict(hashes), value["evidence_hash"])
        if artifact.evidence_hash != _canonical_hash(artifact.evidence_hashes):
            raise ValueError("review evidence aggregate hash does not match evidence_hashes")
        return artifact

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_hashes": dict(self.evidence_hashes),
            "evidence_hash": self.evidence_hash,
        }


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()

CSV_FIELDS = (
    "rank",
    "deterministic_score",
    "semantic_score",
    "source_lanes",
    "matched_position_count",
    "hydration_disposition",
)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def persist_result(output_dir: str | Path, spec: SearchSpec, result: StageResult) -> dict[str, str]:
    repository = Path(__file__).resolve().parents[3]
    private_root = (repository / ".powerpacks").resolve()
    root = Path(output_dir).resolve()
    if root != private_root and private_root not in root.parents:
        raise ValueError("search artifacts must be written under the repository .powerpacks directory")
    root.mkdir(parents=True, exist_ok=True)
    if not result.hard_filter_validation:
        result = replace(result, hard_filter_validation=hard_filter_validation_artifact((), spec))
    paths = {
        "search_spec_json": root / "search_spec.json",
        "result_json": root / "result.json",
        "candidates_jsonl": root / "candidates.jsonl",
        "candidates_csv": root / "candidates.csv",
        "hard_filter_validation_json": root / "hard-filter-validation.json",
        "manifest_json": root / "manifest.json",
    }
    path_strings = {key: str(path) for key, path in paths.items()}
    persisted_result = replace(result, artifact_paths={**result.artifact_paths, **path_strings})
    paths["search_spec_json"].write_bytes(_json_bytes(spec.to_dict()))
    with paths["candidates_jsonl"].open("w", encoding="utf-8") as handle:
        for row in result.frontier.candidates:
            handle.write(json.dumps(row.to_dict(), sort_keys=True, ensure_ascii=False) + "\n")
    with paths["candidates_csv"].open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for rank, row in enumerate(result.frontier.candidates, start=1):
            writer.writerow(
                {
                    "rank": rank,
                    "deterministic_score": row.deterministic_score,
                    "semantic_score": "" if row.semantic_score is None else row.semantic_score,
                    "source_lanes": "|".join(row.source_lanes),
                    "matched_position_count": len(row.matched_position_ids),
                    "hydration_disposition": row.hydration_disposition,
                }
            )
    paths["hard_filter_validation_json"].write_bytes(_json_bytes(result.hard_filter_validation))
    paths["result_json"].write_bytes(_json_bytes(persisted_result.to_dict()))
    manifest = {
        "schema_version": "search.manifest.v1",
        "status": result.status,
        "counts": dict(result.counts),
        "corpus_observation": dict(result.corpus_observation),
        "artifacts": {
            name: {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for name, path in paths.items()
            if name != "manifest_json"
        },
    }
    for name, value in result.artifact_paths.items():
        artifact = Path(value)
        if artifact.exists() and artifact.is_file() and (
            artifact.resolve().parent == root or root in artifact.resolve().parents
        ):
            manifest["artifacts"].setdefault(
                name,
                {"path": str(artifact.resolve().relative_to(root)), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()},
            )
    paths["manifest_json"].write_bytes(_json_bytes(manifest))
    return path_strings

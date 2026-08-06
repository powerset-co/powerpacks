"""Explicit current-artifact handoff into canonical Deep Context SQLite.

Workers write paid/cacheable files and replace one fixed ``manifest.json``.
``project_manifest`` validates that named snapshot outside a transaction, then
projects it atomically.  Web/runtime reads never call this module.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    CandidatePersonRow,
    FactRow,
    JobKind,
    JobRow,
    JobStatus,
    LinkRow,
    MachineWorth,
    ProjectionStatus,
    ResearchRow,
    ResearchStatus,
    ReviewAction,
    ReviewSource,
    RowKind,
    StageStateRow,
    StageStatus,
    SyntheticProfileRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url


class ProjectionError(StoreError):
    pass


@dataclass(frozen=True)
class ProjectionResult:
    stage: str
    status: str
    artifacts: int
    projected: int


@dataclass(frozen=True)
class _Parsed:
    artifact: ArtifactRow
    raw_artifact: ArtifactRow | None = None
    fact: FactRow | None = None
    candidate: LinkRow | None = None
    members: tuple[CandidatePersonRow, ...] = ()
    research: ResearchRow | None = None
    synthetic: SyntheticProfileRow | None = None


_TERMINAL = {"complete", "completed", "completed_with_errors", "research_complete"}
_KNOWN_STATUS = {
    "not_started", "stale", "needs_approval", "running", "submitted",
    "research_complete", "completed", "complete", "failed", "completed_with_errors",
    "no_match", "noop",
}


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _number(value: object) -> float | None:
    try:
        return float(str(value)) if str(value or "").strip() else None
    except ValueError:
        return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _path(root: Path, value: object) -> tuple[Path, str]:
    relative = Path(str(value or ""))
    if not str(value or "").strip() or relative.is_absolute():
        raise ProjectionError("artifact path must be relative to manifest directory")
    resolved_root = root.resolve()
    resolved = (root / relative).resolve()
    try:
        normalized = resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ProjectionError(f"artifact path escapes manifest directory: {relative}") from exc
    if not resolved.is_file():
        raise ProjectionError(f"artifact does not exist: {normalized}")
    return resolved, normalized.as_posix()


def _bytes(root: Path, entry: dict[str, Any], prefix: str = "") -> tuple[bytes, str, str]:
    path, relative = _path(root, entry.get(f"{prefix}path"))
    data = path.read_bytes()
    actual = _sha256(data)
    declared = str(entry.get(f"{prefix}sha256") or "").strip().lower()
    if len(declared) != 64 or declared != actual:
        raise ProjectionError(f"sha256 mismatch for {relative}")
    return data, relative, actual


def _json(data: bytes, relative: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectionError(f"invalid JSON artifact {relative}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProjectionError(f"JSON artifact must be an object: {relative}")
    return value


def _facts(data: bytes, relative: str) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ProjectionError(f"invalid UTF-8 facts artifact {relative}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProjectionError(f"invalid JSONL {relative}:{number}: {exc}") from exc
        if not isinstance(record, dict):
            raise ProjectionError(f"facts record must be an object: {relative}:{number}")
        facts = record.get("facts") if isinstance(record.get("facts"), dict) else record
        merged.update(facts)
    if not merged:
        raise ProjectionError(f"facts artifact is empty: {relative}")
    return merged


def _linkedin(profile: dict[str, Any]) -> str | None:
    social = profile.get("social") if isinstance(profile.get("social"), dict) else {}
    value = _text(profile.get("linkedin_url") or social.get("linkedin_url"))
    return normalize_linkedin_url(value) if value else None


def _manifest_status(value: str) -> tuple[str, str]:
    if value not in _KNOWN_STATUS:
        raise ProjectionError(f"unsupported manifest status: {value!r}")
    if value == "needs_approval":
        return StageStatus.NEEDS_APPROVAL.value, JobStatus.QUEUED.value
    if value in {"failed"}:
        return StageStatus.FAILED.value, JobStatus.FAILED.value
    if value in _TERMINAL:
        return StageStatus.COMPLETE.value, JobStatus.APPLIED.value
    if value in {"no_match", "noop"}:
        return StageStatus.COMPLETE.value, JobStatus.NO_MATCH.value
    if value in {"not_started", "stale"}:
        return StageStatus.PENDING.value, JobStatus.QUEUED.value
    return StageStatus.RUNNING.value, JobStatus.RUNNING.value


def _parse_entry(
    root: Path, entry: dict[str, Any], *, parents: set[str],
    people: dict[str, str], candidates: dict[str, tuple[str, str]], selection: str | None,
) -> _Parsed:
    kind = str(entry.get("kind") or "").strip().lower()
    if kind not in {item.value for item in ArtifactKind}:
        raise ProjectionError(f"unsupported artifact kind: {kind!r}")
    parent_id = str(entry.get("parent_id") or "").strip().lower()
    person_id = str(entry.get("person_id") or "").strip().lower() or None
    candidate_key = str(entry.get("candidate_key") or "").strip().lower() or None
    if parent_id not in parents:
        raise ProjectionError(f"unknown artifact parent: {parent_id or '?'}")
    if person_id and people.get(person_id) != parent_id:
        raise ProjectionError(f"person {person_id} does not belong to {parent_id}")
    data, relative, fingerprint = _bytes(root, entry)
    artifact_path = str((root / relative).resolve())
    owner = person_id or candidate_key or parent_id
    artifact_key = str(entry.get("artifact_key") or f"{kind}:{owner}").strip().lower()
    parsed_json = (None if kind in {ArtifactKind.DOSSIER.value, ArtifactKind.FACTS.value}
                   else _json(data, relative))
    artifact = ArtifactRow(
        artifact_key, kind, parent_id, artifact_path, fingerprint,
        ProjectionStatus.PROJECTED.value,
        person_id=person_id, candidate_key=candidate_key,
        input_fingerprint=_text(entry.get("input_fingerprint")),
        payload_json=(json.dumps(parsed_json, separators=(",", ":"))
                      if kind in {ArtifactKind.PROFILE.value, ArtifactKind.RESEARCH.value} else None),
        projected_at=now_iso(),
    )
    raw_artifact = None
    if entry.get("raw_path") or entry.get("raw_sha256"):
        if not entry.get("raw_path") or not entry.get("raw_sha256"):
            raise ProjectionError(f"raw artifact path/hash must be paired: {artifact_key}")
        raw, raw_relative, raw_fingerprint = _bytes(root, entry, "raw_")
        raw_path = str((root / raw_relative).resolve())
        raw_artifact = ArtifactRow(
            str(entry.get("raw_artifact_key") or f"raw-result:{owner}").strip().lower(),
            ArtifactKind.RAW_RESULT.value, parent_id, raw_path, raw_fingerprint,
            ProjectionStatus.PROJECTED.value, person_id=person_id, candidate_key=candidate_key,
            payload_json=json.dumps(_json(raw, raw_relative), separators=(",", ":")),
            projected_at=now_iso(),
        )

    if kind == ArtifactKind.FACTS.value:
        if not person_id:
            raise ProjectionError(f"facts artifact requires person_id: {artifact_key}")
        projected = _facts(data, relative)
        verdict = projected.get("network_worth")
        verdict = verdict if isinstance(verdict, dict) else {}
        worth = str(verdict.get("decision") or "").strip().lower() or None
        if worth and worth not in set(MachineWorth):
            raise ProjectionError(f"invalid machine worth in {relative}: {worth!r}")
        return _Parsed(artifact, raw_artifact, fact=FactRow(
            str(entry.get("subject_key") or person_id).strip().lower(), parent_id,
            artifact_key, person_id, worth, _text(verdict.get("reason")),
            _number(projected.get("confidence")), int(bool(projected.get("is_owner"))),
            json.dumps(projected, separators=(",", ":")), now_iso(),
        ))

    if kind not in {ArtifactKind.RESEARCH.value, ArtifactKind.PROFILE.value,
                    ArtifactKind.SYNTHETIC.value} and candidate_key:
        if not candidates.get(candidate_key) or candidates[candidate_key][0] != parent_id:
            raise ProjectionError(f"candidate {candidate_key} does not belong to {parent_id}")

    if kind == ArtifactKind.RESEARCH.value:
        if not candidate_key:
            raise ProjectionError(f"research artifact requires candidate_key: {artifact_key}")
        handle = str(entry.get("handle") or "").strip()
        if not handle:
            raise ProjectionError(f"research artifact requires handle: {artifact_key}")
        person_ids = tuple(sorted({str(value).strip().lower()
                                   for value in entry.get("person_ids") or [] if str(value).strip()}))
        if not person_ids:
            raise ProjectionError(f"research artifact requires person_ids: {artifact_key}")
        if any(people.get(value) != parent_id for value in person_ids):
            raise ProjectionError(f"research membership crosses parent: {artifact_key}")
        profile = parsed_json or {}
        linkedin_url = _linkedin(profile)
        proposed_pub = extract_public_identifier(linkedin_url).lower() if linkedin_url else None
        machine_action = _text(entry.get("machine_action"))
        machine_action = machine_action or (ReviewAction.RETARGET.value if linkedin_url else None)
        candidate = LinkRow(
            candidate_key, parent_id, _text(entry.get("public_identifier")) or candidate_key,
            (candidates[candidate_key][1] if candidate_key in candidates else RowKind.RESEARCH.value),
            _text(entry.get("linkedin_url")), _text(entry.get("display_name")),
            machine_action=machine_action,
            machine_approved=_text(entry.get("machine_approved")),
            machine_proposed_url=linkedin_url,
            machine_proposed_public_identifier=proposed_pub,
            machine_confidence=_number(entry.get("machine_confidence")
                                       or ((profile.get("person") or {}).get("confidence")
                                           if isinstance(profile.get("person"), dict) else None)),
            machine_reason=_text(entry.get("machine_reason")),
            machine_judgment=_text(entry.get("machine_judgment")),
            machine_reject=_text(entry.get("machine_reject")),
            machine_reject_confidence=_number(entry.get("machine_reject_confidence")),
            machine_reject_reason=_text(entry.get("machine_reject_reason")),
            authoritative_detach=int(bool(entry.get("authoritative_detach"))),
            candidate_origin=int(bool(entry.get("candidate_origin"))),
            raw_import=int(bool(entry.get("raw_import"))), paid_profile=1,
            judgment_fingerprint=_text(entry.get("judgment_fingerprint")),
            judgment_artifact_path=artifact_path,
            judgment_payload_json=json.dumps(profile, separators=(",", ":")),
            source=ReviewSource.DEEP_RESEARCH.value, updated_at=now_iso(),
        )
        members = tuple(CandidatePersonRow(candidate_key, value, parent_id) for value in person_ids)
        research_status = ResearchStatus.COMPLETE.value if linkedin_url else ResearchStatus.NO_MATCH.value
        return _Parsed(
            artifact, raw_artifact, candidate=candidate, members=members,
            research=ResearchRow(handle, parent_id, research_status, candidate_key,
                                 artifact_key, selection, json.dumps(profile, separators=(",", ":")),
                                 now_iso()),
        )

    if kind == ArtifactKind.SYNTHETIC.value:
        if not candidate_key:
            raise ProjectionError(f"synthetic artifact requires candidate_key: {artifact_key}")
        profile = parsed_json or {}
        public_identifier = str(entry.get("public_identifier")
                                or profile.get("public_identifier") or "").strip().lower()
        if not public_identifier:
            raise ProjectionError(f"synthetic artifact requires public_identifier: {artifact_key}")
        person_ids = tuple(sorted({str(value).strip().lower()
                                   for value in entry.get("person_ids") or [] if str(value).strip()}))
        if any(people.get(value) != parent_id for value in person_ids):
            raise ProjectionError(f"synthetic membership crosses parent: {artifact_key}")
        prior = candidates.get(candidate_key)
        if prior and prior != (parent_id, RowKind.SYNTHETIC.value):
            raise ProjectionError(f"synthetic candidate owner/kind mismatch: {candidate_key}")
        linkedin_url = _linkedin(profile)
        candidate = LinkRow(
            candidate_key, parent_id, public_identifier, RowKind.SYNTHETIC.value,
            linkedin_url, _text(entry.get("display_name") or profile.get("full_name")),
            machine_action=(ReviewAction.VERIFY.value
                            if str(entry.get("approved") or "") == "auto" else None),
            machine_approved=("auto" if str(entry.get("approved") or "") == "auto" else None),
            source=ReviewSource.DEEP_RESEARCH.value, updated_at=now_iso(),
        )
        members = tuple(CandidatePersonRow(candidate_key, value, parent_id) for value in person_ids)
        synthetic = SyntheticProfileRow(
            public_identifier, candidate_key, json.dumps(profile, separators=(",", ":")),
            artifact_key, linkedin_url,
            _text(entry.get("display_name") or profile.get("full_name")), now_iso(),
        )
        return _Parsed(artifact, raw_artifact, candidate=candidate,
                       members=members, synthetic=synthetic)

    if (kind == ArtifactKind.PROFILE.value and candidate_key
            and (not candidates.get(candidate_key) or candidates[candidate_key][0] != parent_id)):
        raise ProjectionError(f"unknown profile candidate: {candidate_key}")

    if kind == ArtifactKind.SOURCE_BUNDLE.value:
        return _Parsed(artifact, raw_artifact)
    if kind in {ArtifactKind.DOSSIER.value, ArtifactKind.PROFILE.value}:
        return _Parsed(artifact, raw_artifact)
    raise ProjectionError(f"artifact kind requires a specialized projector: {kind}")


def project_manifest(db: Db, manifest_path: Path) -> ProjectionResult:
    """Project one explicit manifest snapshot; identical artifact hashes are no-ops."""
    manifest_path = Path(manifest_path)
    if manifest_path.name != "manifest.json":
        raise ProjectionError("projector requires the fixed manifest.json")
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectionError(f"cannot parse manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ProjectionError("manifest must be an object")
    stage = str(manifest.get("stage") or "").strip()
    status = str(manifest.get("status") or "").strip().lower()
    if not stage:
        raise ProjectionError("manifest requires stage")
    stage_status, job_status = _manifest_status(status)
    inventory = manifest.get("artifacts")
    counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else None
    zero_work = counts is not None and counts.get("total") == 0
    if status in _TERMINAL and (
        not isinstance(inventory, list) or (not inventory and not zero_work)
    ):
        raise ProjectionError("terminal manifest requires an artifacts inventory")
    if inventory is None:
        inventory = []
    if not isinstance(inventory, list) or any(not isinstance(item, dict) for item in inventory):
        raise ProjectionError("manifest artifacts must be an array of objects")

    selection_obj = manifest.get("selection")
    selection = (_text(selection_obj.get("fingerprint")) if isinstance(selection_obj, dict)
                 else _text(selection_obj))
    parents = {row["parent_id"] for row in db._query("SELECT parent_id FROM parents")}
    people = {row["person_id"]: row["parent_id"]
              for row in db._query("SELECT person_id, parent_id FROM people")}
    candidates = {row["row_key"]: (row["parent_id"], row["kind"])
                  for row in db._query("SELECT row_key, parent_id, kind FROM links")}
    parsed = tuple(_parse_entry(
        manifest_path.parent, item, parents=parents, people=people,
        candidates=candidates, selection=selection,
    ) for item in inventory)
    keys = [item.artifact.artifact_key for item in parsed]
    keys += [item.raw_artifact.artifact_key for item in parsed if item.raw_artifact]
    if len(keys) != len(set(keys)):
        raise ProjectionError("manifest contains duplicate artifact keys")
    counts = counts or {}
    total = int(counts.get("total") or len(parsed))
    completed = min(total, int(counts.get("completed") or 0))
    manifest_hash = _sha256(manifest_bytes)
    projected = 0

    with db._connect() as conn:
        for item in parsed:
            current = conn.execute(
                "SELECT content_fingerprint, status FROM artifacts WHERE artifact_key=?",
                (item.artifact.artifact_key,),
            ).fetchone()
            changed = not current or tuple(current) != (
                item.artifact.content_fingerprint, ProjectionStatus.PROJECTED.value)
            if changed and item.candidate:
                db._project_candidate(item.candidate, conn=conn)
            raw_changed = (db._project_artifact(item.raw_artifact, conn=conn)
                           if item.raw_artifact else False)
            changed = db._project_artifact(item.artifact, conn=conn)
            projected += int(changed) + int(raw_changed)
            if not changed:
                continue
            if item.candidate:
                db._replace_candidate_people(item.candidate.row_key, item.members, conn=conn)
            if item.fact:
                db._project_fact(item.fact, conn=conn)
            if item.research:
                db._project_research(item.research, conn=conn)
            if item.synthetic:
                db._project_synthetic_profile(item.synthetic, conn=conn)
        db._write("jobs", JobRow(
            stage, JobKind.ENRICHMENT.value, job_status,
            selection_fingerprint=selection, completed_count=completed, total_count=total,
            error=_text(manifest.get("error")),
            result_json=json.dumps(manifest, separators=(",", ":")),
            started_at=_text(manifest.get("started_at")),
            finished_at=_text(manifest.get("completed_at")),
        ), conn=conn)
        db._save_stage(StageStateRow(
            stage, stage_status, selection, manifest_hash,
            _text(manifest.get("completed_at")) if stage_status == StageStatus.COMPLETE.value else None,
            _text(manifest.get("error")), _text(manifest.get("updated_at")) or now_iso(),
        ), conn=conn)
    return ProjectionResult(stage, status, len(keys), projected)

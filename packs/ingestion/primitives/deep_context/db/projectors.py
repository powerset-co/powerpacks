"""Explicit current-artifact handoff into canonical Deep Context SQLite.

Workers write paid/cacheable files, then pass their completed artifact inventory
directly to ``project_artifacts``. Stage manifests remain display receipts and
are never read to select work or to populate workflow state.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactReplacement,
    ArtifactRow,
    CandidatePersonRow,
    FactRow,
    LinkRow,
    MachineWorth,
    ProjectionStatus,
    ResearchRow,
    ResearchStatus,
    ReviewAction,
    ReviewSource,
    RowKind,
    SyntheticProfileRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.dossier.facts import NETWORK_WORTH_VALUES
from packs.ingestion.schemas.people_schema import normalize_linkedin_url


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


def _bytes(root: Path, entry: dict[str, Any], prefix: str = "") -> tuple[bytes, str, str]:
    value = entry.get(f"{prefix}path")
    relative = Path(str(value or ""))
    if not str(value or "").strip() or relative.is_absolute():
        raise ProjectionError("artifact path must be relative to artifact root")
    resolved_root = root.resolve()
    resolved = (root / relative).resolve()
    try:
        normalized = resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ProjectionError(f"artifact path escapes artifact root: {relative}") from exc
    if not resolved.is_file():
        raise ProjectionError(f"artifact does not exist: {normalized}")
    relative_path = normalized.as_posix()
    data = resolved.read_bytes()
    actual = _sha256(data)
    declared = str(entry.get(f"{prefix}sha256") or "").strip().lower()
    if len(declared) != 64 or declared != actual:
        raise ProjectionError(f"sha256 mismatch for {relative_path}")
    return data, relative_path, actual


def _json(data: bytes, relative: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectionError(f"invalid JSON artifact {relative}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProjectionError(f"JSON artifact must be an object: {relative}")
    return value


def _content_type(data: bytes) -> str:
    """Detect the small image set profile providers return, without extensions."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


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


def project_parent_fact(db: Db, path: Path, parent_id: str) -> dict[str, Any]:
    """Project one synthesis output owned directly by its canonical parent."""
    path = Path(path)
    if not path.is_file():
        changed = db.project_rows((
            ArtifactReplacement(ArtifactKind.FACTS.value, (), parent_id=parent_id),
        ))
        return {"parent_id": parent_id, "synced_rows": changed, "without_worth": 0}
    if not any(row.parent_id == parent_id for row in canonical_snapshot(db).parents):
        raise StoreError(f"facts parent is absent from canonical graph: {parent_id}")
    data = path.read_bytes()
    records = [
        json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()
    ]
    record = records[-1] if records else {}
    facts = record.get("facts") if isinstance(record.get("facts"), dict) else record
    worth = facts.get("network_worth") if isinstance(facts, dict) else None
    worth = worth if isinstance(worth, dict) else {}
    raw_decision = str(worth.get("decision") or "").strip().lower()
    decision = raw_decision if raw_decision in NETWORK_WORTH_VALUES else None
    artifact_key = f"facts:{parent_id}"
    projected = db.project_rows((
        ArtifactReplacement(
            ArtifactKind.FACTS.value,
            (ArtifactRow(
                artifact_key=artifact_key,
                kind=ArtifactKind.FACTS.value,
                parent_id=parent_id,
                path=str(path.resolve()),
                input_fingerprint=_text(record.get("input_evidence_fingerprint")),
                content_fingerprint=_sha256(data),
                status=ProjectionStatus.PROJECTED.value,
                payload_json=json.dumps(record, separators=(",", ":")),
                projected_at=now_iso(),
            ),),
            parent_id=parent_id,
        ),
        FactRow(
            subject_key=parent_id,
            parent_id=parent_id,
            artifact_key=artifact_key,
            machine_worth=decision,
            machine_worth_reason=worth.get("reason") or None,
            confidence=float(record.get("final_confidence") or facts.get("confidence") or 0),
            is_owner=int(bool(facts.get("is_owner"))),
            facts_json=json.dumps(facts, separators=(",", ":")),
            projected_at=now_iso(),
        ),
    ))
    return {
        "parent_id": parent_id,
        "synced_rows": projected,
        "without_worth": int(decision is None),
    }


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
    if kind in {ArtifactKind.DOSSIER.value, ArtifactKind.FACTS.value}:
        parsed_json = None
    elif kind == ArtifactKind.AVATAR.value:
        parsed_json = {
            "content_type": _content_type(data),
            "base64": base64.b64encode(data).decode("ascii"),
        }
    else:
        parsed_json = _json(data, relative)
    artifact = ArtifactRow(
        artifact_key, kind, parent_id, artifact_path, fingerprint,
        ProjectionStatus.PROJECTED.value,
        person_id=person_id, candidate_key=candidate_key,
        input_fingerprint=_text(entry.get("input_fingerprint")),
        payload_json=(json.dumps(parsed_json, separators=(",", ":"))
                      if kind in {
                          ArtifactKind.AVATAR.value,
                          ArtifactKind.PROFILE.value,
                          ArtifactKind.RESEARCH.value,
                          ArtifactKind.SOURCE_BUNDLE.value,
                      } else None),
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
        projected = _facts(data, relative)
        verdict = projected.get("network_worth")
        verdict = verdict if isinstance(verdict, dict) else {}
        worth = str(verdict.get("decision") or "").strip().lower() or None
        if worth and worth not in set(MachineWorth):
            raise ProjectionError(f"invalid machine worth in {relative}: {worth!r}")
        return _Parsed(artifact, raw_artifact, fact=FactRow(
            str(entry.get("subject_key") or person_id or parent_id).strip().lower(), parent_id,
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
        social = profile.get("social") if isinstance(profile.get("social"), dict) else {}
        linkedin_value = _text(profile.get("linkedin_url") or social.get("linkedin_url"))
        linkedin_url = normalize_linkedin_url(linkedin_value) if linkedin_value else None
        candidate = None
        if candidate_key not in candidates:
            candidate = LinkRow(
                candidate_key,
                parent_id,
                _text(entry.get("public_identifier")) or candidate_key,
                RowKind.RESEARCH.value,
                _text(entry.get("linkedin_url")),
                _text(entry.get("display_name")),
                candidate_origin=int(bool(entry.get("candidate_origin"))),
                raw_import=int(bool(entry.get("raw_import"))),
                paid_profile=1,
                source=ReviewSource.DEEP_RESEARCH.value,
                updated_at=now_iso(),
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
        social = profile.get("social") if isinstance(profile.get("social"), dict) else {}
        linkedin_value = _text(profile.get("linkedin_url") or social.get("linkedin_url"))
        linkedin_url = normalize_linkedin_url(linkedin_value) if linkedin_value else None
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
        owner_id = person_id or parent_id
        if str((parsed_json or {}).get("person_id") or "").strip().lower() != owner_id:
            raise ProjectionError(f"source bundle owner mismatch: {artifact_key}")
        return _Parsed(artifact, raw_artifact)
    if kind in {
        ArtifactKind.AVATAR.value,
        ArtifactKind.DOSSIER.value,
        ArtifactKind.PROFILE.value,
    }:
        return _Parsed(artifact, raw_artifact)
    raise ProjectionError(f"artifact kind requires a specialized projector: {kind}")


def project_artifacts(
    db: Db,
    artifact_root: Path,
    inventory: list[dict[str, Any]],
    *,
    stage: str,
    selection: str | None = None,
) -> ProjectionResult:
    """Project one completed in-process inventory; identical hashes are no-ops."""
    if not stage.strip():
        raise ProjectionError("artifact projection requires stage")
    if any(not isinstance(item, dict) for item in inventory):
        raise ProjectionError("artifact inventory must contain objects")
    artifact_root = Path(artifact_root)
    parents = {row["parent_id"] for row in db.query("SELECT parent_id FROM parents")}
    people = {
        row["person_id"]: row["parent_id"]
        for row in db.query("SELECT person_id, parent_id FROM people")
    }
    candidates = {
        row["row_key"]: (row["parent_id"], row["kind"])
        for row in db.query("SELECT row_key, parent_id, kind FROM links")
    }
    parsed = tuple(
        _parse_entry(
            artifact_root,
            item,
            parents=parents,
            people=people,
            candidates=candidates,
            selection=selection,
        )
        for item in inventory
    )
    keys = [item.artifact.artifact_key for item in parsed]
    keys += [item.raw_artifact.artifact_key for item in parsed if item.raw_artifact]
    if len(keys) != len(set(keys)):
        raise ProjectionError("artifact inventory contains duplicate keys")
    projected = 0

    with db.transaction() as conn:
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
                db._replace_children(
                    "candidate_people", item.candidate.row_key, item.members, conn=conn,
                )
            if item.fact:
                db._write("facts", item.fact, conn)
            if item.research:
                db._write("research", item.research, conn)
            if item.synthetic:
                db._write("synthetic_profiles", item.synthetic, conn)
    return ProjectionResult(stage, "projected", len(keys), projected)


def project_parent_source_bundle(db: Db, path: Path, parent_id: str) -> ProjectionResult:
    """Project one parent bundle, or remove its projection when absent."""
    path = Path(path)
    if not path.is_file():
        changed = db.project_rows((
            ArtifactReplacement(
                ArtifactKind.SOURCE_BUNDLE.value, (), parent_id=parent_id,
            ),
        ))
        return ProjectionResult("collect_person_context", "projected", 0, changed)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ProjectionError(f"cannot read source bundle {path}: {exc}") from exc
    owners = db.query("SELECT parent_id FROM parents WHERE parent_id=?", (parent_id,))
    if len(owners) != 1:
        raise ProjectionError(f"source bundle parent is absent from canonical graph: {parent_id}")
    return project_artifacts(db, path.parent, [{
        "artifact_key": f"source-bundle:{parent_id}",
        "kind": ArtifactKind.SOURCE_BUNDLE.value,
        "parent_id": parent_id,
        "path": path.name,
        "sha256": _sha256(data),
    }], stage="collect_person_context")

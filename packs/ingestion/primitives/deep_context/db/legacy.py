"""Absorb pre-SQLite Deep Context artifacts into one canonical database.

This is the sole tolerant artifact reader. It parses the old fixed files into
one in-memory graph, validates ownership, then commits the graph in one SQLite
transaction. Current stages project their own outputs directly and never call
this module.

Removal countdown (2026-08-06): delete when no supported install predates the
SQLite cutover release produced from PR #435.

Changelog:
  2026-08-06: split the monolithic importer into parse, reconcile, artifact,
    validation, and commit phases; manifests remain excluded.
  2026-08-05: preserve facts-backed worth population, aliases, and owners.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.contact_fields import normalize_email, normalize_phone
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.common.legacy import MESSAGE_LINKEDIN_PREFIX, message_linkedin_aliases
from packs.ingestion.primitives.deep_context.db import graph as canonical_graph
from packs.ingestion.primitives.deep_context.db import models as m
from packs.ingestion.primitives.deep_context.db.identity_policy import IdentityPolicy
from packs.ingestion.primitives.deep_context.db.projectors import ProjectionValue
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.parents.assignment import mint_parent_id
from packs.ingestion.schemas.people_schema import extract_public_identifier

LEGACY_INDEX_JSON = Path(".powerpacks/deep-context/index.json")
LEGACY_MERGE_VERDICTS_CSV = Path(".powerpacks/deep-context/merge-verdicts.csv")


class LegacyImportError(StoreError):
    pass


class LegacyGraphMigration:
    """Temporary whole-graph projector for pre-SQLite migration only."""

    @staticmethod
    def apply(
        db: Db,
        projection: m.CanonicalGraphProjection,
    ) -> m.CanonicalGraphCounts:
        with db.transaction() as conn:
            return LegacyGraphMigration._apply(conn, projection)

    @staticmethod
    def _apply(
        conn: sqlite3.Connection,
        projection: m.CanonicalGraphProjection,
    ) -> m.CanonicalGraphCounts:
        conn.execute("PRAGMA defer_foreign_keys=ON")
        if not conn.in_transaction:
            conn.execute("BEGIN DEFERRED")
        try:
            return canonical_graph._replace_canonical_graph(conn, projection)
        except canonical_graph._GraphError as exc:
            raise LegacyImportError(str(exc)) from exc


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_ACTIONS = {item.value for item in m.ReviewAction}
_APPROVALS = {item.value for item in m.ApprovedState}
_HUMAN_WORTH = {item.value for item in m.HumanWorth}
_MACHINE_WORTH = {item.value for item in m.MachineWorth}
_REVIEW_METADATA = {"public_identifier", "source", "updated_at"}
_HumanLink = tuple[str, str, str, str | None, str | None, str | None]
_Signal = tuple[str, str, str | None]


@dataclass(frozen=True)
class _Facts:
    subject: str
    payload: dict[str, Any]
    artifact_payload: dict[str, Any]
    name: str | None
    worth: str | None
    reason: str | None
    confidence: float | None
    is_owner: bool
    path: Path
    fingerprint: str


@dataclass
class _Graph:
    review: dict[str, dict[str, str]]
    aliases: dict[str, str]
    parents: dict[str, m.ParentRow]
    people: dict[str, m.PersonRow]
    slug_parent: dict[str, str]
    identifiers: dict[str, set[tuple[str, str, str | None]]]
    sources: dict[str, set[str]]
    merged_candidates: set[str]
    facts: list[_Facts]
    indexed_people: set[str]
    person_parent: dict[str, str]
    links: dict[str, m.LinkRow] = field(default_factory=dict)
    memberships: dict[str, set[str]] = field(default_factory=dict)
    human_links: dict[str, _HumanLink] = field(default_factory=dict)
    parent_signals: dict[str, _Signal] = field(default_factory=dict)
    child_signals: dict[str, _Signal] = field(default_factory=dict)
    verdict_keys: set[str] = field(default_factory=set)
    synthetic_members: set[str] = field(default_factory=set)
    artifacts: list[m.ArtifactRow] = field(default_factory=list)
    fact_rows: list[m.FactRow] = field(default_factory=list)
    synthetics: list[m.SyntheticProfileRow] = field(default_factory=list)
    research: list[m.ResearchRow] = field(default_factory=list)
    merge_verdicts: list[m.MergeVerdictRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stale_memberships: int = 0
    stale_synthetic_memberships: int = 0
    profiles: int = 0


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _sha(path: Path) -> str:
    return ProjectionValue.sha256(path.read_bytes())


def _object(path: Path, label: str | None = None) -> tuple[bytes, dict[str, Any]] | None:
    """Read one legacy JSON object; unlabeled cache entries are best-effort."""
    try:
        data = path.read_bytes()
        payload = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if label is None:
            return None
        raise LegacyImportError(f"cannot parse {label}: {exc}") from exc
    if not isinstance(payload, dict):
        if label is None:
            return None
        raise LegacyImportError(f"{label} must be a JSON object")
    return data, payload


def _artifact(
    key: str,
    kind: str,
    parent_id: str,
    path: Path | None,
    *,
    payload: object | None = None,
    person_id: str | None = None,
    candidate_key: str | None = None,
    input_fingerprint: str | None = None,
    data: bytes | None = None,
    fingerprint: str | None = None,
) -> m.ArtifactRow:
    content_fingerprint = fingerprint or ProjectionValue.sha256(data if data is not None else path.read_bytes())
    return m.ArtifactRow(
        key,
        kind,
        parent_id,
        str(path),
        content_fingerprint,
        m.ProjectionStatus.PROJECTED.value,
        person_id=person_id,
        candidate_key=candidate_key,
        input_fingerprint=input_fingerprint,
        payload_json=_json(payload) if payload is not None else None,
        projected_at=now_iso(),
    )


def _dossier(path: Path, parent_id: str, slug: str, name: str | None, person_id: str | None = None) -> m.ArtifactRow:
    try:
        body = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LegacyImportError(f"cannot parse dossier {slug}: {exc}") from exc
    key = f"dossier-person:{person_id}" if person_id else f"dossier:{parent_id}"
    payload = {"name": name or slug, "full_name": name or "", "path": str(path), "body": body}
    return _artifact(key, m.ArtifactKind.DOSSIER.value, parent_id, path, payload=payload, person_id=person_id)


def _csv_rows(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _review_rows(path: Path) -> dict[str, dict[str, str]]:
    return {key: row for row in _csv_rows(path) if (key := str(row.get("public_identifier") or "").strip().lower())}


def _facts(path: Path) -> _Facts | None:
    merged: dict[str, Any] = {}
    artifact: dict[str, Any] = {}
    name = worth = reason = None
    confidence: float | None = None
    is_owner = False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        artifact = record
        value = record.get("facts") if isinstance(record.get("facts"), dict) else record
        merged.update(value)
        name = ProjectionValue.text(value.get("canonical_name")) or name
        is_owner = bool(value.get("is_owner")) or is_owner
        verdict = value.get("network_worth")
        if isinstance(verdict, dict):
            decision = str(verdict.get("decision") or "").strip().lower()
            if decision in _MACHINE_WORTH:
                worth, reason = decision, ProjectionValue.text(verdict.get("reason"))
        confidence = ProjectionValue.number(value.get("confidence") or record.get("final_confidence")) or confidence
    if not merged:
        return None
    return _Facts(path.stem.lower(), merged, artifact, name, worth, reason, confidence, is_owner, path, _sha(path))


def _index(
    path: Path | None,
) -> tuple[
    dict[str, m.ParentRow], dict[str, m.PersonRow], dict[str, str], dict[str, set[tuple[str, str, str | None]]], list[m.ArtifactRow]
]:
    if path is None or not path.is_file():
        return {}, {}, {}, {}, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegacyImportError(f"cannot parse index.json: {exc}") from exc
    slugs = payload.get("slugs") if isinstance(payload.get("slugs"), dict) else {}
    parents: dict[str, m.ParentRow] = {}
    people: dict[str, m.PersonRow] = {}
    slug_parent: dict[str, str] = {}
    identifiers: dict[str, set[tuple[str, str, str | None]]] = {}
    dossiers: list[m.ArtifactRow] = []
    for raw_slug, raw in (payload.get("parents") or {}).items():
        if not isinstance(raw, dict):
            continue
        parent_slug = str(raw_slug)
        child_ids = [str((slugs.get(slug) or {}).get("person_id") or "").strip().lower() for slug in raw.get("children") or []]
        child_ids = [value for value in child_ids if value]
        parent_id = str(raw.get("parent_id") or "").strip().lower()
        parent_id = parent_id or (mint_parent_id(child_ids) if child_ids else "")
        if not parent_id:
            continue
        slug_parent[parent_slug] = parent_id
        parents[parent_id] = m.ParentRow(
            parent_id, f"parent-worth:{parent_id}", ProjectionValue.text(raw.get("name")), parent_slug, source=m.ReviewSource.LEGACY_MIGRATION.value
        )
        parent_path = path.parent / str(raw.get("path") or "")
        if parent_path.is_file():
            dossiers.append(_dossier(parent_path.resolve(), parent_id, parent_slug, ProjectionValue.text(raw.get("name"))))
        for child_slug in raw.get("children") or []:
            info = slugs.get(child_slug) or {}
            person_id = str(info.get("person_id") or "").strip().lower()
            if not person_id:
                continue
            name = ProjectionValue.text(info.get("name") or info.get("full_name"))
            people[person_id] = m.PersonRow(person_id, parent_id, str(child_slug), parent_slug, name)
            child_path = path.parent / str(info.get("path") or "")
            if child_path.is_file():
                dossiers.append(_dossier(child_path.resolve(), parent_id, str(child_slug), name, person_id))
    for field_name, kind, normalize in (("by_email", "email", normalize_email), ("by_phone", "phone", normalize_phone)):
        for display, owner_slugs in (payload.get(field_name) or {}).items():
            owners = {str((slugs.get(slug) or {}).get("person_id") or "").strip().lower() for slug in owner_slugs or []} - {""}
            normalized = normalize(display)
            if len(owners) == 1 and normalized:
                identifiers.setdefault(next(iter(owners)), set()).add((kind, normalized, str(display)))
    return parents, people, slug_parent, identifiers, dossiers


def _merged(path: Path | None) -> tuple[dict[str, set[str]], set[str]]:
    sources: dict[str, set[str]] = {}
    candidates: set[str] = set()
    for row in _csv_rows(path):
        person_id = str(row.get("id") or "").strip().lower()
        if not person_id:
            continue
        sources.setdefault(person_id, set()).update(
            value.strip() for value in str(row.get("source_channels") or "").split(",") if value.strip()
        )
        if person_id.startswith("candidate:"):
            candidates.add(person_id)
    return sources, candidates


def _owner(path: Path | None) -> m.OwnerContextRow | None:
    if path is None or not path.is_file():
        return None
    content, payload = _object(path, "owner context")
    return m.OwnerContextRow("owner", _json(payload), str(path), hashlib.sha256(content).hexdigest(), now_iso())


def _human_signal(row: dict[str, str]) -> tuple[str, str] | None:
    mark = str(row.get("network_worth") or "").strip().lower()
    if mark not in _HUMAN_WORTH:
        if (
            str(row.get("action") or "").strip().lower() == m.ReviewAction.EXCLUDE.value
            and str(row.get("approved") or "").strip().lower() == m.ApprovedState.YES.value
        ):
            mark = m.HumanWorth.NO.value
        else:
            return None
    return mark, str(row.get("updated_at") or "")


def _parent(g: _Graph, key: str | None) -> str | None:
    value = str(key or "").strip().lower()
    alias = g.aliases.get(value, value)
    return g.person_parent.get(value) or g.person_parent.get(alias) or g.slug_parent.get(value)


def _metadata_only_review(row: dict[str, str]) -> bool:
    return not any(str(value or "").strip() for key, value in row.items() if key not in _REVIEW_METADATA)


def _load_graph(review_csv: Path, index_json: Path | None, facts_dir: Path | None, merged_people_csv: Path | None) -> _Graph:
    review = _review_rows(review_csv)
    parents, people, slug_parent, identifiers, dossiers = _index(index_json)
    sources, candidates = _merged(merged_people_csv)
    parsed = (
        [item for item in (_facts(path) for path in sorted(facts_dir.glob("*.jsonl"))) if item] if facts_dir and facts_dir.is_dir() else []
    )
    graph = _Graph(
        review,
        message_linkedin_aliases(list(review.values())),
        parents,
        people,
        slug_parent,
        identifiers,
        sources,
        candidates,
        parsed,
        set(people),
        {},
        artifacts=dossiers,
    )
    for fact in parsed:
        alias = graph.aliases.get(fact.subject, fact.subject)
        existing = graph.people.get(fact.subject)
        indexed = existing or graph.people.get(alias)
        parent_id = indexed.parent_id if indexed else mint_parent_id([alias])
        graph.parents.setdefault(
            parent_id, m.ParentRow(parent_id, f"parent-worth:{parent_id}", fact.name, alias, source=m.ReviewSource.LEGACY_MIGRATION.value)
        )
        graph.people[fact.subject] = m.PersonRow(
            fact.subject,
            parent_id,
            existing.child_slug if existing else fact.subject,
            existing.parent_slug if existing else alias,
            fact.name or (existing.display_name if existing else None),
            int(fact.is_owner),
            int(fact.subject.startswith(MESSAGE_LINKEDIN_PREFIX)),
            _json(fact.payload),
            fact.confidence,
            now_iso(),
        )
    graph.person_parent = {key: row.parent_id for key, row in graph.people.items()}
    _machine_worth(graph)
    return graph


def _machine_worth(g: _Graph) -> None:
    priority = {m.MachineWorth.NO.value: 0, m.MachineWorth.MAYBE.value: 1, m.MachineWorth.YES.value: 2}
    grouped: dict[str, list[_Facts]] = {}
    for fact in g.facts:
        grouped.setdefault(g.person_parent[fact.subject], []).append(fact)
    for parent_id, members in grouped.items():
        winner = max(members, key=lambda item: (priority[item.worth or "maybe"], item.subject))
        current = g.parents[parent_id]
        g.parents[parent_id] = replace(
            current,
            display_name=current.display_name or winner.name,
            machine_worth=winner.worth or m.MachineWorth.MAYBE.value,
            machine_worth_reason=winner.reason,
            updated_at=now_iso(),
        )


def _split(value: object) -> list[str]:
    return sorted({item.strip() for item in str(value or "").split("|") if item.strip()})


def _review(g: _Graph) -> None:
    for key, row in g.review.items():
        if key.startswith(m.PARENT_WORTH_PREFIX):
            parent_id = key.removeprefix(m.PARENT_WORTH_PREFIX)
            signal = _human_signal(row)
            if signal and parent_id in g.parents:
                g.parent_signals[parent_id] = (*signal, ProjectionValue.text(row.get("user_worth_note")))
            elif signal:
                g.errors.append(f"{key}: worth owner not found")
            continue
        action = str(row.get("action") or "").strip().lower() or None
        approved = str(row.get("approved") or "").strip().lower() or None
        reject = str(row.get("llm_reject") or "").strip().lower() or None
        if action and action not in _ACTIONS:
            g.errors.append(f"{key}: unknown action {action!r}")
        if approved and approved not in _APPROVALS:
            g.errors.append(f"{key}: unknown approved {approved!r}")
        if reject and reject not in m.LLM_REJECT_VALUES:
            g.errors.append(f"{key}: unknown llm_reject {reject!r}")
        person_id = str(row.get("person_id") or "").strip().lower()
        parent_id = _parent(g, person_id or key)
        if not parent_id and not person_id and _metadata_only_review(row):
            continue
        if not parent_id:
            seed = g.aliases.get(person_id or key, person_id or key)
            parent_id = mint_parent_id([seed])
            g.parents.setdefault(
                parent_id,
                m.ParentRow(parent_id, f"parent-worth:{parent_id}", display_slug=seed, source=m.ReviewSource.LEGACY_MIGRATION.value),
            )
            if person_id:
                g.people[person_id] = m.PersonRow(
                    person_id, parent_id, person_id, seed, is_ghost=int(person_id.startswith(MESSAGE_LINKEDIN_PREFIX))
                )
                g.person_parent[person_id] = parent_id
        proposed_url = ProjectionValue.text(row.get("new_linkedin_url"))
        proposed_pub = ProjectionValue.text(row.get("new_public_identifier"))
        machine_proposal = action == m.ReviewAction.RETARGET.value and approved not in {m.ApprovedState.YES.value, m.ApprovedState.NO.value}
        g.links[key] = m.LinkRow(
            key,
            parent_id,
            ProjectionValue.text(row.get("public_identifier")) or key,
            _kind(key).value,
            ProjectionValue.text(row.get("linkedin_url")),
            machine_action=action if approved != m.ApprovedState.YES.value else None,
            machine_approved=approved if approved == m.ApprovedState.AUTO.value else None,
            machine_proposed_url=proposed_url if machine_proposal else None,
            machine_proposed_public_identifier=proposed_pub if machine_proposal else None,
            machine_confidence=ProjectionValue.number(row.get("confidence")),
            machine_reason=ProjectionValue.text(row.get("reason")),
            machine_reject=reject,
            machine_reject_confidence=ProjectionValue.number(row.get("llm_reject_confidence")),
            machine_reject_reason=ProjectionValue.text(row.get("llm_reject_reason")),
            authoritative_detach=int(
                action == m.ReviewAction.DETACH.value
                and (ProjectionValue.number(row.get("confidence")) or 0)
                >= m.IDENTITY_THRESHOLDS["detach"]
            ),
            candidate_origin=int(key.startswith("candidate:")),
            raw_import=int(key.startswith("candidate:") and not (proposed_url or proposed_pub)),
            judgment_fingerprint=ProjectionValue.text(row.get("llm_judge_fingerprint")),
            source=ProjectionValue.text(row.get("source")),
            updated_at=ProjectionValue.text(row.get("updated_at")),
        )
        if person_id:
            g.memberships.setdefault(key, set()).add(person_id)
            for value in _split(row.get("match_emails")):
                if normalized := normalize_email(value):
                    g.identifiers.setdefault(person_id, set()).add(("email", normalized, value))
            for value in _split(row.get("match_phones")):
                if normalized := normalize_phone(value):
                    g.identifiers.setdefault(person_id, set()).add(("phone", normalized, value))
        if approved in {m.ApprovedState.YES.value, m.ApprovedState.NO.value} and action:
            g.human_links[key] = (
                action,
                approved,
                ProjectionValue.text(row.get("source")) or m.ReviewSource.REVIEW.value,
                ProjectionValue.text(row.get("updated_at")),
                proposed_url,
                proposed_pub,
            )
        if signal := _human_signal(row):
            candidate = (*signal, ProjectionValue.text(row.get("user_worth_note")))
            for child in {key, person_id} - {""}:
                if child not in g.child_signals or candidate[1] > g.child_signals[child][1]:
                    g.child_signals[child] = candidate


def _kind(key: str) -> m.RowKind:
    if key.startswith(m.PARENT_WORTH_PREFIX):
        return m.RowKind.PARENT
    if key.startswith("candidate:email:"):
        return m.RowKind.CANDIDATE_EMAIL
    if key.startswith("candidate:phone:"):
        return m.RowKind.CANDIDATE_PHONE
    if key.startswith(MESSAGE_LINKEDIN_PREFIX):
        return m.RowKind.MESSAGE_LINKEDIN
    return m.RowKind.PERSON_UUID if _UUID_RE.match(key) else m.RowKind.PUB


def _verdicts(g: _Graph, path: Path | None) -> None:
    if path is None or not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        person_ids = [str(value).strip().lower() for value in payload.get("person_ids") or [] if str(value).strip()]
        raw = [value for value in person_ids if value.startswith("candidate:")]
        key = (
            next(iter(raw), "")
            if payload.get("no_link") and raw
            else str(payload.get("candidate_key") or "").strip().lower() or next(iter(person_ids), "")
        )
        owners = {g.person_parent.get(person_id) for person_id in person_ids} - {None}
        if slug_owner := g.slug_parent.get(str(payload.get("parent_slug") or "").strip()):
            owners.add(slug_owner)
        if len(owners) != 1:
            g.errors.append(f"verdict:{key or '?'}: cannot resolve one parent")
            continue
        parent_id = next(iter(owners))
        for person_id in person_ids:
            if person_id not in g.people:
                g.people[person_id] = m.PersonRow(person_id, parent_id, person_id, str(payload.get("parent_slug") or ""))
                g.person_parent[person_id] = parent_id
            elif g.people[person_id].parent_id != parent_id:
                g.errors.append(f"verdict:{key}: person {person_id} belongs to another parent")
        if not key:
            continue
        g.verdict_keys.add(key)
        verdict = payload.get("verdict") if isinstance(payload.get("verdict"), dict) else {}
        prior = g.links.get(key)
        g.links[key] = replace(
            prior or m.LinkRow(key, parent_id, key, _kind(key).value),
            parent_id=parent_id,
            machine_judgment=ProjectionValue.text(verdict.get("verdict")),
            machine_confidence=ProjectionValue.number(verdict.get("confidence")),
            machine_reason=ProjectionValue.text(verdict.get("reason")),
            paid_profile=1,
            authoritative_detach=int(
                bool(prior and prior.machine_action == m.ReviewAction.DETACH.value)
                and str(verdict.get("verdict") or "") == "wrong_person"
                and (ProjectionValue.number(verdict.get("confidence")) or 0)
                >= m.IDENTITY_THRESHOLDS["detach"]
            ),
            judgment_fingerprint=ProjectionValue.text(payload.get("fingerprint") or payload.get("judge_input_fingerprint")),
            judgment_artifact_path=str(path),
            judgment_payload_json=_json(payload),
        )
        g.memberships.setdefault(key, set()).update(person_ids)


def _synthetic(g: _Graph, path: Path | None) -> None:
    fingerprint = _sha(path) if path and path.is_file() else None
    for row in _csv_rows(path):
        pub = str(row.get("public_identifier") or "").strip().lower()
        person_ids = [str(value).strip().lower() for value in json.loads(row.get("source_person_ids") or "[]")]
        g.synthetic_members.update(person_ids)
        indexed_owners = {g.person_parent.get(value) for value in person_ids if value in g.indexed_people} - {None}
        owners = indexed_owners or ({g.person_parent.get(value) for value in person_ids} - {None})
        source_key = str(row.get("source_candidate_public_identifier") or "").strip().lower()
        slug_owner = g.slug_parent.get(str(row.get("source_parent_slug") or "").strip())
        if not indexed_owners and source_key in g.links:
            owners = {g.links[source_key].parent_id}
        elif not indexed_owners and slug_owner:
            owners = {slug_owner}
        if not owners:
            seeds = person_ids or [str(row.get("id") or pub).strip().lower()]
            parent_id = mint_parent_id(seeds)
            owners.add(parent_id)
            g.parents.setdefault(
                parent_id,
                m.ParentRow(
                    parent_id,
                    f"parent-worth:{parent_id}",
                    ProjectionValue.text(row.get("full_name")),
                    ProjectionValue.text(row.get("source_parent_slug")) or pub,
                    source=m.ReviewSource.LEGACY_MIGRATION.value,
                ),
            )
        if not pub or len(owners) != 1:
            g.errors.append(f"synthetic:{pub or '?'}: cannot resolve one parent")
            continue
        parent_id = next(iter(owners))
        person_ids = person_ids or [str(row.get("id") or pub).strip().lower()]
        current: list[str] = []
        for person_id in person_ids:
            prior = g.people.get(person_id)
            if prior and prior.parent_id != parent_id:
                g.stale_synthetic_memberships += 1
                continue
            if not prior:
                g.people[person_id] = m.PersonRow(
                    person_id, parent_id, person_id, ProjectionValue.text(row.get("source_parent_slug")) or pub, ProjectionValue.text(row.get("full_name"))
                )
                g.person_parent[person_id] = parent_id
            current.append(person_id)
            g.sources.setdefault(person_id, set()).update(
                value.strip() for value in str(row.get("source_channels") or "").split(",") if value.strip()
            )
        approved = str(row.get("approved") or "").strip().lower()
        g.links[pub] = m.LinkRow(
            pub,
            parent_id,
            pub,
            m.RowKind.SYNTHETIC.value,
            ProjectionValue.text(row.get("linkedin_url")),
            ProjectionValue.text(row.get("full_name")),
            machine_action=m.ReviewAction.VERIFY.value if approved == "auto" else None,
            machine_approved="auto" if approved == "auto" else None,
            source=m.ReviewSource.LEGACY_MIGRATION.value,
        )
        g.memberships[pub] = set(current)
        if approved in {"yes", "no"}:
            g.human_links[pub] = (
                m.ReviewAction.VERIFY.value if approved == "yes" else m.ReviewAction.DETACH.value,
                m.ApprovedState.YES.value,
                m.ReviewSource.REVIEW.value,
                ProjectionValue.text(row.get("enriched_at")),
                None,
                None,
            )
        artifact_key = f"synthetic:{pub}"
        g.synthetics.append(
            m.SyntheticProfileRow(
                pub,
                pub,
                _json(row),
                artifact_key,
                linkedin_url=ProjectionValue.text(row.get("linkedin_url")),
                name=ProjectionValue.text(row.get("full_name"))
                or " ".join(filter(None, (ProjectionValue.text(row.get("first_name")), ProjectionValue.text(row.get("last_name")))))
                or None,
                updated_at=ProjectionValue.text(row.get("enriched_at")),
            )
        )
        g.artifacts.append(
            _artifact(
                artifact_key,
                m.ArtifactKind.SYNTHETIC.value,
                parent_id,
                path,
                payload=row,
                candidate_key=pub,
                fingerprint=fingerprint or "0" * 64,
            )
        )


def _finish_graph(g: _Graph) -> None:
    fact_keys = {fact.subject for fact in g.facts if fact.worth is not None or fact.subject in g.merged_candidates}
    displayed = {
        person_id
        for key in g.verdict_keys | {key for key, row in g.links.items() if row.kind == m.RowKind.SYNTHETIC.value}
        for person_id in g.memberships.get(key, set())
    } | g.synthetic_members
    covered = {key for key in fact_keys if key in displayed and key not in g.verdict_keys and key not in g.human_links}
    for key in covered:
        g.links.pop(key, None)
        g.memberships.pop(key, None)
    fact_keys -= covered
    for key in fact_keys - set(g.links):
        parent_id = g.person_parent[key]
        g.links[key] = m.LinkRow(key, parent_id, key, _kind(key).value, source=m.ReviewSource.LEGACY_MIGRATION.value)
        g.memberships[key] = {key}
    g.links = {key: replace(row, candidate_origin=0, raw_import=0) for key, row in g.links.items()}
    for key in fact_keys:
        row = g.links[key]
        resolved = (
            (
                row.machine_action == m.ReviewAction.RETARGET.value
                and bool(row.machine_proposed_url or row.machine_proposed_public_identifier)
            )
            or (row.machine_action in {m.ReviewAction.VERIFY.value, m.ReviewAction.DETACH.value} and row.machine_approved in _APPROVALS)
            or key in g.human_links
        )
        g.links[key] = replace(row, candidate_origin=int(key.startswith("candidate:")), raw_import=int(not resolved))
    _machine_worth(g)
    children: dict[str, list[str]] = {}
    for person in g.people.values():
        children.setdefault(person.parent_id, []).append(person.person_id)
    for parent_id in g.parents:
        if parent_id in g.parent_signals:
            continue
        signals = [g.child_signals[key] for key in children.get(parent_id, []) if key in g.child_signals]
        if signals:
            g.parent_signals[parent_id] = max(signals, key=lambda item: item[1])
    for key, person_ids in g.memberships.items():
        mismatched = [value for value in person_ids if value not in g.people or g.people[value].parent_id != g.links[key].parent_id]
        if mismatched and all(value in g.indexed_people for value in mismatched) and g.links[key].source == m.ReviewSource.RECONCILE.value:
            g.memberships[key] -= set(mismatched)
            g.stale_memberships += len(mismatched)
            mismatched = []
        if key in g.verdict_keys and not g.memberships[key]:
            g.errors.append("legacy verdict has no current candidate membership")
        if mismatched:
            g.errors.append(
                f"{_kind(key).value} candidate has {len(mismatched)} cross-parent members "
                f"(indexed={sum(value in g.indexed_people for value in mismatched)}, "
                f"members={len(person_ids)}, source={g.links[key].source or 'none'})"
            )
    if g.errors:
        raise LegacyImportError("; ".join(g.errors[:20]))


def _embedded_artifacts(
    g: _Graph, profile_cache_dir: Path | None, avatar_dir: Path | None, research_dir: Path | None, raw_dir: Path | None
) -> None:
    profiles: dict[str, tuple[Path, bytes, dict[str, Any]]] = {}
    if profile_cache_dir and profile_cache_dir.is_dir():
        for path in sorted(profile_cache_dir.glob("*.json")):
            if path.name == "_metadata.json":
                continue
            parsed = _object(path)
            if parsed is None:
                continue
            content, payload = parsed
            normalized = payload.get("normalized_profile")
            normalized = normalized if isinstance(normalized, dict) else {}
            keys = {
                path.stem.lower(),
                str(payload.get("public_identifier") or "").strip().lower(),
                str(normalized.get("public_identifier") or "").strip().lower(),
                extract_public_identifier(str(payload.get("linkedin_url") or "")).lower(),
                extract_public_identifier(str(normalized.get("linkedin_url") or "")).lower(),
            } - {""}
            for key in keys:
                profiles.setdefault(key, (path.resolve(), content, payload))
    for key, link in g.links.items():
        human = g.human_links.get(key)
        pubs = (
            human[5] if human else None,
            extract_public_identifier(human[4] or "") if human else None,
            link.machine_proposed_public_identifier,
            extract_public_identifier(link.machine_proposed_url or ""),
            link.public_identifier,
            extract_public_identifier(link.linkedin_url or ""),
        )
        cached = next((profiles.get(str(pub).strip().lower()) for pub in pubs if pub and profiles.get(str(pub).strip().lower())), None)
        if cached:
            path, content, payload = cached
            g.artifacts.append(
                _artifact(
                    f"profile:{key}", m.ArtifactKind.PROFILE.value, link.parent_id, path, payload=payload, candidate_key=key, data=content
                )
            )
            g.profiles += 1
    for fact in g.facts:
        artifact_key = f"facts:{fact.subject}"
        parent_id = g.person_parent[fact.subject]
        g.artifacts.append(
            _artifact(
                artifact_key,
                m.ArtifactKind.FACTS.value,
                parent_id,
                fact.path,
                payload=fact.artifact_payload,
                person_id=fact.subject,
                input_fingerprint=ProjectionValue.text(fact.artifact_payload.get("input_evidence_fingerprint")),
                fingerprint=fact.fingerprint,
            )
        )
        g.fact_rows.append(
            m.FactRow(
                fact.subject,
                parent_id,
                artifact_key,
                fact.subject,
                fact.worth,
                fact.reason,
                fact.confidence,
                int(fact.is_owner),
                _json(fact.payload),
                now_iso(),
            )
        )
    if raw_dir and raw_dir.is_dir():
        for path in sorted(raw_dir.glob("*.json")):
            if path.name == "manifest.json":
                continue
            data, payload = _object(path, f"source bundle {path.name}")
            person_id = path.stem.lower()
            if str(payload.get("person_id") or "").strip().lower() != person_id:
                raise LegacyImportError(f"source bundle person mismatch: {path.name}")
            if person_id not in g.person_parent:
                raise LegacyImportError(f"source bundle person is absent from graph: {person_id}")
            g.artifacts.append(
                _artifact(
                    f"source-bundle:{person_id}",
                    m.ArtifactKind.SOURCE_BUNDLE.value,
                    g.person_parent[person_id],
                    path.resolve(),
                    payload=payload,
                    person_id=person_id,
                    data=data,
                )
            )
    _avatars(g, avatar_dir)
    _research(g, research_dir)


def _avatars(g: _Graph, directory: Path | None) -> None:
    if directory is None or not directory.is_dir():
        return
    for key, link in g.links.items():
        human = g.human_links.get(key)
        pubs = (human[5] if human else None, link.machine_proposed_public_identifier, link.public_identifier)
        for pub in dict.fromkeys(pubs):
            if not pub:
                continue
            digest = hashlib.sha256(pub.strip().lower().encode()).hexdigest()[:24]
            path = directory / f"{digest}.image"
            if not path.is_file():
                continue
            data = path.read_bytes()
            payload = {"base64": base64.b64encode(data).decode("ascii"), "content_type": ProjectionValue.content_type(data)}
            g.artifacts.append(
                _artifact(
                    f"avatar:{key}",
                    m.ArtifactKind.AVATAR.value,
                    link.parent_id,
                    path.resolve(),
                    payload=payload,
                    candidate_key=key,
                    data=data,
                )
            )
            break


def _research(g: _Graph, directory: Path | None) -> None:
    if directory is None or not directory.is_dir():
        return
    for result_dir in sorted(path for path in directory.iterdir() if path.is_dir()):
        path = result_dir / "01_research_parallel.json"
        owner = g.slug_parent.get(result_dir.name)
        if not path.is_file() or not owner:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LegacyImportError(f"cannot parse research {result_dir.name}: {exc}") from exc
        artifact_key = f"research:{result_dir.name}"
        g.artifacts.append(_artifact(artifact_key, m.ArtifactKind.RESEARCH.value, owner, path, payload=payload))
        g.research.append(
            m.ResearchRow(
                result_dir.name,
                owner,
                m.ResearchStatus.COMPLETE.value,
                artifact_key=artifact_key,
                result_json=_json(payload),
                updated_at=now_iso(),
            )
        )


def _merges(g: _Graph, verdict_path: Path | None, accepted_path: Path | None) -> None:
    slug_people = {str(row.child_slug): row.person_id for row in g.people.values() if row.child_slug}
    accepted = {
        frozenset({str(row.get("slug_a") or ""), str(row.get("slug_b") or "")})
        for row in _csv_rows(accepted_path)
        if row.get("slug_a") and row.get("slug_b")
    }
    for row in _csv_rows(verdict_path):
        slug_a, slug_b = str(row.get("slug_a") or ""), str(row.get("slug_b") or "")
        person_a, person_b = slug_people.get(slug_a), slug_people.get(slug_b)
        signature = str(row.get("sig") or "")
        if not person_a or not person_b or person_a == person_b or not signature:
            continue
        if person_a > person_b:
            person_a, person_b, slug_a, slug_b = person_b, person_a, slug_b, slug_a
        g.merge_verdicts.append(
            m.MergeVerdictRow(
                person_a,
                person_b,
                slug_a,
                slug_b,
                signature,
                str(row.get("judge") or "llm"),
                int(str(row.get("same_person") or "").lower() == "true"),
                ProjectionValue.number(row.get("confidence")) or 0.0,
                int(str(row.get("tone_consistent") or "").lower() == "true"),
                str(row.get("reason") or ""),
                int(frozenset({slug_a, slug_b}) in accepted),
                now_iso(),
            )
        )


def _commit(db: Db, g: _Graph, owner: m.OwnerContextRow | None) -> None:
    tables = (
        "parents",
        "people",
        "person_identifiers",
        "person_sources",
        "links",
        "candidate_people",
        "artifacts",
        "facts",
        "synthetic_profiles",
        "research",
        "guidance",
        "jobs",
        "merge_verdicts",
    )
    with db.transaction() as conn:
        occupied = [name for name in tables if conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()]
        if occupied:
            raise LegacyImportError(f"canonical DB is not empty: {', '.join(occupied)}")
        if owner:
            db._write("owner_context", owner, conn)
        projection = m.CanonicalGraphProjection(
            parents=tuple(g.parents.values()),
            people=tuple(g.people.values()),
            identifiers=tuple(
                m.PersonIdentifierRow(person_id, kind, normalized, display)
                for person_id, values in g.identifiers.items()
                if person_id in g.people
                for kind, normalized, display in sorted(values)
            ),
            sources=tuple(
                m.PersonSourceRow(person_id, source)
                for person_id, values in g.sources.items()
                if person_id in g.people
                for source in sorted(values)
            ),
        )
        LegacyGraphMigration._apply(conn, projection)
        for row in g.links.values():
            db._project_candidate(row, conn=conn)
        for key, person_ids in g.memberships.items():
            db._replace_children(
                "candidate_people",
                key,
                tuple(m.CandidatePersonRow(key, person_id, g.links[key].parent_id) for person_id in sorted(person_ids)),
                conn=conn,
            )
        for row in g.artifacts:
            db._project_artifact(row, conn=conn)
        for table, rows in (
            ("facts", g.fact_rows),
            ("synthetic_profiles", g.synthetics),
            ("research", g.research),
            ("merge_verdicts", g.merge_verdicts),
        ):
            for row in rows:
                db._write(table, row, conn)
        for parent_id, (value, decided_at, note) in g.parent_signals.items():
            conn.execute(
                "UPDATE parents SET human_worth=?, human_worth_note=?, human_worth_source=?, human_worth_at=? WHERE parent_id=?",
                (value, note, m.ReviewSource.REVIEW.value, decided_at or now_iso(), parent_id),
            )
        for key, (action, approved, source, at, url, pub) in g.human_links.items():
            conn.execute(
                "UPDATE links SET decision_action=?, decision_approved=?, decision_source=?, decided_at=?, replacement_url=?, replacement_public_identifier=? WHERE row_key=?",
                (action, approved, source, at or now_iso(), url, pub, key),
            )
        IdentityPolicy.settle_human_families(conn, g.parents)
        IdentityPolicy.clear_machine_winner_conflicts(conn, g.parents)
        conn.execute("INSERT INTO meta (key, value) VALUES ('legacy_imported_at', ?)", (now_iso(),))
        violations = list(conn.execute("PRAGMA foreign_key_check"))
        if violations:
            raise LegacyImportError(f"legacy import left {len(violations)} orphan relations")


def import_legacy(
    db: Db,
    *,
    review_csv: Path,
    synthetic_csv: Path | None = None,
    index_json: Path | None = None,
    facts_dir: Path | None = None,
    verdicts_jsonl: Path | None = None,
    research_dir: Path | None = None,
    merged_people_csv: Path | None = None,
    owner_json: Path | None = None,
    profile_cache_dir: Path | None = None,
    avatar_dir: Path | None = None,
    merge_verdicts_csv: Path | None = None,
    merge_csv: Path | None = None,
    raw_dir: Path | None = None,
) -> dict[str, int]:
    """Import old fixed artifacts once; unresolved ownership aborts all writes."""
    owner = _owner(owner_json)
    graph = _load_graph(review_csv, index_json, facts_dir, merged_people_csv)
    _review(graph)
    _verdicts(graph, verdicts_jsonl)
    _synthetic(graph, synthetic_csv)
    _finish_graph(graph)
    _embedded_artifacts(graph, profile_cache_dir, avatar_dir, research_dir, raw_dir)
    _merges(graph, merge_verdicts_csv, merge_csv)
    _commit(db, graph, owner)
    return {
        "people": len(graph.people),
        "parents": len(graph.parents),
        "links": len(graph.links),
        "person_sources": sum(len(values) for key, values in graph.sources.items() if key in graph.people),
        "candidate_people": sum(map(len, graph.memberships.values())),
        "artifacts": len(graph.artifacts),
        "facts": len(graph.fact_rows),
        "synthetic_profiles": len(graph.synthetics),
        "research": len(graph.research),
        "human_worth": len(graph.parent_signals),
        "human_identity": len(graph.human_links),
        "stale_memberships": graph.stale_memberships,
        "stale_synthetic_memberships": graph.stale_synthetic_memberships,
        "merge_verdicts": len(graph.merge_verdicts),
        "owner_context": int(owner is not None),
        "profiles": graph.profiles,
    }

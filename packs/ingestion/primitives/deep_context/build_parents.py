#!/usr/bin/env python3
"""Apply accepted parent merges and refresh only changed parent dossiers."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso, parse_json_object
from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    PARENT_TEMPLATE,
    PARENTS_DIR,
    PARENTS_MANIFEST,
    emit,
    slugify,
)
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactReplacement,
    ArtifactRow,
    IdentifierKind,
    MergeVerdictRow,
    PARENT_DOSSIER_ARTIFACT_PREFIX,
    PersonRow,
    ProjectionStatus,
)
from packs.ingestion.primitives.deep_context.db.queries import (
    artifacts as artifact_rows,
    facts as fact_rows,
    identifiers as identifier_rows,
    merge_verdicts,
    parents as parent_rows,
    people as person_rows,
    sources as source_rows,
)
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.dossier.facts import (
    headline,
    merge_fact_records,
)
from packs.ingestion.primitives.deep_context.dossier.models import (
    FactRecord,
    SynthesizedFacts,
)
from packs.ingestion.primitives.deep_context.merge_candidates.blocking import connected_components
from packs.ingestion.primitives.deep_context.parents import rendering as parent_rendering
from packs.ingestion.primitives.deep_context.parents.assignment import load_assignment
from packs.ingestion.primitives.deep_context.parents.models import ChildEntry, ParentPlan
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest

PARENT_RENDER_CONTRACT = "parent-dossier-v1"


class BuildParentsManifest(StageManifest):
    source: str = "build_parents"
    merge_components: int = 0
    parents_changed: int = 0
    parents_merged: int = 0
    singletons_written: int = 0
    owner_excluded: int = 0
    orphans_removed: int = 0
    parents_dir: str = ""
    elapsed_ms: int = 0


def _accepted_components(db: Db) -> tuple[tuple[str, ...], ...]:
    """Current parent components linked by their latest accepted verdict."""
    parent_by_person = {row.person_id: row.parent_id for row in person_rows(db)}
    latest: dict[tuple[str, str], MergeVerdictRow] = {}
    for row in merge_verdicts(db):
        left: str | None = parent_by_person.get(row.person_a)
        right: str | None = parent_by_person.get(row.person_b)
        if not left or not right or left == right:
            continue
        key = tuple(sorted((left, right)))
        prior: MergeVerdictRow | None = latest.get(key)
        rank = (row.updated_at or "", row.person_a, row.person_b)
        if prior is None or rank >= (prior.updated_at or "", prior.person_a, prior.person_b):
            latest[key] = row

    edges = [key for key, row in latest.items() if row.accepted]
    nodes = sorted({parent_id for edge in edges for parent_id in edge})
    return tuple(tuple(sorted(group)) for group in connected_components(nodes, edges))


def _parent_plans(db: Db) -> tuple[tuple[ParentPlan, ...], int]:
    identifiers: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for row in identifier_rows(db):
        value = row.display_value or row.normalized_value
        values = identifiers[row.person_id][row.kind]
        if value not in values:
            values.append(value)
    sources: dict[str, list[str]] = defaultdict(list)
    for row in source_rows(db):
        if row.source not in sources[row.person_id]:
            sources[row.person_id].append(row.source)
    people_by_parent: dict[str, list[PersonRow]] = defaultdict(list)
    for row in person_rows(db):
        people_by_parent[row.parent_id].append(row)
    facts_by_parent: dict[str, list[FactRecord]] = defaultdict(list)
    for row in fact_rows(db):
        payload = parse_json_object(row.facts_json)
        if payload:
            record: FactRecord | None = FactRecord.from_payload({"facts": payload})
            if record is not None:
                facts_by_parent[row.parent_id].append(record)
    plans: list[ParentPlan] = []
    owner_excluded = 0
    for parent in parent_rows(db):
        members = sorted(
            people_by_parent.get(parent.parent_id, ()),
            key=lambda row: (row.child_slug or row.person_id, row.person_id),
        )
        visible = [row for row in members if not row.is_owner]
        owner_excluded += len(members) - len(visible)
        if not visible:
            continue
        merged = merge_fact_records(facts_by_parent.get(parent.parent_id, [])) or SynthesizedFacts()
        name = str(merged.canonical_name or parent.display_name or visible[0].display_name or "person")
        slug = parent.display_slug or slugify(name, parent.parent_id)
        emails: list[str] = []
        phones: list[str] = []
        children: list[ChildEntry] = []
        for person in visible:
            child_name = str(person.display_name or person.child_slug or person.person_id)
            child_slug = person.child_slug or person.person_id
            children.append(
                ChildEntry(
                    child_slug,
                    child_name,
                    0.0,
                    "",
                    tuple(sources.get(person.person_id, ())),
                    person.person_id,
                )
            )
            for value in identifiers.get(person.person_id, {}).get(IdentifierKind.EMAIL.value, ()):
                if value not in emails:
                    emails.append(value)
            for value in identifiers.get(person.person_id, {}).get(IdentifierKind.PHONE.value, ()):
                if value not in phones:
                    phones.append(value)
        plans.append(
            ParentPlan(
                parent.parent_id,
                slug,
                name,
                tuple(emails),
                tuple(phones),
                tuple(children),
                merged,
            )
        )
    return tuple(plans), owner_excluded


def _render_input_fingerprint(plan: ParentPlan) -> str:
    plan_payload = asdict(plan)
    plan_payload["merged"] = plan.merged.to_payload()
    payload = json.dumps(
        {"contract": PARENT_RENDER_CONTRACT, "plan": plan_payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class BuildParents(Node):
    """Incrementally merge parent families and write changed parent dossiers."""

    name = "deep_parents"
    inputs = ()
    outputs = (Artifact(path=PARENT_TEMPLATE, writes="upsert", required=False),)
    payload = BuildParentsManifest
    manifest = str(PARENTS_MANIFEST)

    def __init__(self, *, db: Db, parents_dir: Path | None = None) -> None:
        self.db = db
        self.parents_dir = Path(parents_dir or PARENTS_DIR)

    def bindings(self) -> dict[str, str]:
        return {
            PARENT_TEMPLATE: str(self.parents_dir / "{slug}.md"),
            self.manifest: str(self.parents_dir / "manifest.json"),
        }

    def execute(self) -> BuildParentsManifest:
        started = time.monotonic()
        components = _accepted_components(self.db)
        assignment = load_assignment(self.db)
        parents_merged = 0
        for component in components:
            survivor = assignment.elect(list(component))
            for absorbed in component:
                if absorbed == survivor:
                    continue
                self.db.merge_parents(survivor, absorbed)
                parents_merged += 1

        plans, owner_excluded = _parent_plans(self.db)
        prior_artifacts: dict[str, list[ArtifactRow]] = defaultdict(list)
        for row in artifact_rows(self.db, kind=ArtifactKind.DOSSIER.value):
            if row.kind == ArtifactKind.DOSSIER.value and row.person_id is None and row.candidate_key is None:
                prior_artifacts[row.parent_id].append(row)
        owners_by_path: dict[str, set[str]] = defaultdict(set)
        for parent_id, rows in prior_artifacts.items():
            for row in rows:
                owners_by_path[row.path].add(parent_id)
        colliding_paths = {path for path, parent_ids in owners_by_path.items() if len(parent_ids) > 1}

        self.parents_dir.mkdir(parents=True, exist_ok=True)
        replacements = []
        parents_changed = 0
        singleton_written = 0
        resolved_parents_dir = self.parents_dir.resolve()
        for plan in plans:
            singleton = len(plan.confirmed) == 1
            file_slug = slugify(plan.name, plan.parent_id)
            path = self.parents_dir / f"{file_slug}.md"
            artifact_key = f"{PARENT_DOSSIER_ARTIFACT_PREFIX}{plan.parent_id}"
            composed_key = f"dossier:{plan.parent_id}"
            input_fingerprint = _render_input_fingerprint(plan)
            prior = prior_artifacts.get(plan.parent_id, [])
            current: ArtifactRow | None = next((row for row in prior if row.artifact_key == artifact_key), None)
            composed = tuple(
                row
                for row in prior
                if row.artifact_key == composed_key and Path(row.path).resolve().parent != resolved_parents_dir
            )
            disk_fingerprint = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            changed = (
                current is None
                or current.input_fingerprint != input_fingerprint
                or current.path != str(path.resolve())
                or current.path in colliding_paths
                or disk_fingerprint != current.content_fingerprint
            )
            kept_keys = {artifact_key, *(row.artifact_key for row in composed)}
            stale_keys = {row.artifact_key for row in prior} - kept_keys
            if not changed:
                if stale_keys:
                    replacements.append(
                        ArtifactReplacement(
                            ArtifactKind.DOSSIER.value,
                            (current, *composed),
                            parent_id=plan.parent_id,
                        )
                    )
                continue

            body = parent_rendering.render_singleton(plan) if singleton else parent_rendering.render_parent(plan)
            data = body.encode()
            fingerprint = hashlib.sha256(data).hexdigest()
            artifact = ArtifactRow(
                artifact_key,
                ArtifactKind.DOSSIER.value,
                plan.parent_id,
                str(path.resolve()),
                fingerprint,
                ProjectionStatus.PROJECTED.value,
                input_fingerprint=input_fingerprint,
                payload_json=json.dumps(
                    {
                        "parent_id": plan.parent_id,
                        "name": plan.name,
                        "slug": plan.slug,
                        "path": f"parents/{file_slug}.md",
                        "needs_review": [],
                        "children": [child.slug for child in plan.confirmed],
                        "emails": list(plan.emails),
                        "phones": list(plan.phones),
                        "headline": headline(plan.merged),
                        "full_name": plan.name,
                        "source_channels": list(
                            dict.fromkeys(source for child in plan.confirmed for source in child.channels)
                        ),
                        "body": body,
                        **({"singleton": True} if singleton else {}),
                    },
                    separators=(",", ":"),
                ),
                projected_at=now_iso(),
            )
            path.write_bytes(data)
            parents_changed += 1
            singleton_written += int(singleton)
            replacements.append(
                ArtifactReplacement(
                    ArtifactKind.DOSSIER.value,
                    (artifact, *composed),
                    parent_id=plan.parent_id,
                )
            )

        if replacements:
            self.db.project_rows(tuple(replacements))
        active_slugs = {slugify(plan.name, plan.parent_id) for plan in plans}
        orphans = parent_rendering.remove_orphans(self.parents_dir, active_slugs)
        return BuildParentsManifest(
            status="completed",
            merge_components=len(components),
            parents_changed=parents_changed,
            parents_merged=parents_merged,
            singletons_written=singleton_written,
            owner_excluded=owner_excluded,
            orphans_removed=orphans,
            parents_dir=str(self.parents_dir),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply accepted parent merges and refresh changed canonical dossiers.")
    parser.add_argument("--parents-dir", default=str(PARENTS_DIR))
    parser.add_argument("--db", default=str(CANONICAL_DB))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = BuildParents(db=open_existing_db(args.db), parents_dir=Path(args.parents_dir)).run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

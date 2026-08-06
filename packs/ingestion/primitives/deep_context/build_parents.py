#!/usr/bin/env python3
"""Apply accepted parent merges and refresh only changed parent dossiers."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import defaultdict
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
    CanonicalSnapshot,
    IdentifierKind,
    ProjectionStatus,
)
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.dossier.facts import headline, merge_facts
from packs.ingestion.primitives.deep_context.merge_candidates.blocking import connected_components
from packs.ingestion.primitives.deep_context.parents.assignment import load_assignment
from packs.ingestion.primitives.deep_context.parents.models import ChildEntry, ParentPlan
from packs.ingestion.primitives.deep_context.parents.rendering import (
    remove_orphans,
    render_parent,
    render_singleton,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest


class BuildParentsManifest(StageManifest):
    source: str = "build_parents"
    clusters: int = 0
    parents_written: int = 0
    merged_parents: int = 0
    singleton_parents: int = 0
    owner_excluded: int = 0
    orphans_removed: int = 0
    parents_dir: str = ""
    elapsed_ms: int = 0


def _accepted_components(snapshot: CanonicalSnapshot) -> tuple[tuple[str, ...], ...]:
    """Current parent components linked by their latest accepted verdict."""
    parent_by_person = {row.person_id: row.parent_id for row in snapshot.people}
    latest = {}
    for row in snapshot.merge_verdicts:
        left = parent_by_person.get(row.person_a)
        right = parent_by_person.get(row.person_b)
        if not left or not right or left == right:
            continue
        key = tuple(sorted((left, right)))
        prior = latest.get(key)
        rank = (row.updated_at or "", row.person_a, row.person_b)
        if prior is None or rank >= (prior.updated_at or "", prior.person_a, prior.person_b):
            latest[key] = row

    edges = [key for key, row in latest.items() if row.accepted]
    nodes = sorted({parent_id for edge in edges for parent_id in edge})
    return tuple(tuple(sorted(group)) for group in connected_components(nodes, edges))


def _parent_plans(snapshot: CanonicalSnapshot) -> tuple[tuple[ParentPlan, ...], int]:
    identifiers: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for row in snapshot.identifiers:
        value = row.display_value or row.normalized_value
        values = identifiers[row.person_id][row.kind]
        if value not in values:
            values.append(value)
    sources: dict[str, list[str]] = defaultdict(list)
    for row in snapshot.sources:
        if row.source not in sources[row.person_id]:
            sources[row.person_id].append(row.source)
    people_by_parent: dict[str, list] = defaultdict(list)
    for row in snapshot.people:
        people_by_parent[row.parent_id].append(row)
    facts_by_parent: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in snapshot.facts:
        facts = parse_json_object(row.facts_json)
        if facts:
            facts_by_parent[row.parent_id].append({"facts": facts})
    dossier_by_person = {
        row.person_id: row for row in snapshot.dossiers if row.person_id is not None
    }
    dossier_by_parent = {
        row.parent_id: row for row in snapshot.dossiers if row.person_id is None
    }

    plans = []
    owner_excluded = 0
    for parent in snapshot.parents:
        members = sorted(
            people_by_parent.get(parent.parent_id, ()),
            key=lambda row: (row.child_slug or row.person_id, row.person_id),
        )
        visible = [row for row in members if not row.is_owner]
        owner_excluded += len(members) - len(visible)
        if not visible:
            continue
        merged = merge_facts(facts_by_parent.get(parent.parent_id, []))
        prior_dossier = dossier_by_parent.get(parent.parent_id)
        prior_headline = (
            prior_dossier.headline
            if prior_dossier
            else next(
                (
                    dossier_by_person[row.person_id].headline
                    for row in visible
                    if row.person_id in dossier_by_person
                    and dossier_by_person[row.person_id].headline
                ),
                "",
            )
        )
        if not merged and prior_headline:
            merged = {"headline": prior_headline}
        name = str(merged.get("canonical_name") or parent.display_name or visible[0].display_name or "person")
        slug = parent.display_slug or slugify(name, parent.parent_id)
        emails = []
        phones = []
        children = []
        for person in visible:
            child_dossier = dossier_by_person.get(person.person_id)
            child_name = str(
                (child_dossier.name if child_dossier else "")
                or person.display_name
                or person.child_slug
                or person.person_id
            )
            child_slug = person.child_slug or person.person_id
            children.append(ChildEntry(
                child_slug,
                child_name,
                0.0,
                "",
                tuple(sources.get(person.person_id, ())),
                person.person_id,
            ))
            for value in identifiers.get(person.person_id, {}).get(IdentifierKind.EMAIL.value, ()):
                if value not in emails:
                    emails.append(value)
            for value in identifiers.get(person.person_id, {}).get(IdentifierKind.PHONE.value, ()):
                if value not in phones:
                    phones.append(value)
        plans.append(ParentPlan(
            parent.parent_id,
            slug,
            name,
            tuple(emails),
            tuple(phones),
            tuple(children),
            merged,
        ))
    return tuple(plans), owner_excluded


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
        initial = canonical_snapshot(self.db)
        components = _accepted_components(initial)
        assignment = load_assignment(initial)
        merged_parents = 0
        for component in components:
            survivor = assignment.elect(list(component))
            for absorbed in component:
                if absorbed == survivor:
                    continue
                self.db.merge_parents(survivor, absorbed)
                merged_parents += 1

        snapshot = canonical_snapshot(self.db)
        plans, owner_excluded = _parent_plans(snapshot)
        prior_artifacts: dict[str, list[ArtifactRow]] = defaultdict(list)
        for row in snapshot.artifacts:
            if (
                row.kind == ArtifactKind.DOSSIER.value
                and row.person_id is None
                and row.candidate_key is None
            ):
                prior_artifacts[row.parent_id].append(row)

        self.parents_dir.mkdir(parents=True, exist_ok=True)
        replacements = []
        parents_written = 0
        singleton_written = 0
        for plan in plans:
            singleton = len(plan.confirmed) == 1
            body = render_singleton(plan) if singleton else render_parent(plan)
            data = body.encode()
            fingerprint = hashlib.sha256(data).hexdigest()
            path = self.parents_dir / f"{plan.slug}.md"
            artifact = ArtifactRow(
                f"dossier:{plan.parent_id}",
                ArtifactKind.DOSSIER.value,
                plan.parent_id,
                str(path.resolve()),
                fingerprint,
                ProjectionStatus.PROJECTED.value,
                payload_json=json.dumps({
                    "parent_id": plan.parent_id,
                    "name": plan.name,
                    "slug": plan.slug,
                    "path": f"parents/{plan.slug}.md",
                    "needs_review": [],
                    "children": [child.slug for child in plan.confirmed],
                    "person_ids": [child.person_id for child in plan.confirmed],
                    "emails": list(plan.emails),
                    "phones": list(plan.phones),
                    "headline": headline(plan.merged) or str(plan.merged.get("headline") or ""),
                    "full_name": plan.name,
                    "source_channels": list(dict.fromkeys(
                        source for child in plan.confirmed for source in child.channels
                    )),
                    "body": body,
                    **({"singleton": True} if singleton else {}),
                }, separators=(",", ":")),
                projected_at=now_iso(),
            )
            prior = prior_artifacts.get(plan.parent_id, [])
            current = next((row for row in prior if row.artifact_key == artifact.artifact_key), None)
            changed = (
                current is None
                or current.content_fingerprint != fingerprint
                or current.path != artifact.path
                or not path.is_file()
            )
            stale_keys = {row.artifact_key for row in prior} - {artifact.artifact_key}
            if changed:
                path.write_bytes(data)
                parents_written += 1
                singleton_written += int(singleton)
            if changed or stale_keys:
                replacements.append(ArtifactReplacement(
                    ArtifactKind.DOSSIER.value,
                    (artifact,),
                    parent_id=plan.parent_id,
                ))

        if replacements:
            self.db.project_rows(tuple(replacements))
        active_slugs = {plan.slug for plan in plans}
        orphans = remove_orphans(self.parents_dir, active_slugs)
        return BuildParentsManifest(
            status="completed",
            clusters=len(components),
            parents_written=parents_written,
            merged_parents=merged_parents,
            singleton_parents=singleton_written,
            owner_excluded=owner_excluded,
            orphans_removed=orphans,
            parents_dir=str(self.parents_dir),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply accepted parent merges and refresh changed canonical dossiers."
    )
    parser.add_argument("--parents-dir", default=str(PARENTS_DIR))
    parser.add_argument("--db", default=str(CANONICAL_DB))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = BuildParents(db=Db(Path(args.db)), parents_dir=Path(args.parents_dir)).run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

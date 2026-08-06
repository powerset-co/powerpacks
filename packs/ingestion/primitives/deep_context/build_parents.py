#!/usr/bin/env python3
"""Build parent dossier files and their canonical SQLite graph."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    DEFAULT_PEOPLE_CSV,
    DOSSIER_DIR,
    emit,
    FACTS_DIR,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    MERGE_CSV,
    PARENT_TEMPLATE,
    PARENTS_DIR,
    PARENTS_MANIFEST,
    RAW_DIR,
)
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    ProjectionStatus,
)
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.parents.graph import (
    clusters_from_pairs,
    parent_id_for,
    plan_parents,
    singleton_plan,
)
from packs.ingestion.primitives.deep_context.parents.projection import CanonicalGraphBuilder
from packs.ingestion.primitives.deep_context.parents.rendering import (
    remove_orphans,
    render_parent,
    render_singleton,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest

def _payload(value: str | None) -> dict:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


class BuildParentsManifest(StageManifest):
    source: str = "build_parents"
    clusters: int = 0
    parents_written: int = 0
    merged_parents: int = 0
    singleton_parents: int = 0
    owner_excluded: int = 0
    owner_aliases_added: list[str] = []
    orphans_removed: int = 0
    parent_slug_keys_migrated: int = 0
    parent_slug_directories_renamed: int = 0
    parent_slug_directory_conflicts: int = 0
    parent_slug_csv_rows_rewritten: int = 0
    parent_slug_jsonl_rows_rewritten: int = 0
    worth_parent_rows: int = 0
    worth_human_migrated: int = 0
    worth_legacy_marks_cleared: int = 0
    worth_stale_parent_rows_removed: int = 0
    parents_dir: str = ""
    elapsed_ms: int = 0


class BuildParents(Node):
    """Build canonical parent files and their SQLite membership projection."""

    name = "deep_parents"
    inputs = ()
    outputs = (
        Artifact(path=PARENT_TEMPLATE, writes="full_rewrite", required=False),
    )
    payload = BuildParentsManifest
    manifest = str(PARENTS_MANIFEST)

    def __init__(
        self,
        *,
        db: Db,
        parents_dir: Path | None = None,
    ) -> None:
        self.db = db
        self.parents_dir = Path(parents_dir or PARENTS_DIR)

    def bindings(self) -> dict[str, str]:
        return {
            PARENT_TEMPLATE: str(self.parents_dir / "{slug}.md"),
            self.manifest: str(self.parents_dir / "manifest.json"),
        }

    def execute(self) -> BuildParentsManifest:
        started = time.monotonic()
        snapshot = canonical_snapshot(self.db)
        slugs_info = {
            row.slug: {
                "person_id": row.person_id,
                "name": row.name,
                "headline": row.headline,
                "full_name": row.full_name,
                "emails": list(row.emails),
                "phones": list(row.phones),
                "source_channels": list(row.source_channels),
            }
            for row in snapshot.dossiers if row.person_id
        }
        slug_by_person = {info["person_id"]: slug for slug, info in slugs_info.items()}
        pairs = [
            {
                "slug_a": slug_by_person[row.person_a],
                "slug_b": slug_by_person[row.person_b],
                "confidence": str(row.confidence),
                "reason": row.reason,
            }
            for row in snapshot.merge_verdicts
            if row.accepted
            and row.person_a in slug_by_person and row.person_b in slug_by_person
        ]
        clusters = clusters_from_pairs(pairs)
        facts_by_person = {
            row.person_id: _payload(row.facts_json)
            for row in snapshot.facts
            if row.person_id
        }
        owner_ids = {row.person_id for row in snapshot.facts if row.is_owner and row.person_id}
        owner_slugs = {
            slug for slug, info in slugs_info.items() if info.get("person_id") in owner_ids
        }
        plans = plan_parents(
            clusters, pairs, slugs_info, owner_slugs, facts_by_person,
        )

        self.parents_dir.mkdir(parents=True, exist_ok=True)
        projector = CanonicalGraphBuilder(self.db, snapshot, slugs_info)
        parent_payloads: dict[str, dict[str, object]] = {}
        clustered_slugs = {child.slug for plan in plans for child in plan.confirmed}
        singleton_plans = []
        owner_excluded = 0
        for child_slug, info in slugs_info.items():
            if child_slug in clustered_slugs:
                continue
            if child_slug in owner_slugs:
                owner_excluded += 1
                continue
            singleton_plans.append(singleton_plan(child_slug, info))

        for plan in (*plans, *singleton_plans):
            singleton = len(plan.confirmed) == 1
            projector.add_parent(plan.parent_id, plan.name, plan.slug)
            for child in plan.confirmed:
                projector.add_member(child.slug, plan.parent_id, plan.slug)
            body = render_singleton(plan) if singleton else render_parent(plan)
            (self.parents_dir / f"{plan.slug}.md").write_text(body, encoding="utf-8")
            payload: dict[str, object] = {
                "parent_id": plan.parent_id, "name": plan.name, "slug": plan.slug,
                "path": f"parents/{plan.slug}.md", "needs_review": [],
                "children": [child.slug for child in plan.confirmed],
                "emails": list(plan.emails), "phones": list(plan.phones),
                "headline": str(plan.merged.get("headline") or ""),
                "full_name": plan.name,
                "source_channels": list(dict.fromkeys(
                    source for child in plan.confirmed for source in child.channels
                )),
                "body": body,
            }
            if singleton:
                payload["singleton"] = True
            parent_payloads[plan.parent_id] = payload

        for child_slug in sorted(owner_slugs):
            info = slugs_info[child_slug]
            parent_id = parent_id_for([info["person_id"]])
            name = info.get("name", child_slug)
            owner_plan = singleton_plan(child_slug, info)
            projector.add_parent(parent_id, name, owner_plan.slug)
            projector.add_member(child_slug, parent_id, owner_plan.slug, is_owner=True)

        orphans = remove_orphans(self.parents_dir, {plan.slug for plan in (*plans, *singleton_plans)})
        projection = projector.apply()
        parent_artifacts = []
        for parent_id, payload in sorted(parent_payloads.items()):
            path = self.parents_dir / f"{payload['slug']}.md"
            data = str(payload["body"]).encode()
            parent_artifacts.append(ArtifactRow(
                f"dossier:{parent_id}", ArtifactKind.DOSSIER.value, parent_id,
                str(path.resolve()), hashlib.sha256(data).hexdigest(),
                ProjectionStatus.PROJECTED.value,
                payload_json=json.dumps(payload, separators=(",", ":")), projected_at=now_iso(),
            ))
        self.db.project_rows(tuple(parent_artifacts))
        return BuildParentsManifest(
            status="completed", clusters=len(clusters),
            parents_written=len(plans) + len(singleton_plans),
            merged_parents=len(plans), singleton_parents=len(singleton_plans),
            owner_excluded=owner_excluded,
            orphans_removed=orphans,
            worth_parent_rows=projection.parent_rows,
            worth_human_migrated=projection.human_migrated,
            worth_stale_parent_rows_removed=projection.stale_parent_rows_removed,
            parents_dir=str(self.parents_dir),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build parent canonical dossiers from merge clusters.")
    parser.add_argument("--merge-csv", default=str(MERGE_CSV))
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV),
                        help="Merged people.csv; superseded_person_ids rows fold pre-match identities")
    parser.add_argument("--index-json", default=str(INDEX_JSON))
    parser.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    parser.add_argument("--facts-dir", default=str(FACTS_DIR))
    parser.add_argument("--raw-dir", default=str(RAW_DIR))
    parser.add_argument("--parents-dir", default=str(PARENTS_DIR))
    parser.add_argument("--db", default=str(CANONICAL_DB), help="Canonical Deep Context SQLite database")
    parser.add_argument("--review-csv", default=str(LINKEDIN_OVERRIDES_CSV))
    parser.add_argument("--confirm-threshold", type=float, default=0.85,
                        help="Min judge confidence to merge a child into the parent (else listed as needs-review)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = BuildParents(db=Db(Path(args.db)), parents_dir=Path(args.parents_dir)).run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

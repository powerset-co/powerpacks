#!/usr/bin/env python3
"""Build the complete canonical-parent layer from child dossiers and merge edges.

The stable Node and CLI orchestrate typed graph planning, byte-stable parent
rendering, legacy slug-artifact migration, index replacement, and one canonical
SQLite graph projection. Concrete policy lives in ``deep_context.parents``.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from packs.ingestion.primitives.deep_context.common import (
    DEEP_RESEARCH_DIR,
    DEFAULT_PEOPLE_CSV,
    DOSSIER_DIR,
    DOSSIER_TEMPLATE,
    emit,
    FACTS_DIR,
    FACTS_TEMPLATE,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    load_index,
    MERGE_CSV,
    OWNER_JSON,
    PARENT_TEMPLATE,
    PARENTS_DIR,
    PARENTS_MANIFEST,
    RAW_BUNDLE_TEMPLATE,
    RAW_DIR,
    RECONCILE_DIR,
    ROOT,
    VERDICTS_CSV,
    VERDICTS_JSONL,
    write_index,
)
from packs.ingestion.primitives.common.legacy import (
    migrate_parent_slug_artifacts,
    parent_slug_migrations,
)
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.parents.graph import (
    clusters_from_pairs,
    fold_owner_aliases,
    is_owner,
    load_pairs,
    parent_id_for,
    plan_parents,
    singleton_plan,
    superseded_pairs,
)
from packs.ingestion.primitives.deep_context.parents.projection import CanonicalGraphBuilder
from packs.ingestion.primitives.deep_context.parents.rendering import (
    inject_parent_backref,
    remove_orphans,
    render_parent,
    render_singleton,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest

SYNTHETIC_PEOPLE_CSV = LINKEDIN_OVERRIDES_CSV.parent / "synthetic-people.csv"
CANONICAL_DB = ROOT / "deep-context.sqlite"


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
    """Build canonical parent files, index membership, and SQLite projection."""

    name = "deep_parents"
    inputs = (
        Artifact(path=str(MERGE_CSV), required=False),
        Artifact(path=str(DEFAULT_PEOPLE_CSV), required=False),
        Artifact(path=str(INDEX_JSON), required=False),
        Artifact(path=FACTS_TEMPLATE, required=False),
        Artifact(path=RAW_BUNDLE_TEMPLATE, required=False),
        Artifact(path=DOSSIER_TEMPLATE, required=False),
        Artifact(path=str(OWNER_JSON), required=False),
    )
    outputs = (
        Artifact(path=PARENT_TEMPLATE, writes="full_rewrite", required=False),
        Artifact(
            path=str(INDEX_JSON), writes="upsert", owns_columns=("parents",), feedback=True,
        ),
    )
    payload = BuildParentsManifest
    manifest = str(PARENTS_MANIFEST)

    def __init__(
        self,
        *,
        db: Db,
        merge_csv: Path | None = None,
        people_csv: Path | None = None,
        index_json: Path | None = None,
        dossier_dir: Path | None = None,
        facts_dir: Path | None = None,
        raw_dir: Path | None = None,
        parents_dir: Path | None = None,
        review_csv: Path | str | None = LINKEDIN_OVERRIDES_CSV,
        confirm_threshold: float = 0.85,
    ) -> None:
        self.db = db
        self.merge_csv = Path(merge_csv or MERGE_CSV)
        self.people_csv = Path(people_csv or DEFAULT_PEOPLE_CSV)
        self.index_json = Path(index_json or INDEX_JSON)
        self.dossier_dir = Path(dossier_dir or DOSSIER_DIR)
        self.facts_dir = Path(facts_dir or FACTS_DIR)
        self.raw_dir = Path(raw_dir or RAW_DIR)
        self.parents_dir = Path(parents_dir or PARENTS_DIR)

    def bindings(self) -> dict[str, str]:
        return {
            str(MERGE_CSV): str(self.merge_csv),
            str(DEFAULT_PEOPLE_CSV): str(self.people_csv),
            str(INDEX_JSON): str(self.index_json),
            FACTS_TEMPLATE: str(self.facts_dir / "{person_id}.jsonl"),
            RAW_BUNDLE_TEMPLATE: str(self.raw_dir / "{person_id}.json"),
            DOSSIER_TEMPLATE: str(self.dossier_dir / "{slug}.md"),
            PARENT_TEMPLATE: str(self.parents_dir / "{slug}.md"),
            self.manifest: str(self.parents_dir / "manifest.json"),
        }

    def execute(self) -> BuildParentsManifest:
        started = time.monotonic()
        snapshot = canonical_snapshot(self.db)
        index = load_index(self.index_json)
        old_parents = dict(index.get("parents") or {})
        slugs_info = index.get("slugs") or {}
        pairs = load_pairs(self.merge_csv) + superseded_pairs(self.people_csv, slugs_info)
        clusters = clusters_from_pairs(pairs)
        owner_slugs = {
            slug for slug, info in slugs_info.items()
            if is_owner(info.get("person_id", ""), self.facts_dir)
        }
        owner_aliases = (
            fold_owner_aliases(owner_slugs, slugs_info, self.raw_dir) if owner_slugs else []
        )
        plans = plan_parents(
            clusters, pairs, index, owner_slugs, self.facts_dir, self.raw_dir,
        )

        self.parents_dir.mkdir(parents=True, exist_ok=True)
        index["parents"] = {}
        projector = CanonicalGraphBuilder(self.db, snapshot, slugs_info, self.raw_dir)
        written_slugs: set[str] = set()
        clustered_slugs: set[str] = set()
        for plan in plans:
            projector.add_parent(plan.parent_id, plan.name, plan.slug)
            for child in plan.confirmed:
                projector.add_member(child.slug, plan.parent_id, plan.slug)
            (self.parents_dir / f"{plan.slug}.md").write_text(
                render_parent(plan), encoding="utf-8",
            )
            written_slugs.add(plan.slug)
            for child in plan.confirmed:
                inject_parent_backref(self.dossier_dir, child.slug, plan.slug, plan.name)
                clustered_slugs.add(child.slug)
            index["parents"][plan.slug] = {
                "parent_id": plan.parent_id, "name": plan.name,
                "path": f"parents/{plan.slug}.md",
                "children": [child.slug for child in plan.confirmed],
                "needs_review": [],
            }

        singletons = 0
        owner_excluded = 0
        for child_slug, info in slugs_info.items():
            if child_slug in clustered_slugs:
                continue
            if child_slug in owner_slugs:
                owner_excluded += 1
                continue
            plan = singleton_plan(child_slug, info, index)
            projector.add_parent(plan.parent_id, plan.name, plan.slug)
            projector.add_member(child_slug, plan.parent_id, plan.slug)
            (self.parents_dir / f"{plan.slug}.md").write_text(
                render_singleton(plan), encoding="utf-8",
            )
            singletons += 1
            written_slugs.add(plan.slug)
            inject_parent_backref(self.dossier_dir, child_slug, plan.slug, plan.name)
            index["parents"][plan.slug] = {
                "parent_id": plan.parent_id, "name": plan.name,
                "path": f"parents/{plan.slug}.md", "children": [child_slug],
                "needs_review": [], "singleton": True,
            }

        for child_slug in sorted(owner_slugs):
            info = slugs_info[child_slug]
            parent_id = parent_id_for([info["person_id"]])
            name = info.get("name", child_slug)
            owner_plan = singleton_plan(child_slug, info, index)
            projector.add_parent(parent_id, name, owner_plan.slug)
            projector.add_member(child_slug, parent_id, owner_plan.slug, is_owner=True)

        orphans = remove_orphans(self.parents_dir, written_slugs)
        slug_migration = migrate_parent_slug_artifacts(
            parent_slug_migrations(old_parents, index["parents"]),
            deep_research_dir=DEEP_RESEARCH_DIR,
            verdicts_jsonl=VERDICTS_JSONL,
            verdicts_csv=VERDICTS_CSV,
            applied_csv=RECONCILE_DIR / "applied.csv",
            synthetic_people_csv=SYNTHETIC_PEOPLE_CSV,
        )
        write_index(self.index_json, index)
        projection = projector.apply()
        written = len(plans) + singletons
        return BuildParentsManifest(
            status="completed", clusters=len(clusters), parents_written=written,
            merged_parents=len(plans), singleton_parents=singletons,
            owner_excluded=owner_excluded, owner_aliases_added=owner_aliases,
            orphans_removed=orphans,
            parent_slug_keys_migrated=slug_migration["keys"],
            parent_slug_directories_renamed=slug_migration["directories_renamed"],
            parent_slug_directory_conflicts=slug_migration["directory_conflicts"],
            parent_slug_csv_rows_rewritten=slug_migration["csv_rows_rewritten"],
            parent_slug_jsonl_rows_rewritten=slug_migration["jsonl_rows_rewritten"],
            worth_parent_rows=projection.parent_rows,
            worth_human_migrated=projection.human_migrated,
            worth_legacy_marks_cleared=0,
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
    payload = BuildParents(
        db=Db(Path(args.db)), merge_csv=Path(args.merge_csv), people_csv=Path(args.people_csv),
        index_json=Path(args.index_json), dossier_dir=Path(args.dossier_dir),
        facts_dir=Path(args.facts_dir), raw_dir=Path(args.raw_dir),
        parents_dir=Path(args.parents_dir), review_csv=args.review_csv,
        confirm_threshold=args.confirm_threshold,
    ).run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Render projected facts into dossier files, projections, and a catalog."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    DOSSIER_DIR,
    DOSSIER_TEMPLATE,
    DOSSIERS_MANIFEST,
    INDEX_MD,
    emit,
    slugify,
)
from packs.ingestion.primitives.deep_context.collection.state import projected_bundles
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactReplacement,
    ArtifactRow,
    IdentifierKind,
    ProjectionStatus,
)
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.dossier.facts import headline
from packs.ingestion.primitives.deep_context.dossier.rendering import render_dossier, write_catalog
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest


def _payload(value: str | None) -> dict:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


class ComposeDossierManifest(StageManifest):
    source: str = "compose_dossier"
    dossiers_written: int = 0
    orphans_removed: int = 0
    dossier_dir: str = ""
    index_md: str = ""
    elapsed_ms: int = 0


class ComposeDossier(Node):
    """Write dossier artifacts and project the complete downstream payload."""

    name = "deep_compose"
    inputs = ()
    outputs = (Artifact(path=DOSSIER_TEMPLATE, required=False),)
    payload = ComposeDossierManifest
    manifest = str(DOSSIERS_MANIFEST)

    def __init__(
        self,
        *,
        db: Db | None = None,
        dossier_dir: Path | None = None,
        index_md: Path | None = None,
        person: str = "",
    ) -> None:
        self.db = db or Db(CANONICAL_DB)
        self.dossier_dir = Path(dossier_dir or DOSSIER_DIR)
        self.index_md = Path(index_md or INDEX_MD)
        self.person = person

    def bindings(self) -> dict[str, str]:
        return {
            DOSSIER_TEMPLATE: str(self.dossier_dir / "{slug}.md"),
            self.manifest: str(self.dossier_dir / "manifest.json"),
        }

    def execute(self) -> ComposeDossierManifest:
        started = time.monotonic()
        self.dossier_dir.mkdir(parents=True, exist_ok=True)
        snapshot = canonical_snapshot(self.db)
        parents = {row.parent_id: row for row in snapshot.parents}
        people_by_parent: dict[str, list] = {}
        for row in snapshot.people:
            people_by_parent.setdefault(row.parent_id, []).append(row)
        owner_ids = {row.person_id for row in snapshot.people if row.is_owner}
        owner_emails = tuple(
            row.display_value or row.normalized_value
            for row in snapshot.identifiers
            if row.person_id in owner_ids and row.kind == IdentifierKind.EMAIL.value
        )
        owner_phones = tuple(
            row.display_value or row.normalized_value
            for row in snapshot.identifiers
            if row.person_id in owner_ids and row.kind == IdentifierKind.PHONE.value
        )
        projection_rows: list[object] = []
        dossier_artifacts: list[ArtifactRow] = []
        written_slugs: set[str] = set()

        facts = {row.parent_id: row for row in snapshot.facts if row.person_id is None}
        artifacts = {
            (row.kind, row.parent_id): row
            for row in snapshot.artifacts
            if row.person_id is None and row.status == ProjectionStatus.PROJECTED.value
        }
        bundles = projected_bundles(snapshot)
        selected_parent = next(
            (row.parent_id for row in snapshot.people if row.person_id == self.person),
            self.person,
        )
        for parent_id, fact in sorted(facts.items()):
            if selected_parent and parent_id != selected_parent:
                continue
            meta = bundles.get(parent_id)
            if meta is None:
                continue
            prior = parents.get(parent_id)
            if prior is None:
                raise StoreError(f"dossier parent is absent from canonical graph: {parent_id}")
            merged = _payload(fact.facts_json)
            if not merged:
                continue
            facts_artifact = artifacts.get((ArtifactKind.FACTS.value, parent_id))
            depth = _payload(facts_artifact.payload_json if facts_artifact else None)
            meta.setdefault("person_id", parent_id)
            name = merged.get("canonical_name") or prior.display_name or meta.get("full_name") or "person"
            slug = prior.display_slug or slugify(name, parent_id)
            dossier_path = self.dossier_dir / f"{slug}.md"
            body = render_dossier(meta, merged, depth, owner_emails=owner_emails, owner_phones=owner_phones)
            dossier_path.write_text(body, encoding="utf-8")
            written_slugs.add(slug)
            members = people_by_parent.get(parent_id, [])
            record = dict(
                parent_id=parent_id,
                person_ids=[row.person_id for row in members],
                children=[row.child_slug for row in members if row.child_slug],
                name=name, path=f"dossiers/{slug}.md",
                headline=headline(merged), full_name=str(meta.get("full_name") or ""),
                emails=list(meta.get("emails") or []), phones=list(meta.get("phones") or []),
                source_channels=list(meta.get("source_channels") or []), body=body,
            )
            artifact = ArtifactRow(
                f"dossier:{parent_id}", ArtifactKind.DOSSIER.value,
                parent_id, str(dossier_path.resolve()), hashlib.sha256(body.encode()).hexdigest(),
                ProjectionStatus.PROJECTED.value,
                payload_json=json.dumps(record, separators=(",", ":")), projected_at=now_iso(),
            )
            dossier_artifacts.append(artifact)
            projection_rows.append(ArtifactReplacement(
                ArtifactKind.DOSSIER.value, (artifact,), parent_id=parent_id,
            ))

        orphans = 0
        if not selected_parent:
            for path in self.dossier_dir.glob("*.md"):
                if path.stem not in written_slugs:
                    path.unlink()
                    orphans += 1
            written_parents = {row.parent_id for row in dossier_artifacts}
            for artifact in snapshot.artifacts:
                if artifact.kind != ArtifactKind.DOSSIER.value:
                    continue
                if artifact.person_id:
                    projection_rows.append(ArtifactReplacement(
                        ArtifactKind.DOSSIER.value, (), person_id=artifact.person_id,
                    ))
                elif artifact.candidate_key is None and artifact.parent_id not in written_parents:
                    projection_rows.append(ArtifactReplacement(
                        ArtifactKind.DOSSIER.value, (), parent_id=artifact.parent_id,
                    ))
        self.db.project_rows(tuple(projection_rows))
        catalog = [(row.name or row.slug, row.headline, row.slug)
                   for row in canonical_snapshot(self.db).dossiers if row.person_id is None]
        write_catalog(self.index_md, catalog)
        return ComposeDossierManifest(
            status="completed", dossiers_written=len(dossier_artifacts),
            orphans_removed=orphans, dossier_dir=str(self.dossier_dir),
            index_md=str(self.index_md),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compose markdown dossiers + lookup index from synthesized facts.",
    )
    parser.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    parser.add_argument("--index-md", default=str(INDEX_MD))
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--person", default="", help="Only this person id")
    args = parser.parse_args(argv)
    result = ComposeDossier(
        db=Db(Path(args.db)),
        dossier_dir=Path(args.dossier_dir),
        index_md=Path(args.index_md), person=args.person,
    ).run()
    emit(result.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

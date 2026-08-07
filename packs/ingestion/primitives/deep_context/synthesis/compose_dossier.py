"""Render projected facts into dossier files, projections, and a catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso, parse_json_object
from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    DOSSIER_DIR,
    DOSSIER_TEMPLATE,
    DOSSIERS_MANIFEST,
    INDEX_MD,
    emit,
    slugify,
)
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.collection.planning import projected_bundles
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactReplacement,
    ArtifactRow,
    FactRow,
    IdentifierKind,
    PARENT_DOSSIER_ARTIFACT_PREFIX,
    ParentRow,
    PersonRow,
    ProjectionStatus,
)
from packs.ingestion.primitives.deep_context.db.queries import (
    artifacts as artifact_rows,
    facts as fact_rows,
    identifiers as identifier_rows,
    parents as parent_rows,
    people as person_rows,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError, open_existing_db
from packs.ingestion.primitives.deep_context.synthesis.facts import headline
from packs.ingestion.primitives.deep_context.synthesis.models import (
    DossierDepth,
    SynthesizedFacts,
)
from packs.ingestion.primitives.deep_context.synthesis.rendering import render_dossier, write_catalog
from packs.ingestion.primitives.deep_context.manifests.compose_dossier_manifest import (
    ComposeDossierManifest,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node


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
        db: Db,
        dossier_dir: Path | None = None,
        index_md: Path | None = None,
    ) -> None:
        self.db = db
        self.dossier_dir = Path(dossier_dir or DOSSIER_DIR)
        self.index_md = Path(index_md or INDEX_MD)

    def bindings(self) -> dict[str, str]:
        return {
            DOSSIER_TEMPLATE: str(self.dossier_dir / "{slug}.md"),
            self.manifest: str(self.dossier_dir / "manifest.json"),
        }

    def execute(self) -> ComposeDossierManifest:
        started = time.monotonic()
        self.dossier_dir.mkdir(parents=True, exist_ok=True)
        parents: dict[str, ParentRow] = {row.parent_id: row for row in parent_rows(self.db)}
        people_rows = person_rows(self.db)
        people_by_parent: dict[str, list[PersonRow]] = {}
        for row in people_rows:
            people_by_parent.setdefault(row.parent_id, []).append(row)
        owner_ids = {row.person_id for row in people_rows if row.is_owner}
        all_identifiers = identifier_rows(self.db)
        owner_emails = tuple(
            row.display_value or row.normalized_value
            for row in all_identifiers
            if row.person_id in owner_ids and row.kind == IdentifierKind.EMAIL.value
        )
        owner_phones = tuple(
            row.display_value or row.normalized_value
            for row in all_identifiers
            if row.person_id in owner_ids and row.kind == IdentifierKind.PHONE.value
        )
        projection_rows: list[ArtifactReplacement] = []
        dossier_artifacts: list[ArtifactRow] = []
        written_slugs: set[str] = set()

        facts: dict[str, FactRow] = {row.parent_id: row for row in fact_rows(self.db, parent_owned=True)}
        facts_artifacts = artifact_rows(
            self.db,
            kind=ArtifactKind.FACTS.value,
            status=ProjectionStatus.PROJECTED.value,
            parent_owned=True,
        )
        facts_artifacts_by_parent = {(row.kind, row.parent_id): row for row in facts_artifacts}
        dossier_rows = artifact_rows(self.db, kind=ArtifactKind.DOSSIER.value)
        parent_dossiers = {
            row.parent_id: row
            for row in dossier_rows
            if row.artifact_key == f"{PARENT_DOSSIER_ARTIFACT_PREFIX}{row.parent_id}"
        }
        bundles = projected_bundles(self.db)
        for parent_id, fact in sorted(facts.items()):
            meta: CollectionBundle | None = bundles.get(parent_id)
            if meta is None:
                continue
            prior: ParentRow | None = parents.get(parent_id)
            if prior is None:
                raise StoreError(f"dossier parent is absent from canonical graph: {parent_id}")
            merged: SynthesizedFacts | None = SynthesizedFacts.from_payload(parse_json_object(fact.facts_json))
            if merged is None:
                continue
            facts_artifact: ArtifactRow | None = facts_artifacts_by_parent.get((ArtifactKind.FACTS.value, parent_id))
            depth: DossierDepth | None = DossierDepth.from_payload(
                parse_json_object(facts_artifact.payload_json if facts_artifact else None)
            )
            name = merged.canonical_name or prior.display_name or meta.full_name or "person"
            slug = prior.display_slug or slugify(name, parent_id)
            dossier_path = self.dossier_dir / f"{slug}.md"
            body = render_dossier(
                meta,
                merged,
                depth,
                owner_emails=owner_emails,
                owner_phones=owner_phones,
            )
            dossier_path.write_text(body, encoding="utf-8")
            written_slugs.add(slug)
            members = people_by_parent.get(parent_id, [])
            record = dict(
                parent_id=parent_id,
                children=[row.child_slug for row in members if row.child_slug],
                name=name,
                path=f"dossiers/{slug}.md",
                headline=headline(merged),
                full_name=meta.full_name,
                emails=list(meta.emails),
                phones=list(meta.phones),
                source_channels=list(meta.source_channels),
                body=body,
            )
            artifact = ArtifactRow(
                f"dossier:{parent_id}",
                ArtifactKind.DOSSIER.value,
                parent_id,
                str(dossier_path.resolve()),
                hashlib.sha256(body.encode()).hexdigest(),
                ProjectionStatus.PROJECTED.value,
                payload_json=json.dumps(record, separators=(",", ":")),
                projected_at=now_iso(),
            )
            dossier_artifacts.append(artifact)
            parent_dossier: ArtifactRow | None = parent_dossiers.get(parent_id)
            projection_rows.append(
                ArtifactReplacement(
                    ArtifactKind.DOSSIER.value,
                    (artifact, *((parent_dossier,) if parent_dossier else ())),
                    parent_id=parent_id,
                )
            )

        orphans = 0
        for path in self.dossier_dir.glob("*.md"):
            if path.stem not in written_slugs:
                path.unlink()
                orphans += 1
        written_parents = {row.parent_id for row in dossier_artifacts}
        for artifact in dossier_rows:
            if artifact.person_id:
                projection_rows.append(
                    ArtifactReplacement(
                        ArtifactKind.DOSSIER.value,
                        (),
                        person_id=artifact.person_id,
                    )
                )
            elif (
                artifact.candidate_key is None
                and artifact.artifact_key == f"dossier:{artifact.parent_id}"
                and artifact.parent_id not in written_parents
            ):
                preserved = (parent_dossiers[artifact.parent_id],) if artifact.parent_id in parent_dossiers else ()
                projection_rows.append(
                    ArtifactReplacement(
                        ArtifactKind.DOSSIER.value,
                        preserved,
                        parent_id=artifact.parent_id,
                    )
                )
        self.db.project_rows(tuple(projection_rows))
        refreshed_artifacts = artifact_rows(self.db, kind=ArtifactKind.DOSSIER.value)
        catalog_artifacts = {
            row.parent_id: row
            for row in refreshed_artifacts
            if row.status == ProjectionStatus.PROJECTED.value and row.person_id is None and row.candidate_key is None
        }
        catalog = []
        for parent in parents.values():
            artifact = catalog_artifacts.get(parent.parent_id)
            if artifact is None or not parent.display_slug or not people_by_parent.get(parent.parent_id):
                continue
            payload = parse_json_object(artifact.payload_json)
            catalog.append(
                (
                    str(payload.get("name") or parent.display_name or parent.display_slug),
                    str(payload.get("headline") or ""),
                    parent.display_slug,
                )
            )
        write_catalog(self.index_md, catalog)
        return ComposeDossierManifest(
            status="completed",
            dossiers_written=len(dossier_artifacts),
            orphans_removed=orphans,
            dossier_dir=str(self.dossier_dir),
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
    args = parser.parse_args(argv)
    result = ComposeDossier(
        db=open_existing_db(args.db),
        dossier_dir=Path(args.dossier_dir),
        index_md=Path(args.index_md),
    ).run()
    emit(result.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

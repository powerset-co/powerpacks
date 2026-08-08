"""Render complete projected facts into dossier files and a catalog.

Composition follows the pipeline's strict sequence: a parent fact is rendered only
when its owner, parent, source bundle, facts artifact, and display identity all
exist. A missing prerequisite is corruption, so composition fails instead of
silently publishing an incomplete directory.

Besides the per-parent ``dossiers/{slug}.md`` files, this writes the lookup
index (``index.md``) as one line per parent, e.g.::

    - [[jordan-bravo-a1b2c3d4]] **Jordan Bravo** — Product Manager at Acme Corp
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso, parse_json_object
from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    DOSSIER_DIR,
    DOSSIER_TEMPLATE,
    DOSSIERS_MANIFEST,
    INDEX_MD,
    emit,
)
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.collection.planning import projected_bundles
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactReplacement,
    ArtifactRow,
    FactRow,
    PARENT_DOSSIER_ARTIFACT_PREFIX,
    ParentRow,
    PersonRow,
    ProjectionStatus,
)
from packs.ingestion.primitives.deep_context.db.queries import (
    artifacts as artifact_rows,
    facts as fact_rows,
    owner_profile,
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
        owner = owner_profile(self.db)
        if owner is None:
            raise StoreError(
                "dossier composition requires an owner profile; run build-owner first"
            )
        projection_rows: list[ArtifactReplacement] = []
        dossier_artifacts: list[ArtifactRow] = []
        written_slugs: set[str] = set()

        # Only parent-owned facts are dossier sources; synthesis/normalization.py
        # migrates any remaining legacy child-owned rows before this stage runs.
        facts: dict[str, FactRow] = {row.parent_id: row for row in fact_rows(self.db, parent_owned=True)}
        facts_artifacts = artifact_rows(
            self.db,
            kind=ArtifactKind.FACTS.value,
            status=ProjectionStatus.PROJECTED.value,
            parent_owned=True,
        )
        facts_artifacts_by_key = {row.artifact_key: row for row in facts_artifacts}
        dossier_rows = artifact_rows(self.db, kind=ArtifactKind.DOSSIER.value)
        # "dossier-parent:" rows are the merge stage's own stub dossier
        # (build_parents.py), a distinct artifact_key sharing this stage's
        # (kind, parent_id) scope. project_rows() below replaces every row in
        # that scope, so the stub must be re-listed in each ArtifactReplacement
        # or it gets silently retracted alongside the composed "dossier:" row.
        parent_dossiers = {
            row.parent_id: row
            for row in dossier_rows
            if row.artifact_key == f"{PARENT_DOSSIER_ARTIFACT_PREFIX}{row.parent_id}"
        }
        bundles = projected_bundles(self.db)
        for parent_id, fact in sorted(facts.items()):
            meta: CollectionBundle | None = bundles.get(parent_id)
            if meta is None:
                raise StoreError(
                    f"dossier source bundle is absent or invalid for parent: {parent_id}"
                )
            prior: ParentRow | None = parents.get(parent_id)
            if prior is None:
                raise StoreError(f"dossier parent is absent from canonical graph: {parent_id}")
            try:
                facts_payload = json.loads(fact.facts_json or "")
            except (TypeError, json.JSONDecodeError) as exc:
                raise StoreError(
                    f"dossier facts are invalid for parent: {parent_id}"
                ) from exc
            # SynthesizedFacts.from_payload is the sanitization boundary for every
            # LLM-authored field (coerces to str/tuple, drops values of the wrong
            # shape). A payload that fails to parse at all fails composition here
            # rather than rendering a dossier with partial/untyped content.
            merged: SynthesizedFacts | None = SynthesizedFacts.from_payload(facts_payload)
            if merged is None:
                raise StoreError(f"dossier facts are invalid for parent: {parent_id}")
            facts_artifact: ArtifactRow | None = facts_artifacts_by_key.get(
                fact.artifact_key
            )
            if facts_artifact is None:
                raise StoreError(f"dossier facts artifact is absent for parent: {parent_id}")
            depth: DossierDepth | None = DossierDepth.from_payload(
                parse_json_object(facts_artifact.payload_json)
            )
            # Name priority: LLM-synthesized canonical name, then the identity
            # graph's display name, then the raw bundle's contact name — first
            # non-blank wins.
            name = next(
                (
                    value
                    for value in (
                        merged.canonical_name,
                        prior.display_name,
                        meta.full_name,
                    )
                    if value and value.strip()
                ),
                None,
            )
            if name is None:
                raise StoreError(f"dossier name is absent for parent: {parent_id}")
            slug = prior.display_slug
            if slug is None or not slug.strip():
                raise StoreError(f"dossier slug is absent for parent: {parent_id}")
            dossier_path = self.dossier_dir / f"{slug}.md"
            body = render_dossier(
                replace(meta, full_name=name),
                merged,
                depth,
                owner_emails=owner.emails,
                owner_phones=owner.phones,
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

        # Every parent in `facts` above either wrote a dossier or raised —
        # composition never reaches here partially done — so any *.md file
        # whose stem wasn't just written is stale and safe to delete.
        orphans = 0
        for path in self.dossier_dir.glob("*.md"):
            if path.stem not in written_slugs:
                path.unlink()
                orphans += 1
        written_parents = {row.parent_id for row in dossier_artifacts}
        for artifact in dossier_rows:
            if artifact.person_id:
                # Legacy child-owned dossier row: dossiers are parent-owned only
                # now, so retract it unconditionally.
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
            # A parent can exist in the canonical graph with no confirmed people
            # yet (still under review); skip those from the catalog even though
            # they'd otherwise have a slug and a projected dossier artifact.
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

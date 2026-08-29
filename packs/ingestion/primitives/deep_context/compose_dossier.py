"""Render projected facts into dossier files, projections, and a catalog."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import time
from pathlib import Path

from packs.ingestion.primitives.common.contact_fields import normalize_email, normalize_phone
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    DOSSIER_DIR,
    DOSSIER_TEMPLATE,
    DOSSIERS_MANIFEST,
    FACTS_DIR,
    INDEX_JSON,
    INDEX_MD,
    RAW_DIR,
    emit,
    slugify,
)
from packs.ingestion.primitives.deep_context.collection.state import projected_bundles
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactReplacement,
    ArtifactRow,
    IdentifierKind,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
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
    index_json: str = ""
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
        raw_dir: Path | None = None,
        facts_dir: Path | None = None,
        dossier_dir: Path | None = None,
        index_json: Path | None = None,
        index_md: Path | None = None,
        person: str = "",
    ) -> None:
        self.db = db or Db(CANONICAL_DB)
        self.dossier_dir = Path(dossier_dir or DOSSIER_DIR)
        self.index_json = Path(index_json or INDEX_JSON)
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
        people = {row.person_id: row for row in snapshot.people}
        owner_ids = {row.person_id for row in snapshot.people if row.is_owner}
        owner_ids.update(row.person_id for row in snapshot.facts if row.is_owner and row.person_id)
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
        identifiers: dict[str, dict[tuple[str, str], PersonIdentifierRow]] = {}
        for row in snapshot.identifiers:
            identifiers.setdefault(row.person_id, {})[(row.kind, row.normalized_value)] = row
        projection_rows: list[object] = []
        dossier_artifacts: list[ArtifactRow] = []
        written_slugs: set[str] = set()

        facts = {row.person_id: row for row in snapshot.facts if row.person_id}
        artifacts = {
            (row.kind, row.person_id): row
            for row in snapshot.artifacts
            if row.person_id and row.status == ProjectionStatus.PROJECTED.value
        }
        bundles = projected_bundles(snapshot)
        for person_id, fact in sorted(facts.items()):
            if self.person and person_id != self.person:
                continue
            meta = bundles.get(person_id)
            if meta is None:
                continue
            prior = people.get(person_id)
            if prior is None:
                raise StoreError(f"dossier person is absent from canonical graph: {person_id}")
            merged = _payload(fact.facts_json)
            if not merged:
                continue
            facts_artifact = artifacts.get((ArtifactKind.FACTS.value, person_id))
            depth = _payload(facts_artifact.payload_json if facts_artifact else None)
            meta.setdefault("person_id", person_id)
            name = merged.get("canonical_name") or meta.get("full_name") or "person"
            slug = slugify(name, person_id)
            dossier_path = self.dossier_dir / f"{slug}.md"
            body = render_dossier(meta, merged, depth, owner_emails=owner_emails, owner_phones=owner_phones)
            dossier_path.write_text(body, encoding="utf-8")
            written_slugs.add(slug)
            record = dict(
                person_id=person_id, name=name, path=f"dossiers/{slug}.md",
                headline=headline(merged), full_name=str(meta.get("full_name") or ""),
                emails=list(meta.get("emails") or []), phones=list(meta.get("phones") or []),
                source_channels=list(meta.get("source_channels") or []), body=body,
            )
            projection_rows.append(replace(
                prior, child_slug=slug, display_name=name, updated_at=now_iso(),
            ))
            owned_identifiers = dict(identifiers.get(person_id, {}))
            for kind, values, normalize in (
                (IdentifierKind.EMAIL.value, record["emails"], normalize_email),
                (IdentifierKind.PHONE.value, record["phones"], normalize_phone),
            ):
                for value in values:
                    display = str(value or "").strip()
                    normalized = normalize(display)
                    if normalized:
                        owned_identifiers[(kind, normalized)] = PersonIdentifierRow(
                            person_id, kind, normalized, display,
                        )
            projection_rows.append(PersonIdentifiersProjection(
                person_id,
                tuple(owned_identifiers[key] for key in sorted(owned_identifiers)),
            ))
            dossier_artifacts.append(ArtifactRow(
                f"dossier-person:{person_id}", ArtifactKind.DOSSIER.value,
                prior.parent_id, str(dossier_path.resolve()), hashlib.sha256(body.encode()).hexdigest(),
                ProjectionStatus.PROJECTED.value, person_id=person_id,
                payload_json=json.dumps(record, separators=(",", ":")), projected_at=now_iso(),
            ))

        orphans = 0
        if not self.person:
            for path in self.dossier_dir.glob("*.md"):
                if path.stem not in written_slugs:
                    path.unlink()
                    orphans += 1
        projection_rows.append(ArtifactReplacement(
            ArtifactKind.DOSSIER.value, tuple(dossier_artifacts), self.person or None,
        ))
        self.db.project_rows(tuple(projection_rows))
        catalog = [(row.name or row.slug, row.headline, row.slug)
                   for row in canonical_snapshot(self.db).dossiers if row.person_id]
        write_catalog(self.index_md, catalog)
        return ComposeDossierManifest(
            status="completed", dossiers_written=len(dossier_artifacts),
            orphans_removed=orphans, dossier_dir=str(self.dossier_dir),
            index_json=str(self.index_json), index_md=str(self.index_md),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compose markdown dossiers + lookup index from synthesized facts.",
    )
    parser.add_argument("--raw-dir", default=str(RAW_DIR))
    parser.add_argument("--facts-dir", default=str(FACTS_DIR))
    parser.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    parser.add_argument("--index-json", default=str(INDEX_JSON))
    parser.add_argument("--index-md", default=str(INDEX_MD))
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--person", default="", help="Only this person id")
    args = parser.parse_args(argv)
    result = ComposeDossier(
        db=Db(Path(args.db)),
        raw_dir=Path(args.raw_dir), facts_dir=Path(args.facts_dir),
        dossier_dir=Path(args.dossier_dir), index_json=Path(args.index_json),
        index_md=Path(args.index_md), person=args.person,
    ).run()
    emit(result.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

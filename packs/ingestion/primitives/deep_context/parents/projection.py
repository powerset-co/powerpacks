"""Canonical parent membership projection into the Deep Context database."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.contact_fields import normalize_email, normalize_phone
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db import views
from packs.ingestion.primitives.deep_context.db.models import (
    CanonicalGraphProjection,
    CanonicalSnapshot,
    IdentifierKind,
    ParentRow,
    PersonIdentifierRow,
    PersonRow,
    PersonSourceRow,
    ReviewSource,
)
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.parents.graph import read_json
from packs.ingestion.primitives.deep_context.parents.models import ProjectionCounts


class CanonicalGraphBuilder:
    """Build one typed replacement graph while retaining owned prior rows."""

    def __init__(
        self, db: Db, snapshot: CanonicalSnapshot, slugs_info: dict[str, Any], raw_dir: Path,
    ) -> None:
        self.db = db
        self.snapshot = snapshot
        self.slugs_info = slugs_info
        self.raw_dir = raw_dir
        self.parents: list[ParentRow] = []
        self.people: list[PersonRow] = []
        self.identifiers: dict[tuple[str, str, str], PersonIdentifierRow] = {}
        self.sources: dict[tuple[str, str], PersonSourceRow] = {}
        self.existing_people = {row.person_id: row for row in snapshot.people}

    def add_parent(self, parent_id: str, name: str, slug: str) -> None:
        self.parents.append(ParentRow(
            parent_id, f"parent-worth:{parent_id}", name, slug,
            source=ReviewSource.PARENT_WORTH.value, updated_at=now_iso(),
        ))

    def add_member(
        self, child_slug: str, parent_id: str, parent_slug: str, *, is_owner: bool = False,
    ) -> None:
        info = self.slugs_info[child_slug]
        person_id = str(info.get("person_id") or "").strip().lower()
        prior = self.existing_people.get(person_id)
        self.people.append(PersonRow(
            person_id, parent_id, child_slug, parent_slug,
            str(info.get("name") or info.get("full_name") or child_slug),
            int(is_owner or (prior.is_owner if prior else 0)),
            int(prior.is_ghost if prior else 0), updated_at=now_iso(),
        ))
        for kind, values, normalize in (
            (IdentifierKind.EMAIL.value, info.get("emails") or [], normalize_email),
            (IdentifierKind.PHONE.value, info.get("phones") or [], normalize_phone),
        ):
            for value in values:
                display = str(value or "").strip()
                normalized = normalize(display)
                if normalized:
                    self.identifiers[(person_id, kind, normalized)] = PersonIdentifierRow(
                        person_id, kind, normalized, display,
                    )
        for source in read_json(self.raw_dir / f"{person_id}.json").get("source_channels") or []:
            normalized_source = str(source or "").strip()
            if normalized_source:
                self.sources[(person_id, normalized_source)] = PersonSourceRow(
                    person_id, normalized_source,
                )

    def _preserve_ghosts(self) -> None:
        active_real = {row.person_id for row in self.people}
        new_parent_by_old: dict[str, set[str]] = {}
        for person in self.people:
            prior = self.existing_people.get(person.person_id)
            if prior:
                new_parent_by_old.setdefault(prior.parent_id, set()).add(person.parent_id)
        projected_ids = {row.parent_id for row in self.parents}
        projected_slugs = {row.parent_id: row.display_slug for row in self.parents}
        old_parents = {row.parent_id: row for row in self.snapshot.parents}
        for person_id, prior in sorted(self.existing_people.items()):
            if not prior.is_ghost or person_id in active_real:
                continue
            targets = new_parent_by_old.get(prior.parent_id, set())
            target = next(iter(targets)) if len(targets) == 1 else prior.parent_id
            if target not in projected_ids:
                old = old_parents[target]
                self.parents.append(ParentRow(
                    target, old.public_identifier, old.display_name, old.display_slug,
                    old.machine_worth, old.machine_worth_reason, old.source, now_iso(),
                ))
                projected_ids.add(target)
                projected_slugs[target] = old.display_slug
            self.people.append(PersonRow(
                person_id, target, prior.child_slug, projected_slugs.get(target),
                prior.display_name, prior.is_owner, 1, prior.facts_json,
                prior.confidence, now_iso(),
            ))

    def apply(self) -> ProjectionCounts:
        self._preserve_ghosts()
        active_people = {row.person_id for row in self.people}
        for row in self.snapshot.identifiers:
            key = (row.person_id, row.kind, row.normalized_value)
            if row.person_id in active_people:
                self.identifiers.setdefault(key, row)
        for row in self.snapshot.sources:
            key = (row.person_id, row.source)
            if row.person_id in active_people:
                self.sources.setdefault(key, row)
        prior_human = {
            row.parent_id for row in self.snapshot.parents if row.human_worth is not None
        }
        removed = 0
        if self.slugs_info or not self.existing_people:
            counts = self.db.replace_canonical_graph(CanonicalGraphProjection(
                parents=tuple(self.parents), people=tuple(self.people),
                identifiers=tuple(self.identifiers[key] for key in sorted(self.identifiers)),
                sources=tuple(self.sources[key] for key in sorted(self.sources)),
            ))
            removed = counts.parents_removed
        current = canonical_snapshot(self.db)
        return ProjectionCounts(
            parent_rows=len(views.worth_review(self.db, "rows")),
            human_migrated=sum(
                row.parent_id not in prior_human
                for row in current.parents if row.human_worth is not None
            ),
            stale_parent_rows_removed=removed,
        )

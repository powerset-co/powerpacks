"""The share stage's one reader: the roster joined to the canonical store.

The roster arrives through `ensure_parents.imported_people` — people.csv's only
Deep Context reader — and everything the stage knows about a person's context
comes from SQLite: parents (worth), facts (labels, overlaps), artifacts (dossier
bodies and body-free message bundles). No CSV state is read or written here.

Flow: `ShareEvidence(db).load()` -> `list[PersonEvidence]`.

Changelog:
  2026-09-24: read facts, worth, dossiers, and bundles from SQLite, not files.
  2026-09-24: created (split out of share.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    FactRow,
    MachineWorth,
    ParentSnapshotRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import read_imported_people
from packs.ingestion.primitives.deep_context.shared.common import DEFAULT_PEOPLE_CSV
from packs.ingestion.primitives.share.models import NO_MESSAGES, MessageStats, PersonEvidence


def _overlaps(facts: dict[str, Any] | None) -> frozenset[str]:
    """`facts.shared_context[].overlap` values (school | employer | location | era | other)."""
    return frozenset(
        str(entry.get("overlap") or "")
        for entry in (facts or {}).get("shared_context") or []
        if isinstance(entry, dict)
    )


def _messages(bundle: dict[str, Any] | None) -> MessageStats:
    """Body-free message stats. Reads ONLY direction/at/channel and the group
    count — `subject` and `text` are never touched, here or anywhere after."""
    if not bundle:
        return NO_MESSAGES
    timestamps: list[str] = []
    channels: list[str] = []
    from_me = from_them = 0
    for message in bundle.get("messages") or []:
        at = str(message.get("at") or "")
        if at:
            timestamps.append(at)
        channel = str(message.get("channel") or "")
        if channel and channel not in channels:
            channels.append(channel)
        direction = str(message.get("direction") or "")
        if direction == "from_me":
            from_me += 1
        elif direction == "from_them":
            from_them += 1
    return MessageStats(
        first_at=min(timestamps) if timestamps else None,
        last_at=max(timestamps) if timestamps else None,
        from_me=from_me,
        from_them=from_them,
        group_count=len(bundle.get("groups") or []),
        channels=tuple(channels),
    )


class ShareEvidence:
    """The roster joined to the canonical store, parsed once at the boundary."""

    def __init__(self, db: Db, *, people_csv: Path = DEFAULT_PEOPLE_CSV) -> None:
        self.db = db
        self.people_csv = Path(people_csv)

    def load(self) -> list[PersonEvidence]:
        parent_of_person = {row.person_id: row.parent_id for row in queries.people(self.db)}
        parents = {row.parent_id: row for row in queries.parents(self.db)}
        parent_facts = {row.parent_id: row for row in queries.facts(self.db, parent_owned=True)}
        dossiers = self._artifacts(ArtifactKind.DOSSIER.value, lambda payload: str(payload.get("body") or ""))
        bundles = self._artifacts(ArtifactKind.SOURCE_BUNDLE.value, lambda payload: payload)
        children: dict[str, list[str]] = {}
        for person_id, parent_id in parent_of_person.items():
            children.setdefault(parent_id, []).append(person_id)

        people: list[PersonEvidence] = []
        for imported in read_imported_people(self.people_csv):
            identities = {imported.person_id, *imported.superseded_person_ids}
            parent_ids = {parent_of_person[key] for key in identities if key in parent_of_person}
            for parent_id in parent_ids:
                identities.update(children.get(parent_id, ()))
            parent_id = next(iter(sorted(parent_ids)), "")
            facts = _facts_payload(parent_facts.get(parent_id))
            people.append(
                PersonEvidence(
                    person_id=imported.person_id,
                    public_identifier=imported.public_identifier or None,
                    full_name=imported.display_name,
                    source_channels=imported.source_channels,
                    interaction_counts=imported.interaction_counts,
                    last_interaction=imported.last_interaction or None,
                    superseded_person_ids=imported.superseded_person_ids,
                    network_worth=self._worth(parents.get(parent_id), facts),
                    dossier=dossiers.get(parent_id) or None,
                    facts=facts,
                    shared_overlaps=_overlaps(facts),
                    messages=_messages(bundles.get(parent_id)),
                )
            )
        return people

    def _artifacts(self, kind: str, extract: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        """Map parent_id -> extracted payload, preferring the parent's own artifact."""
        index: dict[str, Any] = {}
        rows = sorted(queries.artifacts(self.db, kind=kind), key=lambda row: row.person_id is not None)
        for row in rows:
            value = extract(parse_json_object(row.payload_json))
            if value in (None, "", [], {}):
                continue
            index[row.parent_id] = value
        return index

    def _worth(self, parent: ParentSnapshotRow | None, facts: dict[str, Any] | None) -> str:
        """Effective worth: the human's word, then the model's, then the fact's."""
        if parent is not None and parent.human_worth:
            return parent.human_worth
        if parent is not None and parent.machine_worth:
            return parent.machine_worth
        return str((facts or {}).get("network_worth", {}).get("decision") or MachineWorth.MAYBE.value)


def _facts_payload(row: FactRow | None) -> dict[str, Any] | None:
    return parse_json_object(row.facts_json) if row is not None else None

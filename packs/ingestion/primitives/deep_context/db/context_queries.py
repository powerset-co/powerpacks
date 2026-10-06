"""Narrow typed reads for message collection and dossier evidence.

Changelog:
- 2026-09-25: the identifier read walks the family's people first; the kind index
  had the planner scanning every identifier per call.
- 2026-09-25: id sets bind as one JSON array read through json_each, so the
  variable count no longer grows with the install (27k parents bound twice
  exceeded SQLite's 32,766-variable limit and blocked the cluster survey).
"""

from __future__ import annotations

from collections.abc import Sequence
import json
from packs.ingestion.primitives.deep_context.synthesis.history import FactHistory

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    FactRow,
    MESSAGE_CHANNELS,
    ParentSnapshotRow,
    PersonIdentifierRow,
    PersonRow,
)
from packs.ingestion.primitives.deep_context.db.queries import typed_rows, artifacts, imported_people, people, identifiers
from packs.ingestion.primitives.deep_context.db.schema import ID_SET, id_set
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import (
    CollectionSourceRow,
    DossierEvidenceRows,
)
from packs.ingestion.primitives.pipeline.contract import PeopleRow


def dossier_evidence_rows(
    db: Db,
    subject_ids: Sequence[str],
) -> DossierEvidenceRows:
    """Read only the parent families needed by one evidence packet."""
    wanted = tuple(sorted({value.strip().lower() for value in subject_ids if value.strip()}))
    if not wanted:
        return DossierEvidenceRows((), (), (), (), ())
    wanted_json = id_set(wanted)
    matched_people = typed_rows(
        db,
        f"""
SELECT * FROM people
WHERE lower(person_id) IN {ID_SET}
   OR lower(parent_id) IN {ID_SET}
ORDER BY person_id
""",
        PersonRow,
        (wanted_json, wanted_json),
    )
    direct_parents = typed_rows(
        db,
        f"SELECT * FROM parents WHERE lower(parent_id) IN {ID_SET} ORDER BY parent_id",
        ParentSnapshotRow,
        (wanted_json,),
    )
    parent_ids = tuple(sorted({row.parent_id for row in matched_people} | {row.parent_id for row in direct_parents}))
    if not parent_ids:
        return DossierEvidenceRows((), matched_people, (), (), ())
    parents_json = id_set(parent_ids)
    family_people = typed_rows(
        db,
        f"SELECT * FROM people WHERE parent_id IN {ID_SET} ORDER BY person_id",
        PersonRow,
        (parents_json,),
    )
    family_parents = typed_rows(
        db,
        f"SELECT * FROM parents WHERE parent_id IN {ID_SET} ORDER BY parent_id",
        ParentSnapshotRow,
        (parents_json,),
    )
    family_facts = typed_rows(
        db,
        f"SELECT * FROM facts WHERE parent_id IN {ID_SET} ORDER BY subject_key",
        FactRow,
        (parents_json,),
    )
    source_bundles = typed_rows(
        db,
        f"""
SELECT * FROM artifacts
WHERE parent_id IN {ID_SET}
  AND kind='source_bundle'
  AND status='projected'
ORDER BY artifact_key
""",
        ArtifactRow,
        (parents_json,),
    )
    # `+pi.kind` keeps the planner off the kind index, which made it scan every
    # identifier per batch (3.3 s on 12k people) instead of walking the family's
    # people first (1.5 ms).
    identifiers = typed_rows(
        db,
        f"""
SELECT pi.* FROM person_identifiers pi
JOIN people pe USING(person_id)
WHERE pe.parent_id IN {ID_SET}
  AND +pi.kind IN ('email', 'phone')
ORDER BY pi.person_id, pi.kind, pi.normalized_value
""",
        PersonIdentifierRow,
        (parents_json,),
    )
    return DossierEvidenceRows(
        family_parents,
        family_people,
        family_facts,
        source_bundles,
        identifiers,
    )


def dossier_message_count(db: Db, parent_id: str) -> int:
    """Count extracted messages, electing parent facts before child facts."""
    payloads = db.query("""
        SELECT a.payload_json FROM facts f JOIN artifacts a USING(artifact_key)
        WHERE f.parent_id=? AND (f.person_id IS NULL OR NOT EXISTS (
            SELECT 1 FROM facts parent WHERE parent.parent_id=f.parent_id AND parent.person_id IS NULL
        ))
    """, (parent_id,))
    # Distinct observed messages: a re-extraction and a family's shared thread are seen once.
    # Old records without observations keep their stored tally.
    seen: set[str] = set()
    untracked = 0
    for row in payloads:
        history = FactHistory.from_payload(json.loads(row["payload_json"] or "{}"))
        if history.messages:
            seen |= history.processed
        else:
            untracked += sum(item.record.messages_used for item in history.records)
    return len(seen) + untracked


def collection_sources(db: Db) -> tuple[CollectionSourceRow, ...]:
    """Read each contact's own message-store lookup keys."""
    message_channels = tuple(sorted(MESSAGE_CHANNELS))
    placeholders = ",".join("?" for _ in message_channels)
    names: dict[str, str] = {}
    emails: dict[str, set[str]] = {}
    phones: dict[str, set[str]] = {}
    channels: dict[str, set[str]] = {}
    for row in db.query(
        f"""
SELECT pe.person_id, pe.display_name, pi.kind, pi.normalized_value, ps.source
FROM people pe
JOIN person_identifiers pi USING(person_id)
JOIN person_sources ps USING(person_id)
WHERE pe.is_owner=0
  AND pi.kind IN ('email', 'phone')
  AND EXISTS (
      SELECT 1 FROM person_sources message_source
      WHERE message_source.person_id=pe.person_id
        AND message_source.source IN ({placeholders})
  )
ORDER BY pe.person_id, pi.kind, pi.normalized_value, ps.source
""",
        message_channels,
    ):
        person_id = str(row["person_id"])
        names.setdefault(person_id, str(row["display_name"] or ""))
        target = emails if row["kind"] == "email" else phones
        target.setdefault(person_id, set()).add(str(row["normalized_value"]))
        channels.setdefault(person_id, set()).add(str(row["source"]))
    return tuple(
        CollectionSourceRow(
            person_id,
            names[person_id],
            tuple(sorted(emails.get(person_id, set()))),
            tuple(sorted(phones.get(person_id, set()))),
            tuple(sorted(channels.get(person_id, set()))),
        )
        for person_id in names
    )


def collection_bundle_group_message_count(db: Db) -> int:
    """Count retained iMessage group-chat message bodies for the collection manifest's privacy block.

    A scalar COUNT(*) over json_each, not a full bundle parse: describes the
    store as it now stands, across every parent-owned projected source
    bundle. The literal 'imessage_group' is
    collection.models.MessageChannel.IMESSAGE_GROUP's value, pinned here so
    db/ never imports the collection package.
    """
    rows = db.query(
        """
SELECT COUNT(*) AS n
FROM artifacts a, json_each(a.payload_json, '$.messages') m
WHERE a.kind=? AND a.status='projected' AND a.person_id IS NULL
  AND json_extract(m.value, '$.channel')='imessage_group'
""",
        (ArtifactKind.SOURCE_BUNDLE.value,),
    )
    return int(rows[0]["n"]) if rows else 0


def singleton_people(db: Db) -> dict[str, str]:
    """Parent histories are contact evidence only for one-contact families."""
    return {row['parent_id']: row['person_id'] for row in db.query(
        "SELECT parent_id,person_id FROM people GROUP BY parent_id HAVING count(*)=1")}


def aggregate_people(db: Db) -> frozenset[str]:
    """Metadata aggregates whose copied message keys belong to actual source contacts."""
    return aggregate_people_from_rows(people(db), identifiers(db), imported_people(db))


def aggregate_people_from_rows(people_rows: tuple[PersonRow, ...],
                               identifier_rows: tuple[PersonIdentifierRow, ...],
                               imported_rows: tuple[PeopleRow, ...]) -> frozenset[str]:
    """Share aggregate ownership policy with the read-only identity audit."""
    parent_of = {row.person_id: row.parent_id for row in people_rows}
    keyed = {row.person_id for row in identifier_rows if row.kind in {'email', 'phone'}}
    aggregates = set()
    for row in imported_rows:
        aliases = json.loads(row.superseded_person_ids or '[]')
        source_parents = {parent_of[person] for person in aliases
                          if person.startswith('candidate:') and person in keyed and person in parent_of}
        aggregates.update(person for person in (row.id, *aliases)
                          if not person.startswith('candidate:') and person not in keyed
                          and parent_of.get(person) in source_parents)
    return frozenset(aggregates)


def person_histories(db: Db) -> dict[str, FactHistory]:
    """Contact extraction coverage; mixed parent histories prove no child facts."""
    singleton = singleton_people(db)
    rows = artifacts(db, kind='facts', status='projected')
    grouped: dict[str, list] = {}
    for row in sorted(rows, key=lambda item: bool(item.person_id)):
        if row.artifact_key.startswith('parent-facts:'):
            continue
        person_id = row.person_id
        if person_id is None:
            person_id = singleton.get(row.parent_id)
            if person_id is None:
                continue
        history = FactHistory.from_payload(json.loads(row.payload_json or '{}'))
        grouped.setdefault(person_id, []).extend(item.payload() for item in history.records)
    return {person: FactHistory.from_records(records) for person, records in grouped.items()}

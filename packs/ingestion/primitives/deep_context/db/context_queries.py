"""Narrow typed reads for message collection and dossier evidence."""

from __future__ import annotations

from collections.abc import Sequence

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    FactRow,
    MESSAGE_CHANNELS,
    ParentSnapshotRow,
    PersonRow,
)
from packs.ingestion.primitives.deep_context.db.queries import typed_rows
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import (
    CollectionSourceRow,
    DossierEvidenceRows,
)


def dossier_evidence_rows(
    db: Db,
    subject_ids: Sequence[str],
) -> DossierEvidenceRows:
    """Read only the parent families needed by one evidence packet."""
    wanted = tuple(sorted({value.strip().lower() for value in subject_ids if value.strip()}))
    if not wanted:
        return DossierEvidenceRows((), (), (), ())
    placeholders = ",".join("?" for _ in wanted)
    matched_people = typed_rows(
        db,
        f"""
SELECT * FROM people
WHERE lower(person_id) IN ({placeholders})
   OR lower(parent_id) IN ({placeholders})
ORDER BY person_id
""",
        PersonRow,
        wanted + wanted,
    )
    direct_parents = typed_rows(
        db,
        f"SELECT * FROM parents WHERE lower(parent_id) IN ({placeholders}) ORDER BY parent_id",
        ParentSnapshotRow,
        wanted,
    )
    parent_ids = tuple(sorted({row.parent_id for row in matched_people} | {row.parent_id for row in direct_parents}))
    if not parent_ids:
        return DossierEvidenceRows((), matched_people, (), ())
    parent_placeholders = ",".join("?" for _ in parent_ids)
    family_people = typed_rows(
        db,
        f"SELECT * FROM people WHERE parent_id IN ({parent_placeholders}) ORDER BY person_id",
        PersonRow,
        parent_ids,
    )
    family_parents = typed_rows(
        db,
        f"SELECT * FROM parents WHERE parent_id IN ({parent_placeholders}) ORDER BY parent_id",
        ParentSnapshotRow,
        parent_ids,
    )
    family_facts = typed_rows(
        db,
        f"SELECT * FROM facts WHERE parent_id IN ({parent_placeholders}) ORDER BY subject_key",
        FactRow,
        parent_ids,
    )
    source_bundles = typed_rows(
        db,
        f"""
SELECT * FROM artifacts
WHERE parent_id IN ({parent_placeholders})
  AND kind='source_bundle'
  AND status='projected'
ORDER BY artifact_key
""",
        ArtifactRow,
        parent_ids,
    )
    return DossierEvidenceRows(
        family_parents,
        family_people,
        family_facts,
        source_bundles,
    )


def collection_sources(db: Db) -> tuple[CollectionSourceRow, ...]:
    """Read message-bearing parents and aggregate their typed lookup keys."""
    message_channels = tuple(sorted(MESSAGE_CHANNELS))
    placeholders = ",".join("?" for _ in message_channels)
    names: dict[str, str] = {}
    emails: dict[str, set[str]] = {}
    phones: dict[str, set[str]] = {}
    channels: dict[str, set[str]] = {}
    for row in db.query(
        f"""
SELECT p.parent_id, p.display_name, pi.kind, pi.normalized_value, ps.source
FROM parents p
JOIN people pe USING(parent_id)
JOIN person_identifiers pi USING(person_id)
JOIN person_sources ps USING(person_id)
WHERE pe.is_owner=0
  AND pi.kind IN ('email', 'phone')
  AND EXISTS (
      SELECT 1 FROM person_sources message_source
      WHERE message_source.person_id=pe.person_id
        AND message_source.source IN ({placeholders})
  )
ORDER BY p.parent_id, pi.kind, pi.normalized_value, ps.source
""",
        message_channels,
    ):
        parent_id = str(row["parent_id"])
        names.setdefault(parent_id, str(row["display_name"] or ""))
        target = emails if row["kind"] == "email" else phones
        target.setdefault(parent_id, set()).add(str(row["normalized_value"]))
        channels.setdefault(parent_id, set()).add(str(row["source"]))
    return tuple(
        CollectionSourceRow(
            parent_id,
            names[parent_id],
            tuple(sorted(emails.get(parent_id, set()))),
            tuple(sorted(phones.get(parent_id, set()))),
            tuple(sorted(channels.get(parent_id, set()))),
        )
        for parent_id in names
    )


def collection_bundle_parent_ids(db: Db) -> frozenset[str]:
    """Parent ids that currently own a projected source-bundle artifact.

    A scalar id set, not a full bundle-payload parse: the collection stage's
    orphan sweep only needs to know WHICH parents have a bundle, never their
    message bodies.
    """
    return frozenset(
        str(row["parent_id"])
        for row in db.query(
            "SELECT parent_id FROM artifacts WHERE kind=? AND status='projected' AND person_id IS NULL",
            (ArtifactKind.SOURCE_BUNDLE.value,),
        )
    )


def existing_parent_ids(db: Db) -> frozenset[str]:
    """Every canonical parent id currently on record.

    The collection stage orphan sweep's other half: a bundle is stale when
    its parent_id is absent from this set — never merely because a run's
    message-channel selection (collection_sources) happened to skip it.
    """
    return frozenset(str(row["parent_id"]) for row in db.query("SELECT parent_id FROM parents"))


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

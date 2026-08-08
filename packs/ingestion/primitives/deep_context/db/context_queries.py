"""Narrow typed reads for message collection and dossier evidence."""

from __future__ import annotations

import json
from collections.abc import Sequence

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    FactRow,
    MESSAGE_CHANNELS,
    ParentSnapshotRow,
    PersonRow,
    RowKind,
)
from packs.ingestion.primitives.deep_context.db.queries import typed_rows
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
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


def _rekeyed_synthetic_profile_json(profile_json: str, old_key: str, new_key: str) -> str:
    """Fix the identity fields a legacy synthetic row's OWN json body carries
    under the old key. `assemble_synthetic_profile.build_synthetic_row` wrote
    `public_identifier` as the row's key always, and `id`/`entity_urn` as the
    same key specifically when no directory person_id existed yet for that
    parent — those are the only fields that can hold `old_key`; every
    evidence field (name, headline, positions, ...) is untouched by the
    rename. A row whose body predates these fields, or fails to parse, is
    returned unchanged — the SQL columns are still correctly re-keyed either
    way."""
    try:
        payload = json.loads(profile_json)
    except json.JSONDecodeError:
        return profile_json
    if not isinstance(payload, dict):
        return profile_json
    if payload.get("public_identifier") == old_key:
        payload["public_identifier"] = new_key
    if payload.get("id") == old_key:
        payload["id"] = new_key
    if payload.get("entity_urn") == f"synthetic:{old_key}":
        payload["entity_urn"] = f"synthetic:{new_key}"
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# Tables (besides `links` itself) that can carry a synthetic candidate's
# row_key. `research`/`guidance`/`jobs` never point at a synthetic row_key in
# practice (their candidate_key names the REAL candidate that led to the
# research, not the synthetic row derived from its result) but are included
# defensively — each UPDATE is a cheap no-op when it doesn't apply.
_SYNTHETIC_CANDIDATE_KEY_TABLES = ("candidate_people", "artifacts", "research", "guidance", "jobs")


def migrate_legacy_synthetic_keys(db: Db) -> int:
    """Re-key every pre-existing synthetic `links` row onto its parent id.

    Before 2026-08-08, a synthetic row's `links.row_key`/`public_identifier`
    (and its dependent `candidate_people`/`artifacts`/`synthetic_profiles`
    rows, including the identity fields embedded in
    `synthetic_profiles.profile_json` itself — see
    `_rekeyed_synthetic_profile_json`) were a hash of whichever email/phone
    won that assembly run. This finds every synthetic row whose key predates
    that change (`row_key != parent_id`) and renames it in place, in one
    transaction per row with FK checks deferred to the end (the same pattern
    `Db.merge_parents` uses), so every other column — including a human
    `decision_action`/`decision_approved` — survives untouched under the new
    key. `synthetic_profiles.source_artifact_key` is left pointing at the old
    `artifacts.artifact_key`; that row still exists (only its `candidate_key`
    moved), and the next successful assembly for this parent naturally
    reprojects a fresh artifact under the new key.

    Called by `assemble_synthetic_profile.AssembleSyntheticProfile.execute`
    first, every run — idempotent and cheap: a fresh or already-migrated
    install has zero rows matching the WHERE clause, so this is one SELECT
    and no writes.

    REMOVAL CONDITION: delete once no supported install predates powerpacks
    v1.18.1 (the release that ships this rekey).
    """
    rows = db.query(
        "SELECT l.row_key AS old_key, l.parent_id AS new_key, sp.profile_json AS profile_json "
        "FROM links l JOIN synthetic_profiles sp ON sp.candidate_key=l.row_key "
        "WHERE l.kind=:kind AND l.row_key != l.parent_id",
        {"kind": RowKind.SYNTHETIC.value},
    )
    if not rows:
        return 0
    with db.transaction() as conn:
        conn.execute("PRAGMA defer_foreign_keys=ON")
        if not conn.in_transaction:
            conn.execute("BEGIN DEFERRED")
        for row in rows:
            old_key, new_key = row["old_key"], row["new_key"]
            conn.execute(
                "UPDATE links SET row_key=:new, public_identifier=:new WHERE row_key=:old",
                {"new": new_key, "old": old_key},
            )
            for table in _SYNTHETIC_CANDIDATE_KEY_TABLES:
                column = "row_key" if table == "candidate_people" else "candidate_key"
                conn.execute(
                    f"UPDATE {table} SET {column}=:new WHERE {column}=:old",
                    {"new": new_key, "old": old_key},
                )
            conn.execute(
                "UPDATE synthetic_profiles SET public_identifier=:new, candidate_key=:new, "
                "profile_json=:profile_json WHERE candidate_key=:old",
                {
                    "new": new_key,
                    "old": old_key,
                    "profile_json": _rekeyed_synthetic_profile_json(
                        row["profile_json"], old_key, new_key
                    ),
                },
            )
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise StoreError(f"synthetic key migration violates foreign keys: {violations[0]}")
    return len(rows)

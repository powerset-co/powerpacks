"""Projected dossier lookup, person detail, and avatar reads."""
from __future__ import annotations

from typing import Any

from packs.ingestion.primitives.common.contact_fields import normalize_email
from packs.ingestion.primitives.deep_context.common import normalize_name, phone_digits
from packs.ingestion.primitives.deep_context.db._view_rows import _hydrate_parents, _json
from packs.ingestion.primitives.deep_context.db._view_sql import PARENT_SELECT, WORTH_CTE
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db


def person_lookup(
    db: Db, *, name: str = "", phone: str = "", email: str = "",
) -> list[dict[str, Any]]:
    """Match projected dossiers with the existing phone/email/name policy."""
    records = [
        {
            "slug": row.slug,
            "name": row.name,
            "path": row.path,
            "dossier_path": row.artifact_path,
            "dossier_body": row.body,
            "headline": row.headline,
            "full_name": row.full_name,
            "emails": list(row.emails),
            "phones": list(row.phones),
            "parent_id": row.parent_id,
            **({"person_id": row.person_id} if row.person_id else {}),
            **({"children": list(row.children)} if row.children else {}),
        }
        for row in canonical_snapshot(db).dossiers
    ]
    maps: dict[str, dict[str, list[str]]] = {"email": {}, "phone": {}, "name": {}}
    by_slug = {row["slug"]: row for row in records}
    for row in records:
        slug = row["slug"]
        keys = {
            "email": [normalize_email(str(value or "")) for value in row["emails"]],
            "phone": [phone_digits(str(value or "")) for value in row["phones"]],
            "name": [normalize_name(row["name"])],
        }
        if "children" not in row:
            keys["name"].append(normalize_name(row["full_name"]))
        for kind, values in keys.items():
            for key in sorted(set(values)):
                if key and slug not in maps[kind].setdefault(key, []):
                    maps[kind][key].append(slug)

    hits: list[str] = []
    if phone:
        hits.extend(maps["phone"].get(phone_digits(phone), []))
    if email:
        hits.extend(maps["email"].get(normalize_email(email), []))
    if name:
        key = normalize_name(name)
        if key in maps["name"]:
            hits.extend(maps["name"][key])
        else:
            tokens = set(key.split())
            for candidate, slugs in maps["name"].items():
                if tokens and tokens <= set(candidate.split()):
                    hits.extend(slugs)
    return [by_slug[slug] for index, slug in enumerate(hits) if slug not in hits[:index]]


def person_detail(db: Db, slug_or_parent_id: str) -> dict[str, Any] | None:
    """One SQL-hydrated parent with the requested projected dossier body."""
    rows = db.query(
        WORTH_CTE
        + PARENT_SELECT.format(
            where=(
                "WHERE p.parent_id=? OR p.display_slug=? OR p.public_identifier=? "
                "OR EXISTS (SELECT 1 FROM people pe WHERE pe.parent_id=p.parent_id "
                "AND (pe.person_id=? OR pe.child_slug=?))"
            )
        ),
        (slug_or_parent_id,) * 5,
    )
    hydrated = _hydrate_parents(db, rows[:1], pending_only=False)
    if not hydrated:
        return None
    child = db.query(
        "SELECT a.path, a.payload_json FROM people pe JOIN artifacts a ON a.person_id=pe.person_id "
        "WHERE a.kind='dossier' AND a.status='projected' "
        "AND (pe.person_id=? OR pe.child_slug=?) "
        "ORDER BY a.projected_at DESC, a.artifact_key LIMIT 1",
        (slug_or_parent_id, slug_or_parent_id),
    )
    if child:
        payload = _json(child[0]["payload_json"], {})
        hydrated[0]["dossier_path"] = child[0]["path"]
        hydrated[0]["dossier_body"] = (
            str(payload.get("body") or "") if isinstance(payload, dict) else ""
        )
    return hydrated[0]


def avatar_payload(db: Db, public_identifier: str) -> dict[str, Any] | None:
    """Projected image bytes and content type for one LinkedIn candidate."""
    rows = db.query(
        "SELECT a.payload_json FROM links l JOIN artifacts a ON a.candidate_key=l.row_key "
        "WHERE a.kind='avatar' AND a.status='projected' AND (l.public_identifier=? "
        "OR l.machine_proposed_public_identifier=? OR l.replacement_public_identifier=?) "
        "ORDER BY a.projected_at DESC, a.artifact_key LIMIT 1",
        (public_identifier, public_identifier, public_identifier),
    )
    payload = _json(rows[0]["payload_json"], {}) if rows else {}
    return payload if isinstance(payload, dict) and payload.get("base64") else None

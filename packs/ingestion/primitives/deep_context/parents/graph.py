"""Typed input loading and deterministic canonical-parent graph policy."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.contact_fields import normalize_email
from packs.ingestion.primitives.common.jsonio import write_json
from packs.ingestion.primitives.deep_context.common import (
    OWNER_JSON,
    load_owner,
    parent_identifiers,
    read_jsonl,
    slugify,
)
from packs.ingestion.primitives.deep_context.dossier.facts import merge_facts
from packs.ingestion.primitives.deep_context.parents.models import ChildEntry, ParentPlan


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def is_owner(person_id: str, facts_dir: Path) -> bool:
    if not person_id:
        return False
    return any(
        (record.get("facts") or {}).get("is_owner")
        for record in read_jsonl(facts_dir / f"{person_id}.jsonl")
    )


def fold_owner_aliases(
    owner_slugs: set[str], slugs_info: dict[str, Any], raw_dir: Path,
) -> list[str]:
    owner = load_owner() or {}
    if not owner:
        return []
    existing = [normalize_email(email) for email in owner.get("emails") or []]
    added: list[str] = []
    for slug in owner_slugs:
        person_id = slugs_info.get(slug, {}).get("person_id", "")
        bundle = read_json(raw_dir / f"{person_id}.json") if person_id else {}
        for email in bundle.get("emails") or []:
            normalized = normalize_email(email)
            if normalized and "@" in normalized and normalized not in existing + added:
                added.append(normalized)
    if added:
        owner["emails"] = (owner.get("emails") or []) + added
        write_json(OWNER_JSON, owner)
    return added


def load_pairs(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def superseded_pairs(path: Path, slugs_info: dict[str, Any]) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    slug_by_person = {
        str((info or {}).get("person_id") or "").strip().lower(): slug
        for slug, info in slugs_info.items()
    }
    pairs: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            raw = (row.get("superseded_person_ids") or "").strip()
            if not raw:
                continue
            try:
                superseded = json.loads(raw)
            except json.JSONDecodeError:
                continue
            durable = slug_by_person.get((row.get("id") or "").strip().lower())
            if not durable:
                continue
            name = (row.get("full_name") or "").strip()
            for person_id in superseded if isinstance(superseded, list) else []:
                old = slug_by_person.get(str(person_id or "").strip().lower())
                if old and old != durable:
                    pairs.append({
                        "slug_a": durable, "name_a": name,
                        "slug_b": old, "name_b": name,
                        "confidence": "1.0", "tone_consistent": "true",
                        "reason": "import-superseded identity: the same contact row "
                                  "under its pre-match key",
                    })
    return pairs


def clusters_from_pairs(pairs: list[dict[str, Any]]) -> list[list[str]]:
    parents: dict[str, str] = {}

    def find(slug: str) -> str:
        parents.setdefault(slug, slug)
        while parents[slug] != slug:
            parents[slug] = parents[parents[slug]]
            slug = parents[slug]
        return slug

    for pair in pairs:
        parents[find(pair["slug_a"])] = find(pair["slug_b"])
    groups: dict[str, list[str]] = {}
    for slug in list(parents):
        groups.setdefault(find(slug), []).append(slug)
    return [sorted(group) for group in groups.values() if len(group) > 1]


def parent_id_for(child_person_ids: list[str]) -> str:
    digest = hashlib.sha1("|".join(sorted(child_person_ids)).encode()).hexdigest()
    return f"parent-{digest[:12]}"


def plan_parents(
    clusters: list[list[str]], pairs: list[dict[str, Any]], index: dict[str, Any],
    owner_slugs: set[str], facts_dir: Path, raw_dir: Path,
) -> list[ParentPlan]:
    slugs_info = index.get("slugs") or {}
    pair_rows = {tuple(sorted((row["slug_a"], row["slug_b"]))): row for row in pairs}
    plans: list[ParentPlan] = []
    for cluster in clusters:
        members = [slug for slug in cluster if slug in slugs_info and slug not in owner_slugs]
        if len(members) < 2:
            continue

        def confidence(slug: str) -> float:
            return max((
                float(pair_rows[tuple(sorted((slug, other)))].get("confidence")
                      or pair_rows[tuple(sorted((slug, other)))].get("score") or 0)
                for other in members
                if other != slug and tuple(sorted((slug, other))) in pair_rows
            ), default=0.0)

        def child(slug: str) -> ChildEntry:
            info = slugs_info[slug]
            reason = next((
                pair_rows[tuple(sorted((slug, other)))]["reason"]
                for other in members
                if other != slug and tuple(sorted((slug, other))) in pair_rows
            ), "")
            channels = read_json(raw_dir / f"{info['person_id']}.json").get("source_channels") or []
            return ChildEntry(
                slug, info.get("name", slug), confidence(slug), reason,
                tuple(channels), info["person_id"],
            )

        confirmed = tuple(child(slug) for slug in members)
        records = [
            record
            for item in confirmed
            for record in read_jsonl(facts_dir / f"{item.person_id}.jsonl")
        ]
        merged = merge_facts(records)
        name = merged.get("canonical_name") or confirmed[0].name
        parent_id = parent_id_for([item.person_id for item in confirmed])
        emails, phones = parent_identifiers(index, [item.slug for item in confirmed])
        plans.append(ParentPlan(
            parent_id, slugify(name, parent_id), name, tuple(emails), tuple(phones),
            confirmed, merged,
        ))
    return plans


def singleton_plan(child_slug: str, info: dict[str, Any], index: dict[str, Any]) -> ParentPlan:
    person_id = info["person_id"]
    name = info.get("name", child_slug)
    parent_id = parent_id_for([person_id])
    emails, phones = parent_identifiers(index, [child_slug])
    child = ChildEntry(child_slug, name, 0.0, "", (), person_id)
    return ParentPlan(
        parent_id, slugify(name, parent_id), name, tuple(emails), tuple(phones),
        (child,), {"headline": info.get("headline", "")},
    )

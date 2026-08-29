"""Deterministic canonical-parent graph policy over SQLite snapshots."""
from __future__ import annotations

import hashlib
from typing import Any

from packs.ingestion.primitives.deep_context.common import slugify
from packs.ingestion.primitives.deep_context.dossier.facts import merge_facts
from packs.ingestion.primitives.deep_context.merge_candidates.blocking import connected_components
from packs.ingestion.primitives.deep_context.parents.models import ChildEntry, ParentPlan


def clusters_from_pairs(pairs: list[dict[str, Any]]) -> list[list[str]]:
    nodes = list(dict.fromkeys(
        pair[key] for pair in pairs for key in ("slug_a", "slug_b")
    ))
    return [sorted(group) for group in connected_components(
        nodes, [(pair["slug_a"], pair["slug_b"]) for pair in pairs],
    )]


def parent_id_for(child_person_ids: list[str]) -> str:
    digest = hashlib.sha1("|".join(sorted(child_person_ids)).encode()).hexdigest()
    return f"parent-{digest[:12]}"


def _identifiers(slugs_info: dict[str, Any], child_slugs: list[str]) -> tuple[list[str], list[str]]:
    emails: list[str] = []
    phones: list[str] = []
    for child in child_slugs:
        record = slugs_info.get(child) or {}
        emails.extend(value for value in record.get("emails") or [] if value not in emails)
        phones.extend(value for value in record.get("phones") or [] if value not in phones)
    return emails, phones


def plan_parents(
    clusters: list[list[str]], pairs: list[dict[str, Any]], slugs_info: dict[str, Any],
    owner_slugs: set[str], facts_by_person: dict[str, dict[str, Any]],
) -> list[ParentPlan]:
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
            return ChildEntry(
                slug, info.get("name", slug), confidence(slug), reason,
                tuple(info.get("source_channels") or ()), info["person_id"],
            )

        confirmed = tuple(child(slug) for slug in members)
        records = [
            {"facts": facts_by_person.get(item.person_id, {})}
            for item in confirmed
            if facts_by_person.get(item.person_id)
        ]
        merged = merge_facts(records)
        name = merged.get("canonical_name") or confirmed[0].name
        parent_id = parent_id_for([item.person_id for item in confirmed])
        emails, phones = _identifiers(slugs_info, [item.slug for item in confirmed])
        plans.append(ParentPlan(
            parent_id, slugify(name, parent_id), name, tuple(emails), tuple(phones),
            confirmed, merged,
        ))
    return plans


def singleton_plan(child_slug: str, info: dict[str, Any]) -> ParentPlan:
    person_id = info["person_id"]
    name = info.get("name", child_slug)
    parent_id = parent_id_for([person_id])
    emails, phones = _identifiers({child_slug: info}, [child_slug])
    child = ChildEntry(
        child_slug, name, 0.0, "", tuple(info.get("source_channels") or ()), person_id,
    )
    return ParentPlan(
        parent_id, slugify(name, parent_id), name, tuple(emails), tuple(phones),
        (child,), {"headline": info.get("headline", "")},
    )

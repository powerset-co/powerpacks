"""Byte-stable parent dossier rendering."""
from __future__ import annotations

import json
from pathlib import Path

from packs.ingestion.primitives.deep_context.synthesis.facts import headline
from packs.ingestion.primitives.deep_context.synthesis.rendering import render_fact_sections, yaml_list
from packs.ingestion.primitives.deep_context.merge_candidates.models import ChildEntry, ParentPlan

def _child_line(child: ChildEntry) -> str:
    score = f" — judge {child.score:.2f}" if child.score else ""
    reason = f" ({child.reason})" if child.reason else ""
    channels = ", ".join(child.channels)
    return f"- [[{child.slug}]] **{child.name}**{score}{reason}  ·  {channels}"


def render_parent(plan: ParentPlan) -> str:
    merged = plan.merged
    lines = [
        "---",
        f"parent_id: {plan.parent_id}",
        f"name: {json.dumps(plan.name, ensure_ascii=False)}",
        f"slug: {plan.slug}",
        "kind: parent",
        f"children: {yaml_list([child.slug for child in plan.confirmed])}",
        "needs_review: []",
        f"emails: {yaml_list(list(plan.emails))}",
        f"phones: {yaml_list(list(plan.phones))}",
        f"confidence: {round(merged.confidence, 2)}",
        "---", "", f"# {plan.name}", "", "## Summary", "",
        headline(merged) or "_Merged from the confirmed records below._",
        "", "## Confirmed children (merged)", "",
        "_LLM-judged same person; their facts are merged into this profile._", "",
        *[_child_line(child) for child in plan.confirmed],
    ]
    relationship = merged.relationship_to_owner
    if relationship:
        lines += ["", "## Relationship & cadence", "", relationship]
    lines += render_fact_sections(
        merged, field_of_study=False, empty_status_is_unknown=False,
    )
    identifiers = [f"- {email}" for email in plan.emails] + [
        f"- {phone}" for phone in plan.phones
    ]
    if identifiers:
        lines += ["", "## Identifiers", "", *identifiers]
    return "\n".join(lines) + "\n"


def render_singleton(plan: ParentPlan) -> str:
    child_slug = plan.confirmed[0].slug
    lines = [
        "---", f"parent_id: {plan.parent_id}",
        f"name: {json.dumps(plan.name, ensure_ascii=False)}", f"slug: {plan.slug}",
        "kind: parent", "singleton: true",
        f"children: {yaml_list([child_slug])}",
        f"emails: {yaml_list(list(plan.emails))}",
        f"phones: {yaml_list(list(plan.phones))}",
        "---", "", f"# {plan.name}", "",
        f"Single identity — no duplicates detected. Full context in [[{child_slug}]].",
    ]
    return "\n".join(lines) + "\n"


def remove_orphans(parents_dir: Path, active_slugs: set[str]) -> int:
    removed = 0
    for path in parents_dir.glob("*.md"):
        if path.stem not in active_slugs:
            path.unlink()
            removed += 1
    return removed

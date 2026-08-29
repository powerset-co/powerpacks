"""Render parent dossiers from stage-local Jinja templates."""
from __future__ import annotations

import json
from pathlib import Path

from packs.ingestion.primitives.deep_context.synthesis.facts import headline
from packs.ingestion.primitives.deep_context.synthesis.rendering import render_fact_sections, yaml_list
from packs.ingestion.primitives.deep_context.merge_candidates.models import ParentPlan
from packs.ingestion.primitives.deep_context.shared.template_engine import template_environment

_TEMPLATES = template_environment(Path(__file__).with_name("templates"), html=False)


def render_parent(plan: ParentPlan) -> str:
    merged = plan.merged
    return _TEMPLATES.get_template("parent.md.j2").render(
        plan=plan,
        name_json=json.dumps(plan.name, ensure_ascii=False),
        children_yaml=yaml_list([child.slug for child in plan.confirmed]),
        emails_yaml=yaml_list(list(plan.emails)),
        phones_yaml=yaml_list(list(plan.phones)),
        confidence=round(merged.confidence, 2),
        summary=headline(merged) or "_Merged from the confirmed records below._",
        relationship=merged.relationship_to_owner,
        fact_sections=render_fact_sections(
            merged, field_of_study=False, empty_status_is_unknown=False,
        ),
        identifiers=(*plan.emails, *plan.phones),
    )


def render_singleton(plan: ParentPlan) -> str:
    child_slug = plan.confirmed[0].slug
    return _TEMPLATES.get_template("singleton.md.j2").render(
        plan=plan,
        child_slug=child_slug,
        name_json=json.dumps(plan.name, ensure_ascii=False),
        children_yaml=yaml_list([child_slug]),
        emails_yaml=yaml_list(list(plan.emails)),
        phones_yaml=yaml_list(list(plan.phones)),
    )


def remove_orphans(parents_dir: Path, active_slugs: set[str]) -> int:
    removed = 0
    for path in parents_dir.glob("*.md"):
        if path.stem not in active_slugs:
            path.unlink()
            removed += 1
    return removed

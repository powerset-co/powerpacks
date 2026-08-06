"""Byte-stable parent dossier rendering and anchored child annotations."""
from __future__ import annotations

import json
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.dossier.facts import headline
from packs.ingestion.primitives.deep_context.dossier.rendering import yaml_list
from packs.ingestion.primitives.deep_context.parents.models import ChildEntry, ParentPlan

PARENT_ANCHOR = "<!-- parent-link -->"


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
        f"confidence: {round(float(merged.get('confidence') or 0.0), 2)}",
        f"generated_at: {now_iso()}",
        "---", "", f"# {plan.name}", "", "## Summary", "",
        headline(merged) or "_Merged from the confirmed records below._",
        "", "## Confirmed children (merged)", "",
        "_LLM-judged same person; their facts are merged into this profile._", "",
        *[_child_line(child) for child in plan.confirmed],
    ]
    relationship = merged.get("relationship_to_owner")
    if relationship:
        lines += ["", "## Relationship & cadence", "", relationship]
    if merged.get("shared_context"):
        lines += ["", "## Shared context with you", ""]
        for context in merged["shared_context"]:
            evidence = f" — _{context['evidence']}_" if context.get("evidence") else ""
            lines.append(f"- **{context.get('overlap', 'other')}:** {context['detail']}{evidence}")
    identity = []
    if merged.get("title"):
        identity.append(f"- **Title:** {merged['title']}")
    for employer in merged.get("employers") or []:
        role = f" — {employer['role']}" if employer.get("role") else ""
        identity.append(
            f"- **Employer ({employer.get('status', 'unknown')}):** {employer['name']}{role}"
        )
    if merged.get("school"):
        identity.append(f"- **School:** {merged['school']}")
    if merged.get("location"):
        identity.append(f"- **Location:** {merged['location']}")
    if identity:
        lines += ["", "## Who they are", "", *identity]
    if merged.get("topics"):
        lines += ["", "## Topics", "", *(f"- {topic}" for topic in merged["topics"])]
    if merged.get("notable_events"):
        lines += ["", "## Timeline", ""]
        for event in merged["notable_events"]:
            lines.append(f"- **{event.get('date') or '?'}** — {event['summary']}")
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
        f"generated_at: {now_iso()}", "---", "", f"# {plan.name}", "",
        f"Single identity — no duplicates detected. Full context in [[{child_slug}]].",
    ]
    headline = plan.merged.get("headline") or ""
    if headline:
        lines += ["", headline]
    return "\n".join(lines) + "\n"


def inject_parent_backref(
    dossier_dir: Path, child_slug: str, parent_slug: str, parent_name: str,
) -> None:
    path = dossier_dir / f"{child_slug}.md"
    if not path.exists():
        return
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if PARENT_ANCHOR not in line
    ]
    for index, line in enumerate(lines):
        if line.startswith("# "):
            backref = (
                f"{PARENT_ANCHOR} _Part of [[{parent_slug}]] **{parent_name}** "
                "(proposed merge)_"
            )
            lines.insert(index + 1, "")
            lines.insert(index + 2, backref)
            break
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def remove_orphans(parents_dir: Path, active_slugs: set[str]) -> int:
    removed = 0
    for path in parents_dir.glob("*.md"):
        if path.stem not in active_slugs:
            path.unlink()
            removed += 1
    return removed

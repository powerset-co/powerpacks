"""Byte-stable dossier and human catalog rendering."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    contact_identifiers,
    load_owner,
    phone_digits,
    slugify,
)
from packs.ingestion.primitives.deep_context.dossier.facts import headline


def yaml_list(values: list[str]) -> str:
    return "[" + ", ".join(json.dumps(value, ensure_ascii=False) for value in values) + "]"


def render_dossier(
    meta: dict[str, Any], merged: dict[str, Any], depth: dict[str, Any] | None = None,
) -> str:
    name = merged.get("canonical_name") or meta.get("full_name") or "(unknown)"
    depth = depth or {}
    messages = meta.get("messages") or []
    last_at = max((message.get("at") or "" for message in messages), default="")
    lines = [
        "---", f"person_id: {meta.get('person_id')}",
        f"name: {json.dumps(name, ensure_ascii=False)}",
        f"slug: {slugify(name, str(meta.get('person_id')))}",
        f"emails: {yaml_list(meta.get('emails') or [])}",
        f"phones: {yaml_list(meta.get('phones') or [])}",
        f"source_channels: {yaml_list(meta.get('source_channels') or [])}",
        f"message_count: {len(messages)}",
        f"last_interaction: {json.dumps(last_at, ensure_ascii=False)}",
        f"confidence: {round(float(merged.get('confidence') or 0.0), 2)}",
        f"generated_at: {now_iso()}", "---", "", f"# {name}", "",
        "## Summary", "", headline(merged) or "_No summary yet._",
    ]
    worth = merged.get("network_worth") or {}
    if worth.get("decision"):
        reason = f" — {worth['reason']}" if worth.get("reason") else ""
        lines += ["", f"**Network worth:** {worth['decision']}{reason}"]
    relationship = merged.get("relationship_to_owner")
    if relationship:
        used = depth.get("messages_used", len(messages))
        available = depth.get("messages_available", len(messages))
        channels = ", ".join(meta.get("source_channels") or []) or "unknown channels"
        note = f"_grokked {used} of {available} messages"
        if depth.get("batches_used"):
            note += f" over {depth['batches_used']} batch(es)"
        note += f" across {channels}; last on {last_at[:10] or 'n/a'}"
        note += f" (stopped: {depth['stop_reason']})._" if depth.get("stop_reason") else "._"
        lines += ["", "## Relationship & cadence", "", relationship, "", note]

    shared = merged.get("shared_context") or []
    if shared:
        lines += ["", "## Shared context with you", ""]
        for context in shared:
            evidence = f" — _{context['evidence']}_" if context.get("evidence") else ""
            lines.append(f"- **{context.get('overlap', 'other')}:** {context['detail']}{evidence}")
    identity: list[str] = []
    if merged.get("title"):
        identity.append(f"- **Title:** {merged['title']}")
    for employer in merged.get("employers") or []:
        status = employer.get("status") or "unknown"
        role = f" — {employer['role']}" if employer.get("role") else ""
        identity.append(f"- **Employer ({status}):** {employer['name']}{role}")
    if merged.get("school"):
        field = f" ({merged['field_of_study']})" if merged.get("field_of_study") else ""
        identity.append(f"- **School:** {merged['school']}{field}")
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

    contact_values = [*(meta.get("emails") or []), *(meta.get("phones") or [])]
    known = {value.lower() for value in contact_values}
    known |= {phone_digits(value) for value in contact_values if phone_digits(value)}
    owner = load_owner() or {}
    identifiers = [
        identifier
        for identifier in contact_identifiers(
            merged.get("identifiers"),
            name=str(merged.get("canonical_name") or meta.get("name") or ""),
            known=contact_values,
            owner_emails=owner.get("emails") or [],
            owner_phones=owner.get("phones") or [],
        )
        if identifier.lower() not in known and phone_digits(identifier) not in known
    ]
    contact = [f"- {value}" for value in contact_values]
    if identifiers or contact:
        lines += ["", "## Identifiers", "", *contact,
                  *(f"- {identifier}" for identifier in identifiers)]
    lines += ["", "## Possible same person", "", "_None detected yet._", ""]
    return "\n".join(lines)


def write_catalog(path: Path, catalog: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Deep-context dossiers ({len(catalog)})", "", f"_Generated {now_iso()}._", ""]
    for name, summary, slug in sorted(catalog, key=lambda item: item[0].lower()):
        suffix = f" — {summary}" if summary else ""
        lines.append(f"- [[{slug}]] **{name}**{suffix}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

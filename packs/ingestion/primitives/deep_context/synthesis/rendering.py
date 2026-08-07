"""Byte-stable dossier and human catalog rendering."""
from __future__ import annotations

import json
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.shared.common import (
    contact_identifiers,
    phone_digits,
    slugify,
)
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.synthesis.facts import headline
from packs.ingestion.primitives.deep_context.synthesis.models import (
    DossierDepth,
    SynthesizedFacts,
)


def yaml_list(values: list[str]) -> str:
    return "[" + ", ".join(json.dumps(value, ensure_ascii=False) for value in values) + "]"


def render_fact_sections(
    merged: SynthesizedFacts, *, field_of_study: bool = True,
    empty_status_is_unknown: bool = True,
) -> list[str]:
    """Render fact sections shared by child and parent dossiers."""
    lines: list[str] = []
    if merged.shared_context:
        lines += ["", "## Shared context with you", ""]
        for context in merged.shared_context:
            evidence = f" — _{context.evidence}_" if context.evidence else ""
            lines.append(
                f"- **{context.overlap}:** {context.detail}{evidence}"
            )
    identity: list[str] = []
    if merged.title:
        identity.append(f"- **Title:** {merged.title}")
    for employer in merged.employers:
        status = (
            employer.status or "unknown"
            if empty_status_is_unknown
            else employer.status
        )
        role = f" — {employer.role}" if employer.role else ""
        identity.append(f"- **Employer ({status}):** {employer.name}{role}")
    if merged.school:
        field = (
            f" ({merged.field_of_study})"
            if field_of_study and merged.field_of_study
            else ""
        )
        identity.append(f"- **School:** {merged.school}{field}")
    if merged.location:
        identity.append(f"- **Location:** {merged.location}")
    if identity:
        lines += ["", "## Who they are", "", *identity]
    if merged.topics:
        lines += ["", "## Topics", "", *(f"- {topic}" for topic in merged.topics)]
    if merged.notable_events:
        lines += ["", "## Timeline", ""]
        for event in merged.notable_events:
            lines.append(f"- **{event.date or '?'}** — {event.summary}")
    return lines


def render_dossier(
    meta: CollectionBundle,
    merged: SynthesizedFacts,
    depth: DossierDepth | None = None,
    *, owner_emails: tuple[str, ...] = (), owner_phones: tuple[str, ...] = (),
) -> str:
    name = merged.canonical_name or meta.full_name or "(unknown)"
    messages = meta.messages
    last_at = max((message.at or "" for message in messages), default="")
    lines = [
        "---", f"person_id: {meta.person_id}",
        f"name: {json.dumps(name, ensure_ascii=False)}",
        f"slug: {slugify(name, meta.person_id)}",
        f"emails: {yaml_list(list(meta.emails))}",
        f"phones: {yaml_list(list(meta.phones))}",
        f"source_channels: {yaml_list(list(meta.source_channels))}",
        f"message_count: {len(messages)}",
        f"last_interaction: {json.dumps(last_at, ensure_ascii=False)}",
        f"confidence: {round(merged.confidence, 2)}",
        f"generated_at: {now_iso()}", "---", "", f"# {name}", "",
        "## Summary", "", headline(merged) or "_No summary yet._",
    ]
    worth = merged.network_worth
    if worth:
        reason = f" — {worth.reason}" if worth.reason else ""
        lines += ["", f"**Network worth:** {worth.decision}{reason}"]
    relationship = merged.relationship_to_owner
    if relationship:
        used = (
            depth.messages_used
            if depth and depth.messages_used is not None
            else len(messages)
        )
        available = (
            depth.messages_available
            if depth and depth.messages_available is not None
            else len(messages)
        )
        channels = ", ".join(meta.source_channels) or "unknown channels"
        note = f"_grokked {used} of {available} messages"
        if depth and depth.batches_used:
            note += f" over {depth.batches_used} batch(es)"
        note += f" across {channels}; last on {last_at[:10] or 'n/a'}"
        note += (
            f" (stopped: {depth.stop_reason})._"
            if depth and depth.stop_reason
            else "._"
        )
        lines += ["", "## Relationship & cadence", "", relationship, "", note]

    lines += render_fact_sections(merged)

    contact_values = [*meta.emails, *meta.phones]
    known = {value.lower() for value in contact_values}
    known |= {phone_digits(value) for value in contact_values if phone_digits(value)}
    identifiers = [
        identifier
        for identifier in contact_identifiers(
            merged.identifiers,
            name=merged.canonical_name or meta.full_name,
            known=contact_values,
            owner_emails=owner_emails,
            owner_phones=owner_phones,
        )
        if identifier.lower() not in known and phone_digits(identifier) not in known
    ]
    contact = [f"- {value}" for value in contact_values]
    if identifiers or contact:
        lines += ["", "## Identifiers", "", *contact,
                  *(f"- {identifier}" for identifier in identifiers)]
    return "\n".join(lines)


def write_catalog(path: Path, catalog: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Deep-context dossiers ({len(catalog)})", "", f"_Generated {now_iso()}._", ""]
    for name, summary, slug in sorted(catalog, key=lambda item: item[0].lower()):
        suffix = f" — {summary}" if summary else ""
        lines.append(f"- [[{slug}]] **{name}**{suffix}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

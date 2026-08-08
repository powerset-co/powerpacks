"""Render dossier and catalog documents from stage-local Jinja templates.

``render_dossier`` output looks like (frontmatter and later sections
abbreviated)::

    ---
    name: "Jordan Bravo"
    confidence: 0.82
    ...
    ---

    # Jordan Bravo

    ## Summary

    Product Manager at Acme Corp

    **Network worth:** yes — strong technical network

    ## Relationship & cadence

    Former colleague at Acme; stays in touch.

    _grokked 40 of 40 messages across gmail, imessage; last on 2026-07-01._

    ## Identifiers

    - jordan@example.com
    - +15550100
"""
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
from packs.ingestion.primitives.deep_context.shared.template_engine import template_environment
from packs.ingestion.primitives.deep_context.synthesis.facts import headline
from packs.ingestion.primitives.deep_context.synthesis.models import (
    DossierDepth,
    SynthesizedFacts,
)

_TEMPLATES = template_environment(Path(__file__).with_name("templates"), html=False)


def yaml_list(values: list[str]) -> str:
    """Hand-rolled YAML flow sequence, JSON-quoting each item for frontmatter."""
    return "[" + ", ".join(json.dumps(value, ensure_ascii=False) for value in values) + "]"


def render_fact_sections(
    merged: SynthesizedFacts, *, field_of_study: bool = True,
    empty_status_is_unknown: bool = True,
) -> str:
    """Render fact sections shared by child and parent dossiers.

    NOTE: ``empty_status_is_unknown=False`` (passed by
    merge_candidates/rendering.py) currently has no observable effect —
    EmployerFact.from_payload already defaults status to "unknown" before it
    ever reaches the template, so both sides of the template's ternary
    evaluate to the same non-empty value.
    """
    has_identity = bool(
        merged.title or merged.employers or merged.school or merged.location
    )
    return _TEMPLATES.get_template("fact_sections.md.j2").render(
        merged=merged,
        has_identity=has_identity,
        field_of_study=field_of_study,
        empty_status_is_unknown=empty_status_is_unknown,
    ).strip()


def render_dossier(
    meta: CollectionBundle,
    merged: SynthesizedFacts,
    depth: DossierDepth | None = None,
    *, owner_emails: tuple[str, ...] = (), owner_phones: tuple[str, ...] = (),
) -> str:
    name = merged.canonical_name or meta.full_name or "(unknown)"
    messages = meta.messages
    last_at = max((message.at or "" for message in messages), default="")
    worth = merged.network_worth
    relationship = merged.relationship_to_owner
    relationship_note = ""
    if relationship:
        # depth is only populated for a capped/multi-batch synthesis run;
        # a single-pass run has no DossierDepth record, so used/available
        # both fall back to the full message count.
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
        relationship_note = note

    contact_values = [*meta.emails, *meta.phones]
    known = {value.lower() for value in contact_values}
    known |= {phone_digits(value) for value in contact_values if phone_digits(value)}
    # merged.identifiers is free text the LLM proposed; contact_identifiers()
    # is the sanitizer — it drops anything not shaped like an email/phone and
    # caps phones at two. It also uses `known` as a *validity* signal (a known
    # email is let through even without a name-token match), which is why we
    # still need the exact-match filter below: without it, a value already
    # shown under the structural contact_values header would be repeated here.
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
    worth_line = ""
    if worth:
        reason = f" — {worth.reason}" if worth.reason else ""
        worth_line = f"**Network worth:** {worth.decision}{reason}"
    return _TEMPLATES.get_template("dossier.md.j2").render(
        meta=meta,
        name=name,
        name_json=json.dumps(name, ensure_ascii=False),
        slug=slugify(name, meta.person_id),
        emails_yaml=yaml_list(list(meta.emails)),
        phones_yaml=yaml_list(list(meta.phones)),
        channels_yaml=yaml_list(list(meta.source_channels)),
        message_count=len(messages),
        last_at_json=json.dumps(last_at, ensure_ascii=False),
        confidence=round(merged.confidence, 2),
        generated_at=now_iso(),
        summary=headline(merged) or "_No summary yet._",
        worth_line=worth_line,
        relationship=relationship,
        relationship_note=relationship_note,
        fact_sections=render_fact_sections(merged),
        identifier_lines=[*contact_values, *identifiers],
    ).rstrip("\n")


def write_catalog(path: Path, catalog: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = _TEMPLATES.get_template("catalog.md.j2").render(
        # Alphabetical, not relevance-ranked — chosen for a stable, diffable
        # index rather than to surface any particular person first.
        catalog=sorted(catalog, key=lambda item: item[0].lower()),
        generated_at=now_iso(),
    )
    path.write_text(rendered, encoding="utf-8")

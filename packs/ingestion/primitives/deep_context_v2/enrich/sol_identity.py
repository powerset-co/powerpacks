"""The Sol identity judge's prompt: one LinkedIn profile against one family, in the shape v1 asked.

The system prompt and the answer schema are v1's `linkedin_reconcile` pair, byte for byte. The user prompt
is the owner's background, the contact (written names, the dossier's fields, address-book handles, the
message count, the full facts), the LinkedIn profile, then a note on where the profile came from: the
owner's own first-degree connection (strong evidence, decided 2026-10-07) or a speculative research
find (needs a corroborating detail). The verdict is confirmed, wrong_person or needs_review.

Created: 2026-10-07
"""
from __future__ import annotations

import json

import jinja2
from pathlib import Path
from typing import Any

import jsonschema

from packs.ingestion.primitives.deep_context_v2.db.schema import Origin, Verdict
from packs.ingestion.primitives.deep_context_v2.enrich.family import Family, family_evidence
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import Profile
from packs.ingestion.primitives.deep_context_v2.openai import OpenAIResponsesCaller
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts

from packs.ingestion.primitives.deep_context_v2 import assets

_HERE = Path(__file__).parent
SYSTEM_PROMPT: str = assets.text(_HERE, "linkedin_reconcile_system.txt")
SCHEMA: dict[str, Any] = assets.json_file(_HERE, "linkedin_reconcile_schema.txt")
SCHEMA_NAME = "linkedin_reconcile"
MODEL = "gpt-6.1-sol"
REASONING_EFFORT = "medium"
CONNECTION_NOTE = ("\n\n*** This profile is one of MY OWN first-degree LinkedIn connections, matched to this contact "
                   "by name. Being connected is strong evidence that this is the person I message with: with a "
                   "compatible name and no hard contradiction, confirm. ***")
RESEARCH_NOTE = ("\n\nThis is a speculative web-research proposal. A shared name alone is not corroboration; "
                 "require employer, school, location, topic, domain, or equivalent evidence. Missing information "
                 "is not a contradiction. Interview or referral context does not prove employment; evaluate the dates.")


TEMPLATE: jinja2.Template = assets.template(_HERE, "identity_prompt.j2")


def identity_prompt(family: Family, profile: Profile, origin: str, citations: tuple[dict[str, Any], ...], owner_block: str) -> str:
    """One profile against the family, rendered from identity_prompt.j2: the owner's background, the
    contact (names, the dossier's fields, address-book handles, the full facts), the LinkedIn profile, and
    a note on where the profile came from."""
    facts: SynthesizedFacts = family.facts
    # The contact's fields, one line each, only the ones the dossier filled.
    fields: list[str] = []
    if facts.relationship_to_owner:
        fields.append(f"relationship: {facts.relationship_to_owner}")
    employers: list[str] = []
    for employer in facts.employers:
        if employer.name:
            employers.append(employer.name)
    if facts.title or employers:
        fields.append(f"work: {facts.title} @ {', '.join(employers)}".rstrip(" @"))
    if facts.school:
        fields.append(f"school: {facts.school}")
    if facts.location:
        fields.append(f"location: {facts.location}")
    if facts.topics:
        fields.append("topics: " + ", ".join(facts.topics))
    shared: list[str] = []
    for item in facts.shared_context:
        shared.append(item.detail)
    if shared:
        fields.append("shared context: " + "; ".join(shared))
    evidence_fields: dict[str, Any] = family_evidence(family)
    handles: list[str] = evidence_fields["emails"] + evidence_fields["phones"]
    if handles:
        fields.append("my address-book contact handles for them: " + ", ".join(handles))
        if origin == Origin.RESEARCH:
            fields.append("(a work-email domain is strong evidence only when an independent source ties that employer "
                          "to the proposed person; copied contact facts are not corroboration)")
        else:
            fields.append("(a work-email DOMAIN matching the profile's employer is strong identity proof)")
    view: dict[str, Any] = profile.judge_view()
    citations_json: str = ""
    if citations:
        citations_json = json.dumps(list(citations), ensure_ascii=False)
    return TEMPLATE.render(
        owner_block=owner_block, names=list(family.names), fields=fields, messages=family.messages,
        facts_json=json.dumps(facts.to_payload(), ensure_ascii=False), url=profile.linkedin_url,
        name=view["full_name"], headline=view["headline"], location=view["location"],
        experiences=view["experiences"], education=view["education"],
        own_connection=origin == Origin.LINKEDIN_NETWORK, citations=citations_json,
    )


async def verdict(caller: OpenAIResponsesCaller, prompt: str) -> str:
    """One Sol call: the verdict for one profile."""
    answer: dict[str, Any] = await caller.call(system_prompt=SYSTEM_PROMPT, user_prompt=prompt, schema=SCHEMA,
                                               schema_name=SCHEMA_NAME, context="enrich-judge")
    jsonschema.validate(answer, SCHEMA)
    return Verdict(answer["verdict"]).value

"""Sol judges LinkedIn and cited research with the same identity standard."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jinja2

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
TEMPLATE: jinja2.Template = assets.template(_HERE, "identity_prompt.j2")


@dataclass(frozen=True)
class SolVerdict:
    """One Sol answer: verdict, probability of the same person, and supporting reason."""

    verdict: str
    match_probability: float
    reason: str


def identity_prompt(family: Family, profile: Profile, origin: str, citations: tuple[dict[str, Any], ...], owner_block: str,
                    research_result: dict[str, Any] | None = None) -> str:
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
        if origin in (Origin.RESEARCH, Origin.SYNTHETIC):
            fields.append("(a work-email domain is strong evidence only when an independent source ties that employer "
                          "to the proposed person; copied contact facts are not corroboration)")
        else:
            fields.append("(a work-email DOMAIN matching the profile's employer is strong identity proof)")
    view: dict[str, Any] = profile.judge_view()
    citations_json: str = ""
    if citations:
        citations_json = json.dumps(list(citations), ensure_ascii=False)
    prompt = TEMPLATE.render(
        owner_block=owner_block, names=list(family.names), fields=fields, messages=family.messages,
        facts_json=json.dumps(facts.to_payload(), ensure_ascii=False), url=profile.linkedin_url,
        name=view["full_name"], headline=view["headline"], location=view["location"],
        experiences=view["experiences"], education=view["education"],
        own_connection=origin == Origin.LINKEDIN_NETWORK, citations=citations_json,
    )

    if research_result is not None:
        prompt += "\nPROPOSED WEB RESEARCH (content and source provenance):\n" + json.dumps(research_result, ensure_ascii=False)
    return prompt


async def verdict(caller: OpenAIResponsesCaller, prompt: str) -> SolVerdict:
    """One Sol call: the verdict for one profile."""
    answer: dict[str, Any] = await caller.call(system_prompt=SYSTEM_PROMPT, user_prompt=prompt, schema=SCHEMA,
                                               schema_name=SCHEMA_NAME, context="enrich-judge")
    jsonschema.validate(answer, SCHEMA)
    return SolVerdict(Verdict(answer["verdict"]).value, float(answer["match_probability"]), answer["reason"])

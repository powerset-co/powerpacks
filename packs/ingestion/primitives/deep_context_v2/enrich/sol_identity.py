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
from pathlib import Path
from typing import Any

import jsonschema

from packs.ingestion.primitives.deep_context_v2.db.schema import Origin, Verdict
from packs.ingestion.primitives.deep_context_v2.enrich.family import Family, family_evidence
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import Profile
from packs.ingestion.primitives.deep_context_v2.openai import OpenAIResponsesCaller
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts

_HERE = Path(__file__).parent
SYSTEM_PROMPT: str = (_HERE / "linkedin_reconcile_system.txt").read_text(encoding="utf-8").removesuffix("\n")
SCHEMA: dict[str, Any] = json.loads((_HERE / "linkedin_reconcile_schema.txt").read_text(encoding="utf-8"))
SCHEMA_NAME = "linkedin_reconcile"
MODEL = "gpt-6.1-sol"
REASONING_EFFORT = "medium"
CONNECTION_NOTE = ("\n\n*** This profile is one of MY OWN first-degree LinkedIn connections, matched to this contact "
                   "by name. Being connected is strong evidence that this is the person I message with: with a "
                   "compatible name and no hard contradiction, confirm. ***")
RESEARCH_NOTE = ("\n\nThis is a speculative web-research proposal. A shared name alone is not corroboration; "
                 "require employer, school, location, topic, domain, or equivalent evidence. Missing information "
                 "is not a contradiction. Interview or referral context does not prove employment; evaluate the dates.")


def _bullets(items: list[str], empty: str) -> str:
    if not items:
        return "  " + empty
    lines: list[str] = []
    for item in items:
        lines.append("  - " + item)
    return "\n".join(lines)


def identity_prompt(family: Family, profile: Profile, origin: str, citations: tuple[dict[str, Any], ...], owner_block: str) -> str:
    """One profile against the family, in the identity judge's shape: the owner's background, the contact
    (names, facts, handles), the LinkedIn profile, then a note on where the profile came from."""
    facts: SynthesizedFacts = family.facts
    fields: list[str] = []
    if facts.relationship_to_owner:
        fields.append(f"relationship: {facts.relationship_to_owner}")
    employers: list[str] = []
    for employer in facts.employers:
        if employer.name:
            employers.append(employer.name)
    if facts.title or employers:
        fields.append(f"work: {facts.title} @ {', '.join(employers)}".replace(" @ ", " @ ").rstrip(" @"))
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
    indented: list[str] = []
    for line in fields:
        indented.append("  " + line)
    text: str = owner_block + "\n" + "CONTACT: " + " / ".join(family.names) + "\n" + "\n".join(indented)
    text += f"\n  messages exchanged: {family.messages}"
    text += "\n\nFULL DOSSIER FACTS (synthesized from messages):\n" + json.dumps(facts.to_payload(), ensure_ascii=False)
    view: dict[str, Any] = profile.judge_view()
    text += (f"\n\nLINKEDIN: {profile.linkedin_url}"
             f"\n  name: {view['full_name'] or '(unknown)'}"
             f"\n  headline: {view['headline'] or '(none)'}"
             f"\n  location: {view['location'] or '(unknown)'}"
             f"\n  experience:\n{_bullets(view['experiences'], '(none)')}"
             f"\n  education:\n{_bullets(view['education'], '(none)')}")
    if origin == Origin.LINKEDIN_NETWORK:
        text += CONNECTION_NOTE
    else:
        text += RESEARCH_NOTE
        if citations:
            text += "\nResearch source citations (URLs, titles and excerpts): " + json.dumps(list(citations), ensure_ascii=False)
    return text + "\n\nIs this the same human?"


def prompts(family: Family, candidates: tuple, citations: tuple[dict[str, Any], ...], owner_block: str) -> list[str]:
    """One prompt per proposed profile, for the dry run's token count."""
    rendered: list[str] = []
    for candidate in candidates:
        rendered.append(identity_prompt(family, candidate.profile, candidate.origin, citations, owner_block))
    return rendered


async def verdict(caller: OpenAIResponsesCaller, prompt: str) -> str:
    """One Sol call: the verdict for one profile."""
    answer: dict[str, Any] = await caller.call(system_prompt=SYSTEM_PROMPT, user_prompt=prompt, schema=SCHEMA,
                                               schema_name=SCHEMA_NAME, context="enrich-judge")
    jsonschema.validate(answer, SCHEMA)
    return Verdict(answer["verdict"]).value

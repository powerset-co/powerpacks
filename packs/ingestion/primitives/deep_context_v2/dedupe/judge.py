"""The Sol pair judge: render one pair as a prompt, ask gpt-6.1-sol, read the answer.

The prompt shows the network owner, then each side (its name and email, its facts as relationship /
work / school / location / topics lines, and up to four short messages each way), then both sides'
written names and phones, then the question. The answer is same, different or uncertain; same or
different must name the individual identity evidence behind it.

Created: 2026-10-06
"""
from __future__ import annotations

import json

import jinja2
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from packs.ingestion.primitives.deep_context_v2.collect.bundle import CollectionBundle, MessageDirection, MessageEntry
from packs.ingestion.primitives.deep_context_v2.db.queries_dedupe import Identifier
from packs.ingestion.primitives.deep_context_v2.db.schema import IdentifierKind
from packs.ingestion.primitives.deep_context_v2.openai import OpenAIResponsesCaller
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts

from packs.ingestion.primitives.deep_context_v2 import assets

_HERE = Path(__file__).parent
SYSTEM_PROMPT: str = assets.text(_HERE, "identity_merge_system.txt")
SCHEMA: dict[str, Any] = assets.json_file(_HERE, "identity_merge_schema.txt")
SCHEMA_NAME = "identity_merge"
MODEL = "gpt-6.1-sol"
REASONING_EFFORT = "high"
PROMPT_VERSION = "dedupe-2026-10-07-aliases"  # recorded in each verdict's signature; a pair is never judged again
SAMPLE_MESSAGES = 4      # per direction, newest first
SAMPLE_CHARS = 200       # per message
TOPICS = 10


@dataclass(frozen=True)
class SolSide:
    """One candidate as the judge sees it."""

    display_name: str
    identifiers: tuple[Identifier, ...]
    facts: SynthesizedFacts
    messages: tuple[MessageEntry, ...]


@dataclass(frozen=True)
class SolDecision:
    same_person: int | None  # 1 same, 0 different, None uncertain
    confidence: float
    reason: str


def _sample(messages: tuple[MessageEntry, ...], direction: MessageDirection) -> list[str]:
    """Newest first, the first SAMPLE_CHARS of each non-empty message one way, at most SAMPLE_MESSAGES."""
    ordered: list[MessageEntry] = sorted(messages, key=lambda message: message.at, reverse=True)
    texts: list[str] = []
    for message in ordered:
        text: str = message.text.strip()
        if message.direction != direction or not text:
            continue
        texts.append(text[:SAMPLE_CHARS])
        if len(texts) == SAMPLE_MESSAGES:
            break
    return texts


def _values(side: SolSide, kind: IdentifierKind) -> list[str]:
    values: list[str] = []
    for identifier in side.identifiers:
        if identifier.kind == kind:
            values.append(identifier.normalized_value)
    return values


TEMPLATE: jinja2.Template = assets.template(_HERE, "pair_prompt.j2")


def _side_fields(side: SolSide) -> dict[str, Any]:
    """One side as the template reads it: the fact lines the dossier filled, the samples, the handles."""
    facts: SynthesizedFacts = side.facts
    lines: list[str] = []
    # The name the dossier settled on, and its aliases: the written name may be a first name or a handle.
    if facts.canonical_name:
        lines.append(f"dossier name: {facts.canonical_name}")
    if facts.aliases:
        lines.append("also known as: " + ", ".join(facts.aliases))
    if facts.relationship_to_owner:
        lines.append(f"relationship: {facts.relationship_to_owner}")
    employers: list[str] = []
    for employer in facts.employers:
        if employer.name:
            employers.append(employer.name)
    if facts.title or employers:
        at: str = ""
        if employers:
            at = "@ " + ", ".join(employers)
        lines.append(f"work: {facts.title} {at}".strip())
    if facts.school:
        lines.append(f"school: {facts.school}")
    if facts.location:
        lines.append(f"location: {facts.location}")
    if facts.topics:
        lines.append("we discuss: " + ", ".join(facts.topics[:TOPICS]))
    return {
        "display_name": side.display_name, "emails": _values(side, IdentifierKind.EMAIL), "lines": lines,
        "from_me": _sample(side.messages, MessageDirection.FROM_ME),
        "from_them": _sample(side.messages, MessageDirection.FROM_THEM),
        "names_json": json.dumps([side.display_name], ensure_ascii=False),
        "phones_json": json.dumps(_values(side, IdentifierKind.PHONE)),
    }


def user_prompt(owner_name: str, first: SolSide, second: SolSide) -> str:
    """The whole user prompt for one pair, rendered from pair_prompt.j2."""
    return TEMPLATE.render(owner_name=owner_name, a=_side_fields(first), b=_side_fields(second))


def decision_from_answer(answer: dict[str, Any]) -> SolDecision:
    """Validate the answer at the provider boundary. Uncertain is not different. A same or different
    with no identity evidence behind it is saved as uncertain: the answer was paid for and is kept,
    but it does not merge or block anyone."""
    jsonschema.validate(answer, SCHEMA)
    evidence: str = answer["identity_evidence"].strip()
    same: int | None = None
    if answer["decision"] == "same" and evidence:
        same = 1
    elif answer["decision"] == "different" and evidence:
        same = 0
    reason: str = answer["reason"]
    if evidence:
        reason = reason + " Individual identity evidence: " + evidence
    return SolDecision(same, answer["confidence"], reason)


async def judge(caller: OpenAIResponsesCaller, prompt: str) -> SolDecision:
    """One Sol call for one pair."""
    answer: dict[str, Any] = await caller.call(system_prompt=SYSTEM_PROMPT, user_prompt=prompt, schema=SCHEMA,
                                               schema_name=SCHEMA_NAME, context="dedupe")
    return decision_from_answer(answer)


def side_of(display_name: str, identifiers: list[Identifier], facts_json: str, bundle_json: str) -> SolSide:
    """A Side from the stored rows."""
    facts: SynthesizedFacts = SynthesizedFacts.from_payload(json.loads(facts_json))
    bundle: CollectionBundle = CollectionBundle.from_payload(json.loads(bundle_json))
    return SolSide(display_name, tuple(identifiers), facts, bundle.messages)

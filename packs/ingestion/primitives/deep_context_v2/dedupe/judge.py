"""The Sol pair judge: render one pair as a prompt, ask gpt-6.1-sol, read the answer.

The prompt shows the network owner, then each side (its name and email, its facts as relationship /
work / school / location / topics lines, and up to four short messages each way), then both sides'
written names and phones, then the question. The answer is same, different or uncertain; same or
different must name the individual identity evidence behind it.

Created: 2026-10-06
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from packs.ingestion.primitives.deep_context_v2.collect.bundle import CollectionBundle, MessageDirection, MessageEntry
from packs.ingestion.primitives.deep_context_v2.db.queries_dedupe import Identifier
from packs.ingestion.primitives.deep_context_v2.db.schema import IdentifierKind
from packs.ingestion.primitives.deep_context_v2.openai import OpenAIResponsesCaller
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts

_HERE = Path(__file__).parent
SYSTEM_PROMPT: str = (_HERE / "identity_merge_system.txt").read_text(encoding="utf-8").removesuffix("\n")
SCHEMA: dict[str, Any] = json.loads((_HERE / "identity_merge_schema.txt").read_text(encoding="utf-8"))
SCHEMA_NAME = "identity_merge"
MODEL = "gpt-6.1-sol"
REASONING_EFFORT = "high"
PROMPT_VERSION = "dedupe-2026-10-07-aliases"  # recorded in each verdict's signature; a pair is never judged again
SAMPLE_MESSAGES = 4      # per direction, newest first
SAMPLE_CHARS = 200       # per message
TOPICS = 10
QUESTION = "Are A and B the same person, different people, or uncertain?"


@dataclass(frozen=True)
class Side:
    """One candidate as the judge sees it."""

    display_name: str
    identifiers: tuple[Identifier, ...]
    facts: SynthesizedFacts
    messages: tuple[MessageEntry, ...]


@dataclass(frozen=True)
class Decision:
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


def _values(side: Side, kind: IdentifierKind) -> list[str]:
    values: list[str] = []
    for identifier in side.identifiers:
        if identifier.kind == kind:
            values.append(identifier.normalized_value)
    return values


def render_side(label: str, side: Side) -> str:
    """One CONTACT block: header with the email, the fact lines, then the message samples."""
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
    facts_block: str = "  (no extracted facts)"
    if lines:
        indented: list[str] = []
        for line in lines:
            indented.append("  " + line)
        facts_block = "\n".join(indented)
    # A few short messages each way.
    mine: str = "  (no messages from me)"
    from_me: list[str] = _sample(side.messages, MessageDirection.FROM_ME)
    if from_me:
        quoted: list[str] = []
        for text in from_me:
            quoted.append("  me→them: " + text)
        mine = "\n".join(quoted)
    theirs: str = "  (no messages from them)"
    from_them: list[str] = _sample(side.messages, MessageDirection.FROM_THEM)
    if from_them:
        quoted = []
        for text in from_them:
            quoted.append("  them→me: " + text)
        theirs = "\n".join(quoted)
    emails: str = "none"
    addresses: list[str] = _values(side, IdentifierKind.EMAIL)
    if addresses:
        emails = ", ".join(addresses)
    return f"CONTACT {label} — {side.display_name}  [emails: {emails}]\n{facts_block}\nMessages:\n{mine}\n{theirs}"


def user_prompt(owner_name: str, first: Side, second: Side) -> str:
    """The whole user prompt for one pair."""
    names: str = ("ORIGINAL SOURCE CONTACT NAMES:\nA: " + json.dumps([first.display_name], ensure_ascii=False)
                  + "\nB: " + json.dumps([second.display_name], ensure_ascii=False))
    phones: str = ("SOURCE CONTACT PHONES:\nA: " + json.dumps(_values(first, IdentifierKind.PHONE))
                   + "\nB: " + json.dumps(_values(second, IdentifierKind.PHONE)))
    return (f"Network owner: {owner_name}\n\n{render_side('A', first)}\n\n{render_side('B', second)}"
            f"\n\n{names}\n\n{phones}\n\n{QUESTION}")


def decision_from_answer(answer: dict[str, Any]) -> Decision:
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
    return Decision(same, answer["confidence"], reason)


async def judge(caller: OpenAIResponsesCaller, prompt: str) -> Decision:
    """One Sol call for one pair."""
    answer: dict[str, Any] = await caller.call(system_prompt=SYSTEM_PROMPT, user_prompt=prompt, schema=SCHEMA,
                                               schema_name=SCHEMA_NAME, context="dedupe")
    return decision_from_answer(answer)


def side_of(display_name: str, identifiers: list[Identifier], facts_json: str, bundle_json: str) -> Side:
    """A Side from the stored rows."""
    facts: SynthesizedFacts = SynthesizedFacts.from_payload(json.loads(facts_json))
    bundle: CollectionBundle = CollectionBundle.from_payload(json.loads(bundle_json))
    return Side(display_name, tuple(identifiers), facts, bundle.messages)

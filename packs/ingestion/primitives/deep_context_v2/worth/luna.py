"""Luna's worth and label request for one family.

The questions read the family's raw messages, profile and message counts. Worth is not a
judgment of the person: four detectors (worth_questions.json) each answer with a probability
whether the contact is a broadcast sender, a shared mailbox, a vendor the owner only buys from,
or someone acting for another person. Any detector at NO_THRESHOLD or above is worth no;
otherwise yes (worth.py). The reason names the detector that fired (reason.py). Answers are
cached by REQUEST_VERSION and the exact request.

Changelog:
  2026-10-28: worth = detectors, yes unless one fires; no maybe; no relationship or standing questions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from packs.ingestion.primitives.deep_context_v2 import assets
from packs.ingestion.primitives.deep_context_v2.collect.bundle import CollectionBundle, MessageDirection
from packs.ingestion.primitives.deep_context_v2.db.owner import OwnerProfile
from packs.ingestion.primitives.deep_context_v2.db.schema import SourceChannel
from packs.ingestion.primitives.deep_context_v2.decisions import MODEL, Answer
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts
from packs.ingestion.primitives.deep_context_v2.synthesize.prompt import batches, render_batch
from packs.ingestion.primitives.deep_context_v2.synthesize.synthesize import CHUNK_CHARS, MAX_BATCHES
from packs.ingestion.primitives.share.questions import (
    CHOICE_LABELS, EVIDENCE_POLICY, SCORE_LABELS, label_questions,
)

# A bump asks every family again.
REQUEST_VERSION = "deep-context-luna-detectors-20261028"

_HERE = Path(__file__).parent
WORTH_QUESTIONS: list[dict[str, Any]] = assets.json_file(_HERE, "worth_questions.json")
DETECTORS: tuple[str, ...] = tuple(question["name"] for question in WORTH_QUESTIONS)
# A detector at this probability or above makes the family worth no (the lab on a real store:
# true mailbox 1.00, assistant .92, vendor .81; the one false signal on a kept person was .50).
NO_THRESHOLD = 0.75
# Below the threshold but above this, the reason still names the closest call.
NEAR_MISS = 0.5
EMAIL_SOURCES = frozenset((SourceChannel.GMAIL.value,))

# A notable position is worth yes regardless of messages. "Vice President" is not "president".
NOTABLE_POSITION_RE = re.compile(
    r"\b(?:ceo|cto|coo|cfo|cio|cmo|cpo|cro|chro|cso|cdo|chief\s+(?:[a-z]+\s+){0,3}officer|"
    r"co-?founder|founder|(?<!vice )(?<!vice-)president|chair(?:man|woman)?|"
    r"general\s+partner|managing\s+partner|managing\s+director|partner)\b",
    re.IGNORECASE,
)

Labels = dict[str, str | float]  # predicate -> p; choice -> value, plus <choice>_p; score -> level;
# as worth saves them, "version" -> the REQUEST_VERSION that asked


@dataclass(frozen=True)
class ChannelSummary:
    """The family's messages as counts: no text."""

    sources: tuple[str, ...]
    interaction_counts: dict[str, int]
    first_message_at: str | None  # None = no message carried a timestamp
    last_message_at: str | None
    from_me: int
    from_them: int
    group_count: int


# ---- request 1: the labels and worth


def build_request(facts: SynthesizedFacts, bundles: Sequence[CollectionBundle], channels: ChannelSummary,
                  owner: OwnerProfile, reference_date: str) -> dict[str, Any]:
    """Request 1: the family's raw messages, profile, counts and the owner's background.

    `reference_date` is when the facts were synthesized, so the request changes only with its evidence.
    """
    family = CollectionBundle(
        person_id=bundles[0].person_id,
        full_name=facts.canonical_name,
        emails=tuple(dict.fromkeys(email for bundle in bundles for email in bundle.emails)),
        phones=tuple(dict.fromkeys(phone for bundle in bundles for phone in bundle.phones)),
        source_channels=tuple(dict.fromkeys(source for bundle in bundles for source in bundle.source_channels)),
        groups=tuple(dict.fromkeys(group for bundle in bundles for group in bundle.groups)),
        thread_participants=tuple(thread for bundle in bundles for thread in bundle.thread_participants),
        messages=tuple(message for bundle in bundles for message in bundle.messages),
        messages_available=sum(bundle.messages_available for bundle in bundles),
        capped=any(bundle.capped for bundle in bundles),
    )
    direction_labels = {
        MessageDirection.FROM_ME: f"from {owner.name} (owner) to {facts.canonical_name}",
        MessageDirection.FROM_THEM: f"from {facts.canonical_name} to {owner.name} (owner)",
        MessageDirection.FROM_OTHER: f"from another group participant to {owner.name} (owner) and {facts.canonical_name}",
    }
    messages = []
    for batch in batches(family.messages, chunk_chars=CHUNK_CHARS, max_batches=MAX_BATCHES):
        messages.append(render_batch(family, batch, direction_labels=direction_labels))
    company: str | None = None
    for employer in facts.employers:
        if employer.status == "current":
            company = employer.name
            break
    work: list[dict[str, str]] = []
    for job in owner.work:
        work.append(asdict(job))
    education: list[dict[str, str]] = []
    for school in owner.education:
        education.append(asdict(school))
    state: dict[str, Any] = {
        "messages": "\n\n".join(messages),
        "profile": {"name": facts.canonical_name, "title": facts.title, "company": company,
                    "location": facts.location, "headline": None},
        "channels": {
            "source_channels": list(channels.sources),
            "interaction_counts": channels.interaction_counts,
            "first_message_at": channels.first_message_at,
            "last_message_at": channels.last_message_at,
            "from_me": channels.from_me,
            "from_them": channels.from_them,
            "group_count": channels.group_count,
        },
        "owner": {"name": owner.name, "work": work, "education": education, "locations": list(owner.locations)},
        "reference_date": reference_date,
        "evidence_policy": EVIDENCE_POLICY,
    }
    worth: list[dict[str, Any]] = copy.deepcopy(WORTH_QUESTIONS)
    # One cache partition per owner: the owner's families share nothing with another install's.
    cache_key: str = "powerpacks-worth-" + hashlib.sha256(owner.name.encode("utf-8")).hexdigest()[:16]
    return {"model": MODEL, "input": json.dumps(state, ensure_ascii=False), "questions": label_questions() + worth,
            "prompt_cache_key": cache_key}


def refused(answers: list[Answer]) -> bool:
    """A refused choice or score leaves the family unlabelled: the answer is not saved and the family is
    asked again next run. A refused yes/no question only leaves that label out (Luna declines is_minor for
    some); a refused detector is a detector that did not fire."""
    asked: set[str] = {name for name in CHOICE_LABELS + SCORE_LABELS}
    return any(answer["type"] == "refusal" and answer["name"] in asked for answer in answers)


def labels(answers: list[Answer]) -> Labels:
    """predicate -> its probability; choice -> the chosen value, and its probability as <name>_p;
    score -> the expected level (2.7, not 3). A refused yes/no question has no label."""
    result: Labels = {}
    for answer in answers:
        name: str = answer["name"]
        if answer["type"] == "refusal":
            continue
        if answer["type"] == "predicate":
            result[name] = answer["probability"]
        elif answer["type"] == "score":
            result[name] = round(answer["score"], 2)
        else:
            result[name] = answer["choice"]
            result[name + "_p"] = next(option["probability"] for option in answer["probabilities"]
                                       if option["value"] == answer["choice"])
    return result

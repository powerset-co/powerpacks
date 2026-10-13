"""Luna's two requests for one family, and what their answers mean.

Request 1 asks the share-label questions (share/questions.py) and worth's own over the family's
facts and message counts: worth is the `worth` choice, its reason comes from worth's yes/no
answers (reason.py), and every answer becomes a label. Request 2 asks the share question over those
labels and their confidences. Both go to OpenAI's Decisions API (deep_context_v2/decisions.py),
cached on disk by REQUEST_VERSION and the exact request.

Created: 2026-10-06
Changelog:
  2026-10-13: Luna Decisions replace JEV and its frozen worth model. The dossier left the request
    (it repeated the facts line by line). Request 2, the share call, is new.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context_v2 import assets
from packs.ingestion.primitives.deep_context_v2.db.owner import OwnerProfile
from packs.ingestion.primitives.deep_context_v2.db.schema import SourceChannel
from packs.ingestion.primitives.deep_context_v2.decisions import MODEL, Answer
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts
from packs.ingestion.primitives.share.questions import EVIDENCE_POLICY, SHARE_QUESTION, label_questions

# Both requests' questions: a bump asks every family again.
REQUEST_VERSION = "deep-context-luna-decisions-20261013"

_HERE = Path(__file__).parent
WORTH_QUESTIONS: list[dict[str, Any]] = assets.json_file(_HERE, "worth_questions.json")
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


def _channel_policy(sources: tuple[str, ...]) -> str:
    """Email and phone contacts are held to different bars; a family with both gets the generous one."""
    email: bool = False
    phone: bool = False
    for source in sources:
        if source in EMAIL_SOURCES:
            email = True
        else:
            phone = True
    if email and phone:
        rule = ("These facts come from both email and phone messages. Bias toward yes when either channel "
                "shows a genuine human relationship; automated noise in one channel must not erase real "
                "correspondence in the other. Use maybe only when both channels remain genuinely ambiguous.")
    elif email:
        rule = ("These facts come from email. Bias toward yes for clearly human, person-directed correspondence, "
                "including sparse, one-off, old, academic, or plausibly important professional contacts. Use no only "
                "for clear automated mail, broadcast/transactional noise, or unengaged cold spam. Maybe should be rare.")
    else:
        rule = ("These facts come from phone messages. Repeated or clearly two-way personal or professional "
                "conversation is yes. Sparse context, a bare number, or an uncertain one-sided exchange may be maybe; "
                "automated service traffic or obvious spam is no. A name or area code is weak context only.")
    return "\n\nWORTH SOURCE POLICY:\n" + rule


def build_request(facts: SynthesizedFacts, channels: ChannelSummary, owner: OwnerProfile, reference_date: str) -> dict[str, Any]:
    """Request 1: the family's facts (minus the identifiers it owns), its message counts and the owner's
    background as JSON, with the label questions and worth's. `reference_date` is when the facts were
    synthesized, so the request changes only with its evidence."""
    payload: dict[str, Any] = facts.to_payload()
    del payload["owned_identifiers"]
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
        "facts": payload,
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
    worth[0]["instructions"] += _channel_policy(channels.sources)
    return {"model": MODEL, "input": json.dumps(state, ensure_ascii=False), "questions": label_questions() + worth}


def refused(answers: list[Answer]) -> bool:
    """A refusal leaves the family unlabelled: it stays pending and is asked again next run."""
    return any(answer["type"] == "refusal" for answer in answers)


def labels(answers: list[Answer]) -> Labels:
    """predicate -> its probability; choice -> the chosen value, and its probability as <name>_p;
    score -> the expected level (2.7, not 3)."""
    result: Labels = {}
    for answer in answers:
        name: str = answer["name"]
        if answer["type"] == "predicate":
            result[name] = answer["probability"]
        elif answer["type"] == "score":
            result[name] = round(answer["score"], 2)
        else:
            result[name] = answer["choice"]
            result[name + "_p"] = next(option["probability"] for option in answer["probabilities"]
                                       if option["value"] == answer["choice"])
    return result


# ---- request 2: the share call


def share_request(labels: Labels) -> dict[str, Any]:
    """Request 2: request 1's answers as lines of text, each with its confidence."""
    lines: list[str] = []
    for name, value in labels.items():
        if name.endswith("_p"):
            continue
        if isinstance(value, str):
            lines.append(f"{name}: {value} ({labels[name + '_p']:.2f})")
        elif name == "warmth":
            lines.append(f"warmth: {value:.1f} of 4")
        else:
            lines.append(f"{name}: {value:.2f}")
    return {"model": MODEL, "input": "\n".join(lines), "questions": [SHARE_QUESTION]}

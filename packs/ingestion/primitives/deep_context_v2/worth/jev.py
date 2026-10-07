"""The JEV worth-and-labels request for one family, and what its answers mean.

One request asks the 34 share-label questions and the 7 worth questions together over the family's
facts and message counts. A frozen multinomial logistic model maps the answers to yes, maybe or no;
the answers that pushed hardest toward that verdict become the reason; the answers themselves are
the share labels. Requests are cached on disk by their exact content, so an unchanged request is
never paid twice.

Created: 2026-10-06
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import tiktoken

from packs.ingestion.primitives.deep_context_v2.db.owner import OwnerProfile
from packs.ingestion.primitives.deep_context_v2.db.schema import SourceChannel
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts
from packs.ingestion.primitives.share.questions import build_questions as share_questions
from packs.ingestion.primitives.share.questions import build_request as share_request
from packs.search.primitives.llm_rerank_candidates.jev.client import (
    INPUT_PRICE_PER_MILLION, MAX_CONCURRENCY, answer_requests, cache_path, request_digest,
)

REQUEST_VERSION = "deep-context-worth-labels-family-20261006"
_HERE = Path(__file__).parent
WORTH_QUESTIONS: dict[str, dict[str, Any]] = json.loads((_HERE / "worth_questions.json").read_text())
EMAIL_SOURCES = frozenset((SourceChannel.GMAIL.value,))

# A notable position is worth yes regardless of messages. "Vice President" is not "president".
NOTABLE_POSITION_RE = re.compile(
    r"\b(?:ceo|cto|coo|cfo|cio|cmo|cpo|cro|chro|cso|cdo|chief\s+(?:[a-z]+\s+){0,3}officer|"
    r"co-?founder|founder|(?<!vice )(?<!vice-)president|chair(?:man|woman)?|"
    r"general\s+partner|managing\s+partner|managing\s+director|partner)\b",
    re.IGNORECASE,
)


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


# ---- the request


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
        rule = ("This dossier has both email and phone-message context. Bias toward yes when either channel "
                "shows a genuine human relationship; automated noise in one channel must not erase real "
                "correspondence in the other. Use maybe only when both channels remain genuinely ambiguous.")
    elif email:
        rule = ("This is an email-backed dossier. Bias toward yes for clearly human, person-directed correspondence, "
                "including sparse, one-off, old, academic, or plausibly important professional contacts. Use no only "
                "for clear automated mail, broadcast/transactional noise, or unengaged cold spam. Maybe should be rare.")
    else:
        rule = ("This is a phone-message-backed dossier. Repeated or clearly two-way personal or professional "
                "conversation is yes. Sparse context, a bare number, or an uncertain one-sided exchange may be maybe; "
                "automated service traffic or obvious spam is no. A name or area code is weak context only.")
    return "\n\nWORTH SOURCE POLICY:\n" + rule


def build_request(facts: SynthesizedFacts, channels: ChannelSummary, owner: OwnerProfile, reference_date: str) -> dict[str, Any]:
    """The pinned request: the family's facts (minus the identifiers it owns), its message counts, and the
    owner's background. `reference_date` is when the facts were synthesized, so the request changes only
    with its evidence."""
    payload: dict[str, Any] = facts.to_payload()
    del payload["owned_identifiers"]
    dossier_lines: list[str] = []
    for key, value in payload.items():
        if value:
            dossier_lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
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
    owner_state: dict[str, Any] = {"name": owner.name, "work": work, "education": education,
                                   "locations": list(owner.locations)}
    request: dict[str, Any] = share_request(
        dossier="\n".join(dossier_lines),
        facts=payload,
        profile={"name": facts.canonical_name, "title": facts.title, "company": company,
                 "location": facts.location, "headline": None},
        channels={
            "source_channels": list(channels.sources),
            "interaction_counts": channels.interaction_counts,
            "first_message_at": channels.first_message_at,
            "last_message_at": channels.last_message_at,
            "last_interaction": channels.last_message_at,
            "from_me": channels.from_me,
            "from_them": channels.from_them,
            "group_count": channels.group_count,
        },
        owner=owner_state,
        reference_date=reference_date,
    )
    # The 34 share-label questions, then the 7 worth questions with this family's source policy.
    questions: dict[str, dict[str, Any]] = share_questions()
    worth_questions: dict[str, dict[str, Any]] = copy.deepcopy(WORTH_QUESTIONS)
    worth_questions["worth"]["instructions"] += _channel_policy(channels.sources)
    for name, question in worth_questions.items():
        questions[name] = question
    request["questions"] = questions
    return request


def input_tokens(request: dict[str, Any]) -> int:
    """What the request costs to send, counted the way JEV is billed."""
    encoder = tiktoken.get_encoding("o200k_base")
    return len(encoder.encode(json.dumps(request, ensure_ascii=False, sort_keys=True)))


def cost_usd(tokens: int) -> float:
    return tokens * INPUT_PRICE_PER_MILLION / 1_000_000


def is_cached(request: dict[str, Any], cache_dir: Path) -> bool:
    return cache_path(cache_dir, request_digest(request)).exists()


# ---- the answers


class AnswerKind(StrEnum):
    NOUL = "noul"
    CHOICE = "choice"
    SCORE = "score"


@dataclass(frozen=True)
class Answer:
    kind: AnswerKind
    noul: float                      # the probability, on a noul answer; 0.0 otherwise
    probabilities: dict[str, float]  # option -> probability, on a choice or score answer; empty otherwise


def parse_answers(payload: dict[str, dict[str, Any]]) -> dict[str, Answer]:
    answers: dict[str, Answer] = {}
    for name, row in payload.items():
        probabilities: dict[str, float] = {}
        for option, value in row.get("probabilities", {}).items():
            probabilities[option] = float(value)
        answers[name] = Answer(AnswerKind(row["type"]), float(row.get("noul", 0)), probabilities)
    return answers


@dataclass(frozen=True)
class WorthModel:
    features: tuple[str, ...]
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    intercept: tuple[float, ...]
    coefficients: tuple[tuple[float, ...], ...]
    classes: tuple[str, ...]


def _load_model() -> WorthModel:
    payload: dict[str, Any] = json.loads((_HERE / "model.json").read_text())
    coefficients: list[tuple[float, ...]] = []
    for row in payload["coefficients"]:
        coefficients.append(tuple(row))
    return WorthModel(tuple(payload["features"]), tuple(payload["mean"]), tuple(payload["scale"]),
                      tuple(payload["intercept"]), tuple(coefficients), tuple(payload["classes"]))


MODEL = _load_model()


def features(answers: dict[str, Answer]) -> dict[str, float]:
    """One feature per noul answer and one per option of every other answer, named as the model was trained."""
    values: dict[str, float] = {}
    for name, answer in answers.items():
        prefix: str = "tag:"
        if name in WORTH_QUESTIONS:
            prefix = "worth:"
        if answer.kind == AnswerKind.NOUL:
            values[prefix + name] = answer.noul
            continue
        for option, probability in answer.probabilities.items():
            values[prefix + name + "=" + option] = probability
    return values


def class_scores(answers: dict[str, Answer]) -> tuple[list[float], list[float]]:
    """The standardized features and one logistic score per class."""
    values: dict[str, float] = features(answers)
    normalized: list[float] = []
    for feature, mean, scale in zip(MODEL.features, MODEL.mean, MODEL.scale):
        normalized.append((values[feature] - mean) / scale)
    scores: list[float] = []
    for intercept, weights in zip(MODEL.intercept, MODEL.coefficients):
        score: float = intercept
        for value, weight in zip(normalized, weights):
            score += value * weight
        scores.append(score)
    return normalized, scores


def best_index(scores: list[float], skip: int) -> int:
    best: int = -1
    for index, score in enumerate(scores):
        if index != skip and (best < 0 or score > scores[best]):
            best = index
    return best


def predict(answers: dict[str, Answer]) -> str:
    _, scores = class_scores(answers)
    return MODEL.classes[best_index(scores, -1)]


def labels(answers: dict[str, Answer]) -> dict[str, str | float]:
    """noul -> its probability; choice -> the likeliest option and its probability; score -> the
    expected level (2.7, not 3)."""
    result: dict[str, str | float] = {}
    for name, answer in answers.items():
        if answer.kind == AnswerKind.NOUL:
            result[name] = answer.noul
            continue
        if answer.kind == AnswerKind.SCORE:
            expected: float = 0.0
            for level, probability in answer.probabilities.items():
                expected += int(level) * probability
            result[name] = round(expected, 2)
            continue
        # A choice keeps its likeliest option; on a tie the first listed wins.
        best: str = ""
        for option, probability in answer.probabilities.items():
            if not best or probability > answer.probabilities[best]:
                best = option
        result[name] = best
        result[name + "_p"] = answer.probabilities[best]
    return result


async def answer_all(requests: dict[str, dict[str, Any]], cache_dir: Path) -> dict[str, dict[str, Answer]]:
    """Answer every request (keyed by its digest), reading the disk cache first. A fresh answer is
    cached as it arrives, so a stopped run keeps what it paid for."""
    answered = await answer_requests(requests, output_dir=cache_dir, api_key=None, client=None,
                                     concurrency=MAX_CONCURRENCY, request_version=REQUEST_VERSION,
                                     question_version=REQUEST_VERSION)
    parsed: dict[str, dict[str, Answer]] = {}
    for digest, answer in answered.items():
        parsed[digest] = parse_answers(answer.response["answers"])
    return parsed

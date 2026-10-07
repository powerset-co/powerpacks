"""The JEV identity judge: two evidence views of one family and one LinkedIn profile, and a frozen logistic model.

The network view asks whether the contact resolves to the profile; the association view asks whether
the source contact can be tied to it. Confirmed only when both views lean that way and the model's
probability is at least the threshold (0.5); otherwise needs review. JEV never says wrong person:
missing evidence is not a different person. Answers are cached on disk by their exact request.

Created: 2026-10-07
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tiktoken

from packs.ingestion.primitives.deep_context_v2.db.schema import Verdict
from packs.search.primitives.llm_rerank_candidates.jev.client import (
    INPUT_PRICE_PER_MILLION, answer_requests, cache_path, request_digest,
)
from packs.search.primitives.llm_rerank_candidates.jev.model import MODEL_ID

_HERE = Path(__file__).parent
REQUEST_VERSION = "identity-association-v1"
CONCURRENCY = 16
NETWORK = "network"
ASSOCIATION = "association"


@dataclass(frozen=True)
class Calibration:
    features: tuple[str, ...]
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float
    threshold: float


@dataclass(frozen=True)
class View:
    """One evidence view: its policy text and its questions."""

    policy: str
    questions: dict[str, Any]


def _load_calibration() -> Calibration:
    raw: dict[str, Any] = json.loads((_HERE / "jev_model.json").read_text())
    return Calibration(tuple(raw["features"]), tuple(raw["mean"]), tuple(raw["scale"]), tuple(raw["coefficients"]),
                       float(raw["intercept"]), float(raw["threshold"]))


def _load_views() -> dict[str, View]:
    views: dict[str, View] = {}
    for name, value in json.loads((_HERE / "jev_questions.json").read_text()).items():
        views[name] = View(value["policy"], value["questions"])
    return views


MODEL: Calibration = _load_calibration()
VIEWS: dict[str, View] = _load_views()


def requests(contact: dict[str, Any], profile: dict[str, Any], in_network: bool,
             network_urls: list[str]) -> dict[str, dict[str, Any]]:
    """The two view requests for one family and one profile. `in_network` says the URL came from the
    owner's LinkedIn connections rather than from web research."""
    provenance: str = "Proposed by web research; not independently verified."
    if in_network:
        provenance = "A LinkedIn connection whose name matches the contact's source names; not independently verified."
    context: dict[str, Any] = {
        "proposed_url_in_imported_linkedin_record": in_network,
        "same_parent_imported_urls": sorted(network_urls),
        "provenance": provenance,
    }
    built: dict[str, dict[str, Any]] = {}
    for name, view in VIEWS.items():
        built[name] = {
            "model": MODEL_ID,
            "state": {
                "dossier": json.dumps({"contact": contact, "network_context": context}, ensure_ascii=False),
                "profile": profile, "facts": {}, "channels": {}, "owner": {},
                "reference_date": "", "evidence_policy": view.policy,
            },
            "questions": view.questions,
        }
    return built


def probability(network: dict[str, Any], association: dict[str, Any]) -> float:
    """The model's match probability over both views' answers, in its feature order."""
    score: float = MODEL.intercept
    for feature, mean, scale, coefficient in zip(MODEL.features, MODEL.mean, MODEL.scale, MODEL.coefficients):
        parts: list[str] = feature.split(":")
        answers: dict[str, Any] = association
        if parts[0] == NETWORK:
            answers = network
        answer: dict[str, Any] = answers[parts[1]]
        # A choice feature names its option ("identity:confirmed"); a noul feature is the answer's probability.
        if len(parts) == 3:
            value: float = float(answer["probabilities"][parts[2]])
        else:
            value = float(answer["noul"])
        score += (value - mean) / scale * coefficient
    return 1 / (1 + math.exp(-score))


def _likeliest(probabilities: dict[str, float]) -> str:
    best: str = ""
    for option, value in probabilities.items():
        if not best or value > probabilities[best]:
            best = option
    return best


def classify(network: dict[str, Any], association: dict[str, Any]) -> str:
    """Confirmed only when both views lean to the match and the model agrees; else needs review."""
    if _likeliest(network["identity"]["probabilities"]) != "confirmed":
        return Verdict.NEEDS_REVIEW.value
    if _likeliest(association["association"]["probabilities"]) != "yes":
        return Verdict.NEEDS_REVIEW.value
    if probability(network, association) < MODEL.threshold:
        return Verdict.NEEDS_REVIEW.value
    return Verdict.CONFIRMED.value


def is_cached(cache_dir: Path, view: str, request: dict[str, Any]) -> bool:
    """One cache directory per view under the identity cache: <cache_dir>/<view>/."""
    return cache_path(cache_dir / view, request_digest(request)).exists()


def input_tokens(request: dict[str, Any]) -> int:
    encoder = tiktoken.get_encoding("o200k_base")
    return len(encoder.encode(json.dumps(request, ensure_ascii=False, sort_keys=True)))


def cost_usd(tokens: int) -> float:
    return tokens * INPUT_PRICE_PER_MILLION / 1_000_000


async def answer_all(pairs: list[dict[str, dict[str, Any]]], cache_dir: Path) -> list[str]:
    """One verdict per pair of view requests, in order. Each view's requests go out together; the disk
    cache answers whatever was asked before, and a fresh answer is cached as it arrives."""
    answers: dict[str, dict[str, Any]] = {}  # view -> digest -> answers
    for view in VIEWS:
        batch: dict[str, dict[str, Any]] = {}
        for pair in pairs:
            batch[request_digest(pair[view])] = pair[view]
        replies = await answer_requests(batch, output_dir=cache_dir / view, api_key=None, client=None,
                                        concurrency=CONCURRENCY, request_version=REQUEST_VERSION,
                                        question_version=REQUEST_VERSION)
        answered: dict[str, Any] = {}
        for digest, reply in replies.items():
            answered[digest] = reply.response["answers"]
        answers[view] = answered
    verdicts: list[str] = []
    for pair in pairs:
        network: dict[str, Any] = answers[NETWORK][request_digest(pair[NETWORK])]
        association: dict[str, Any] = answers[ASSOCIATION][request_digest(pair[ASSOCIATION])]
        verdicts.append(classify(network, association))
    return verdicts

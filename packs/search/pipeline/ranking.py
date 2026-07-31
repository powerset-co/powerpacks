"""Deterministic and explicitly authorized bounded GTM ranking."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace
from typing import Callable, Sequence

from .frontier import CandidateFrontier, CandidateRecord
from .models import SearchSpec


@dataclass(frozen=True)
class SemanticOutcome:
    person_id: str
    score: float | None = None
    error: str | None = None


SemanticAdapter = Callable[[SearchSpec, Sequence[CandidateRecord]], Sequence[SemanticOutcome]]


def deterministic_rank(
    frontier: CandidateFrontier,
    spec: SearchSpec,
    *,
    limit: int | None = None,
) -> CandidateFrontier:
    scored = []
    for candidate in frontier.candidates:
        lane_bonus = min(len(candidate.source_lanes), 4) * 0.05
        evidence_bonus = min(len(candidate.matched_position_ids), 3) * 0.02
        score = float(candidate.retrieval_score) + lane_bonus + evidence_bonus
        scored.append(replace(candidate, deterministic_score=score))
    scored.sort(key=lambda row: (-row.deterministic_score, row.person_id))
    return CandidateFrontier.merge(scored, spec.bounds.output_limit if limit is None else limit)


def production_semantic_adapter(
    spec: SearchSpec,
    candidates: Sequence[CandidateRecord],
) -> Sequence[SemanticOutcome]:
    """Invoke the existing reranker once with an explicit model and bounded inputs."""
    from ..primitives.llm_rerank_candidates.llm_rerank_candidates import RerankItem, rerank_all

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for authorized semantic ranking")
    items = [RerankItem(index, candidate.to_dict()) for index, candidate in enumerate(candidates)]
    traits = [
        {"value": criterion.name, "temporal": "all", "meaning": "general"}
        for criterion in spec.soft_criteria
    ]
    results = asyncio.run(
        rerank_all(
            items,
            query=spec.raw_request,
            traits=traits,
            api_base="https://api.openai.com/v1",
            api_key=api_key,
            model=str(spec.rank_model),
            reasoning_effort=None,
            concurrency=min(10, max(1, len(items))),
            timeout=60,
            max_retries=1,
            include_prompt=False,
        )
    )
    return tuple(
        SemanticOutcome(
            result.id,
            result.score if result.error is None else None,
            str(result.error) if result.error is not None else None,
        )
        for result in results
    )


def semantic_rank(
    frontier: CandidateFrontier,
    spec: SearchSpec,
    adapter: SemanticAdapter,
) -> tuple[CandidateFrontier, tuple[str, ...]]:
    bounded = tuple(frontier.candidates[: spec.bounds.semantic_rank_limit])
    try:
        outcomes = tuple(adapter(spec, bounded))
    except Exception as exc:  # noqa: BLE001
        return CandidateFrontier.merge(bounded, spec.bounds.output_limit), (
            f"semantic_rank:adapter:{exc}",
        )
    scores = {outcome.person_id: outcome.score for outcome in outcomes if outcome.error is None}
    errors = tuple(
        f"semantic_rank:{outcome.person_id}:{outcome.error}"
        for outcome in outcomes
        if outcome.error is not None
    )
    returned = {outcome.person_id for outcome in outcomes}
    errors += tuple(
        f"semantic_rank:{candidate.person_id}:adapter returned no outcome"
        for candidate in bounded
        if candidate.person_id not in returned
    )
    ranked = [replace(candidate, semantic_score=scores.get(candidate.person_id)) for candidate in bounded]
    ranked.sort(
        key=lambda candidate: (
            -(candidate.semantic_score if candidate.semantic_score is not None else -1.0),
            -candidate.deterministic_score,
            candidate.person_id,
        )
    )
    return CandidateFrontier.merge(ranked, spec.bounds.output_limit), errors

"""Pure feedback composition shared by local and hosted search viewers."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from packs.search.primitives.shared.human_ratings import convert_rating
from .model import Candidate, SearchResult


def _human_judgment(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("human judgment must be an object")
    rating = convert_rating(value)
    if rating["score"] is None:
        raise ValueError("Choose a score from 1–5")
    return {"score": rating["score"], "scale": rating["scale"]}


def build_feedback_payload(search: SearchResult, comment: str,
                           candidate: Candidate | None = None,
                           human_judgment: Mapping[str, Any] | None = None,
                           ) -> dict[str, Any]:
    """Compose one row from the run's search context plus, for a candidate, its
    group, fit reason, and trait scores — enough to re-label the result later."""
    queries = candidate.queries if candidate else search.queries
    metadata: dict[str, object] = {
        "source": "powerpacks-deep-search-results",
        "action": "candidate" if candidate else "search",
        "run_id": search.run_id,
        "queries": list(queries),
        "jd": search.jd_text,
        "title": search.title,
        "company": search.company,
    }
    if candidate:
        group = search.group_of(candidate.person_id)
        pond_row = candidate.in_pond(candidate.found_run, candidate.found_pond)
        reviewed = _human_judgment(human_judgment)
        if "score" in reviewed:
            reviewed["note"] = comment
        metadata.update({
            "person_id": candidate.person_id,
            "person_name": candidate.name,
            "linkedin_url": candidate.linkedin_url,
            "group": group.key if group else "",
            "group_label": group.label if group else "",
            "why": candidate.why,
            "found_query": candidate.found_query,
            "found_run": candidate.found_run,
            "found_pond": candidate.found_pond,
            "move_likelihood": ({"label": candidate.move_likelihood.label,
                                 "why": candidate.move_likelihood.why}
                                if candidate.move_likelihood else None),
            "human_judgment": reviewed,
            "candidate_judgment": asdict(candidate.candidate_judgment) if candidate.candidate_judgment else None,
            "person_title": candidate.title,
            "person_company": candidate.company,
            "person_location": candidate.location,
        })
        if pond_row:
            metadata.update({
                "reasoning": pond_row.reasoning,
                "final_score": pond_row.final_score,
                "cross_encoder_score": pond_row.cross_encoder_score,
                "traits": [{"name": trait.name, "score": trait.score,
                            "confidence": trait.confidence, "reason": trait.reason}
                           for trait in pond_row.traits],
            })
    return dict(
        comment=comment or "Candidate fit reviewed.",
        feedback_type=("taste_score" if human_judgment else "bad_rerank") if candidate else "bad_search",
        category="search",
        field_value=candidate.linkedin_url if candidate else search.run_id,
        metadata={key: value for key, value in metadata.items() if value not in (None, "", [], {})},
    )


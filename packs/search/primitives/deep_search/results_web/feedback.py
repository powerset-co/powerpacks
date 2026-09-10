"""Feedback wiring for the deep-search results viewer.

One row carries the full local search context it takes to re-label or train on
the result later: the run's pond queries and job description, and — for a
candidate row — its identifiers, group, fit reason, and trait scores. Message
content never travels.

Changelog:
- 2026-08-25: rows carry search/candidate context (JD, group, why, traits);
  they were identifiers-only before.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from packs.powerset.primitives.send_feedback.send_feedback import (
    FeedbackRequest,
    SendFeedback,
    default_set_id,
)

from .model import FIT_LABELS_FILE, Candidate, SearchResult
from packs.search.primitives.shared.human_ratings import convert_rating

ENV_FILE = Path(__file__).resolve().parents[5] / ".env"


def _human_judgment(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("human judgment must be an object")
    rating = convert_rating(value)
    if rating["score"] is None:
        raise ValueError("Choose a score from 1–5")
    return {"score": rating["score"], "scale": rating["scale"]}


def build_feedback_request(search: SearchResult, comment: str,
                           candidate: Candidate | None = None,
                           environ: dict[str, str] | None = None,
                           human_judgment: Mapping[str, Any] | None = None,
                           ) -> FeedbackRequest:
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
            "fit_experts": {
                expert.dimension: {"label": expert.label, "why": expert.why}
                for expert in candidate.fit_experts
            },
            "jd_fit": ({
                "coverage": candidate.jd_fit.coverage,
                "traits": [{"trait": row.trait, "status": row.status.value,
                            "evidence": row.evidence} for row in candidate.jd_fit.traits],
            } if candidate.jd_fit else {}),
            "human_judgment": reviewed,
            "person_title": candidate.title,
            "person_company": candidate.company,
            "person_location": candidate.location,
        })
        if pond_row:
            metadata.update({
                "reasoning": pond_row.reasoning,
                "final_score": pond_row.final_score,
                "traits": [{"name": trait.name, "score": trait.score,
                            "confidence": trait.confidence, "reason": trait.reason}
                           for trait in pond_row.traits],
            })
    return FeedbackRequest(
        comment=comment or "Candidate fit reviewed.",
        feedback_type=("taste_score" if human_judgment else "bad_rerank") if candidate else "bad_search",
        category="search",
        field_value=candidate.linkedin_url if candidate else search.run_id,
        metadata={key: value for key, value in metadata.items() if value},
        set_id=default_set_id(environ),
    )


def record_fit_label(run_dir: Path, request: FeedbackRequest) -> Path:
    """Append feedback and any human score before submitting to the API."""
    human = request.metadata.get("human_judgment", {})
    row = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": request.metadata["run_id"],
        "person_id": request.metadata.get("person_id", ""),
        "human": human,
        "model": {
            "group": request.metadata.get("group", ""),
            "rerank_score": request.metadata.get("final_score", 0),
            "jd_fit": request.metadata.get("jd_fit", {}),
        },
        "comment": request.comment,
    }
    path = run_dir / FIT_LABELS_FILE
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def submit_results_feedback(request: FeedbackRequest) -> dict[str, object]:
    return SendFeedback(request, env_file=ENV_FILE).run()

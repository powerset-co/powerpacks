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

from ..fit_contract import TraitStatus
from .model import Candidate, SearchResult

ENV_FILE = Path(__file__).resolve().parents[5] / ".env"
FIT_LABELS_FILE = "fit-labels.jsonl"
HUMAN_OUTCOMES = ("review", "pass")


def _human_judgment(candidate: Candidate, value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    overall = str(value.get("overall") or "")
    if overall not in HUMAN_OUTCOMES:
        raise ValueError(f"overall must be one of {', '.join(HUMAN_OUTCOMES)}")
    raw_traits = value.get("traits")
    if not isinstance(raw_traits, list):
        raise ValueError("traits must be a list")
    traits = []
    for row in raw_traits:
        if not isinstance(row, Mapping) or set(row) != {"trait", "status"}:
            raise ValueError("each human trait judgment needs trait and status")
        try:
            status = TraitStatus(str(row["status"]))
        except ValueError as exc:
            raise ValueError("human trait judgment has an invalid status") from exc
        traits.append({"trait": str(row["trait"]).strip(), "status": status.value})
    expected = [row.trait for row in candidate.jd_fit.traits] if candidate.jd_fit else []
    if [row["trait"] for row in traits] != expected:
        raise ValueError("human judgment must score every JD trait once, in order")
    return {"overall": overall, "traits": traits}


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
        reviewed = _human_judgment(candidate, human_judgment)
        metadata.update({
            "person_id": candidate.person_id,
            "person_name": candidate.name,
            "linkedin_url": candidate.linkedin_url,
            "group": group.key,
            "group_label": group.label,
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
        category="search",
        field_value=candidate.linkedin_url if candidate else search.run_id,
        metadata={key: value for key, value in metadata.items() if value},
        set_id=default_set_id(environ),
    )


def record_fit_label(run_dir: Path, request: FeedbackRequest) -> Path | None:
    """Append a structured human judgment and its model snapshot for local evaluation."""
    human = request.metadata.get("human_judgment")
    if not human:
        return None
    row = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": request.metadata["run_id"],
        "person_id": request.metadata["person_id"],
        "human": human,
        "model": {
            "group": request.metadata["group"],
            "rerank_score": request.metadata.get("final_score", 0),
            "jd_fit": request.metadata["jd_fit"],
        },
        "comment": request.comment,
    }
    path = run_dir / FIT_LABELS_FILE
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def submit_results_feedback(request: FeedbackRequest) -> dict[str, object]:
    return SendFeedback(request, env_file=ENV_FILE).run()

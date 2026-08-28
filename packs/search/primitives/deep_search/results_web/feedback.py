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

import os
import uuid
from pathlib import Path

from packs.powerset.primitives.send_feedback.send_feedback import FeedbackRequest, SendFeedback

from .model import Candidate, SearchResult

ENV_FILE = Path(__file__).resolve().parents[5] / ".env"


def _default_set_id(environ: dict[str, str] | None = None) -> str:
    raw = str((environ if environ is not None else os.environ)
              .get("POWERPACKS_DEFAULT_SET_ID") or "").strip()
    try:
        uuid.UUID(raw)
    except ValueError:
        return ""
    return raw


def build_feedback_request(search: SearchResult, comment: str,
                           candidate: Candidate | None = None,
                           environ: dict[str, str] | None = None) -> FeedbackRequest:
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
        comment=comment,
        category="search",
        field_value=candidate.linkedin_url if candidate else search.run_id,
        metadata={key: value for key, value in metadata.items() if value},
        set_id=_default_set_id(environ),
    )


def submit_results_feedback(request: FeedbackRequest) -> dict[str, object]:
    return SendFeedback(request, env_file=ENV_FILE).run()

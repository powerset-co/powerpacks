"""Identifiers-only feedback wiring for the deep-search results viewer."""

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
    """Compose one row from run/person identifiers; no evidence prose travels."""
    queries = candidate.queries if candidate else search.queries
    metadata: dict[str, object] = {
        "source": "powerpacks-deep-search-results",
        "action": "candidate" if candidate else "search",
        "run_id": search.run_id,
        "queries": list(queries),
    }
    if candidate:
        metadata.update({
            "person_name": candidate.name,
            "linkedin_url": candidate.linkedin_url,
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

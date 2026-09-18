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
from .feedback_payload import build_feedback_payload

ENV_FILE = Path(__file__).resolve().parents[5] / ".env"


def build_feedback_request(search: SearchResult, comment: str,
                           candidate: Candidate | None = None,
                           environ: dict[str, str] | None = None,
                           human_judgment: Mapping[str, Any] | None = None,
                           ) -> FeedbackRequest:
    return FeedbackRequest(**build_feedback_payload(search, comment, candidate, human_judgment),
                           set_id=default_set_id(environ))


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
            "cross_encoder_score": request.metadata.get("cross_encoder_score"),
            "move_likelihood": request.metadata.get("move_likelihood"),
            "candidate_judgment": request.metadata.get("candidate_judgment"),
        },
        "comment": request.comment,
    }
    path = run_dir / FIT_LABELS_FILE
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def submit_results_feedback(request: FeedbackRequest) -> dict[str, object]:
    return SendFeedback(request, env_file=ENV_FILE).run()

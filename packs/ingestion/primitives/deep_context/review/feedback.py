"""Send identity-level directory feedback without message-derived prose."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any, Protocol

from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp
from packs.ingestion.primitives.deep_context.db.people_views import (
    CandidateViewRow,
    ParentViewRow,
)
from packs.ingestion.primitives.deep_context.review.models import (
    FeedbackAlert,
    FeedbackSubmission,
)
from packs.powerset.primitives.send_feedback.send_feedback import (
    FeedbackRequest,
    SendFeedback,
)

ENV_FILE = Path(__file__).resolve().parents[5] / ".env"
FEEDBACK_ACTIONS = {"worth_yes", "worth_no", "retarget", "general", "skip"}
_feedback_alert = FeedbackAlert()


class GuidanceFeedbackRow(Protocol):
    guidance: str
    state: str
    new_url: str
    submitted_at: IsoTimestamp


def _clean(value: Any) -> str:
    return str(value or "").strip()


def default_set_id(environ: dict[str, str] | None = None) -> str:
    raw = _clean((environ if environ is not None else os.environ)
                 .get("POWERPACKS_DEFAULT_SET_ID"))
    try:
        uuid.UUID(raw)
    except ValueError:
        return ""
    return raw


def build_feedback_request(parent: ParentViewRow, candidate: CandidateViewRow | None, *, action: str,
                           comment: str, retarget_items: list[GuidanceFeedbackRow] | None = None,
                           environ: dict[str, str] | None = None) -> FeedbackRequest:
    slug = parent.slug
    url = candidate.url if candidate else ""
    new_url = candidate.new_url if candidate else ""
    guidance = [
        {"guidance": item.guidance,
         "state": item.state,
         "new_url": item.new_url,
         "submitted_at": item.submitted_at}
        for item in retarget_items or []
        if item.guidance
    ]
    metadata: dict[str, Any] = {
        "source": "powerpacks-directory",
        "action": action,
        "person_name": parent.name,
        "parent_slug": slug,
        "person_ids": [_clean(v) for v in parent.person_ids
                       if _clean(v) and not _clean(v).lower().startswith("candidate:")],
        "public_identifier": candidate.pub if candidate else "",
        "linkedin_url": url,
        "proposed_linkedin_url": new_url,
        "linkedin_confidence": _clean(candidate.confidence if candidate else ""),
        "candidate_action": candidate.action if candidate else "",
        "candidate_approved": candidate.approved if candidate else "",
        "machine_worth": parent.worth_row.machine.decision,
        "human_worth": parent.worth_row.human.decision if parent.worth_row.human else "",
    }
    if guidance:
        metadata["retarget_guidance"] = guidance
    return FeedbackRequest(
        comment=comment, category="linkedin" if guidance else "worth", field_value=url or new_url,
        metadata={key: value for key, value in metadata.items() if value},
        set_id=default_set_id(environ),
    )


def feedback_alert() -> FeedbackAlert:
    return _feedback_alert


def submit_directory_feedback(request: FeedbackRequest) -> FeedbackSubmission:
    return FeedbackSubmission.from_payload(
        SendFeedback(request, env_file=ENV_FILE).run()
    )


def post_feedback_quietly(request: FeedbackRequest) -> None:
    global _feedback_alert
    try:
        result = submit_directory_feedback(request)
        submitted = result.status == "submitted"
        _feedback_alert = FeedbackAlert(
            status="" if submitted else result.status,
            error="" if submitted else result.error,
        )
        if not submitted:
            print(
                f"[feedback] not submitted: {result.status} {result.error}".rstrip(),
                file=sys.stderr,
                flush=True,
            )
    except Exception as exc:
        _feedback_alert = FeedbackAlert(
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        print(f"[feedback] failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

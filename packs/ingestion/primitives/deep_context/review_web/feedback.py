"""Send identity-level directory feedback without message-derived prose."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any, Protocol

from packs.ingestion.primitives.deep_context.db.people_views import (
    CandidateViewRow,
    ParentViewRow,
)
from packs.powerset.primitives.send_feedback.send_feedback import (
    FeedbackRequest,
    SendFeedback,
)

ENV_FILE = Path(__file__).resolve().parents[5] / ".env"
FEEDBACK_ACTIONS = {"worth_yes", "worth_no", "retarget", "general", "skip"}
FEEDBACK_ALERT: dict[str, str] = {"status": "", "error": ""}


class GuidanceFeedbackRow(Protocol):
    guidance: str
    state: str
    new_url: str
    submitted_at: str


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


def submit_directory_feedback(request: FeedbackRequest) -> dict[str, Any]:
    return SendFeedback(request, env_file=ENV_FILE).run()


def post_feedback_quietly(request: FeedbackRequest) -> None:
    try:
        payload = submit_directory_feedback(request)
        status = str(payload.get("status") or "failed")
        FEEDBACK_ALERT.update(status="" if status == "submitted" else status,
                              error="" if status == "submitted" else str(payload.get("error") or ""))
        if status != "submitted":
            print(f"[feedback] not submitted: {payload.get('status')}"
                  f" {payload.get('error', '')}".rstrip(), file=sys.stderr, flush=True)
    except Exception as exc:
        FEEDBACK_ALERT.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        print(f"[feedback] failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

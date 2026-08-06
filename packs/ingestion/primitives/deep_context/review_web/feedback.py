"""Send identity-level directory feedback without message-derived prose."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

from packs.powerset.primitives.send_feedback.send_feedback import (
    FeedbackRequest,
    SendFeedback,
)

ENV_FILE = Path(__file__).resolve().parents[5] / ".env"
FEEDBACK_ACTIONS = {"worth_yes", "worth_no", "retarget", "general", "skip"}
FEEDBACK_ALERT: dict[str, str] = {"status": "", "error": ""}


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


def build_feedback_request(parent: dict[str, Any], candidate: dict[str, Any], *, action: str,
                           comment: str, retarget_items: list[dict[str, Any]] | None = None,
                           environ: dict[str, str] | None = None) -> FeedbackRequest:
    slug = _clean(parent.get("slug"))
    url = _clean(candidate.get("url"))
    new_url = _clean(candidate.get("new_url"))
    worth_row = parent.get("worth_row") or {}
    machine = worth_row.get("machine") or {}
    guidance = [
        {"guidance": _clean(item.get("guidance")),
         "state": _clean(item.get("state")),
         "new_url": _clean(item.get("new_url")),
         "submitted_at": _clean(item.get("submitted_at"))}
        for item in retarget_items or []
        if _clean(item.get("guidance"))
    ]
    metadata: dict[str, Any] = {
        "source": "powerpacks-directory",
        "action": action,
        "person_name": _clean(parent.get("name")),
        "parent_slug": slug,
        "person_ids": [_clean(v) for v in parent.get("person_ids") or []
                       if _clean(v) and not _clean(v).lower().startswith("candidate:")],
        "public_identifier": _clean(candidate.get("pub")),
        "linkedin_url": url,
        "proposed_linkedin_url": new_url,
        "linkedin_confidence": _clean(candidate.get("confidence")),
        "candidate_action": _clean(candidate.get("action")),
        "candidate_approved": _clean(candidate.get("approved")),
        "machine_worth": _clean(machine.get("decision")),
        "human_worth": _clean((worth_row.get("human") or {}).get("decision")),
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

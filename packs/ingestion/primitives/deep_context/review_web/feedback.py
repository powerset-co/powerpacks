"""Directory feedback: compose one Powerset feedback row from a person pane.

No LLM. The user's typed note (from the post-decision popover mirrored off the
network-search-app FeedbackForm) plus everything the in-memory model already
knows about the person — current/proposed LinkedIn, worth decisions, judge
verdict labels, and especially any guided-retarget guidance the user submitted
— is shaped into a `FeedbackRequest` and shipped through the send_feedback
primitive to the existing `POST /v2/feedback` endpoint with the stored
`$powerset login` bearer.

Context collection is deliberately identity-level: names, slugs, URLs,
decisions, confidences, and USER-authored text. Machine free-text reasons
(llm_worth_reason, judge reasons) are dossier-synthesized from message bodies
and stay local per the privacy contract — decisions and confidences travel,
their prose does not.
"""
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

# The repo .env (canonical installs); os.environ still wins for worktree runs.
ENV_FILE = Path(__file__).resolve().parents[5] / ".env"

# Popover actions (worth decisions) plus the automatic one: a guided-retarget
# submit files its guidance as feedback with no extra input.
FEEDBACK_ACTIONS = {"worth_yes", "worth_no", "retarget"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def default_set_id(environ: dict[str, str] | None = None) -> str:
    """POWERPACKS_DEFAULT_SET_ID when it is a real UUID; otherwise blank
    (the server casts set_id, and local installs may carry junk values)."""
    raw = _clean((environ if environ is not None else os.environ)
                 .get("POWERPACKS_DEFAULT_SET_ID"))
    try:
        uuid.UUID(raw)
    except ValueError:
        return ""
    return raw


def build_feedback_request(parent: dict[str, Any], candidate: dict[str, Any], *,
                           action: str, comment: str,
                           retarget_items: list[dict[str, Any]] | None = None,
                           environ: dict[str, str] | None = None) -> FeedbackRequest:
    """One feedback row for one person pane decision. `retarget_items` is the
    guided-retarget queue snapshot; every item for this person rides along —
    the user's guidance text is the highest-value context we hold."""
    slug = _clean(parent.get("dossier_slug") or parent.get("slug"))
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
        "person_ids": [_clean(v) for v in parent.get("person_ids") or [] if _clean(v)],
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
    category = "linkedin" if guidance else "worth"
    return FeedbackRequest(
        comment=comment,
        category=category,
        field_value=url or new_url,
        metadata={key: value for key, value in metadata.items() if value},
        set_id=default_set_id(environ),
    )


def submit_directory_feedback(request: FeedbackRequest) -> dict[str, Any]:
    """Ship one composed row; returns the primitive's payload
    (submitted | needs_auth | failed | dry_run)."""
    return SendFeedback(request, env_file=ENV_FILE).run()


def post_feedback_quietly(request: FeedbackRequest) -> None:
    """Fire-and-forget shipper for automatic feedback (retarget submits):
    a failure is a stderr line, never a UI error."""
    try:
        payload = submit_directory_feedback(request)
        if payload.get("status") != "submitted":
            print(f"[feedback] not submitted: {payload.get('status')}"
                  f" {payload.get('error', '')}".rstrip(), file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"[feedback] failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

#!/usr/bin/env python3
"""Submit product feedback to the Powerset API's existing feedback endpoint.

POSTs one row to `POST /v2/feedback` (the `user_feedback` table) using the
credentials `$powerset login` already stores — the same silent-refresh bearer
`pull_runtime_keys` uses, so no re-auth or login screen is involved. The
`$feedback` skill composes the synopsis/guidance; this primitive only
validates, shapes, and ships the request.

Contract facts this module encodes (from the API):
- `feedback_type` defaults to `data_inconsistency`: the admin feedback queue
  and the LLM triage batch only pick up that type, and its triage taxonomy
  already has a `linkedin_fix` action for wrong-person reports.
- `conversation_id`/`set_id`/`interaction_id`/`message_id` must parse as UUIDs
  (the server casts and 500s otherwise), so they are validated here.
- `person_id` must be a PROD person UUID or omitted entirely — downstream
  consumers cast `person_id::uuid` at query time, so a local slug would poison
  whole batches. Local identifiers belong in `metadata`.
- Request bodies are capped at 1 MB server-side; this module refuses earlier.

Auth/base-URL helpers are imported from `pull_runtime_keys` (one home): env
aliases resolve the API base, `bearer_token()` mints a fresh Auth0 token via
`auth.py token --bearer-only` and raises with a `$powerset login` hint when
signed out.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys import (  # noqa: E402
    api_base,
    bearer_token,
)

FEEDBACK_TYPES = ("data_inconsistency", "bad_rerank", "bad_search", "filter_edit")
DEFAULT_FEEDBACK_TYPE = "data_inconsistency"
FEEDBACK_PATH = "/v2/feedback"
# The API rejects bodies over 1 MB; refuse well before that.
MAX_BODY_BYTES = 900_000
_UUID_FIELDS = ("conversation_id", "set_id", "interaction_id", "message_id", "person_id")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _require_uuid(name: str, value: str) -> str:
    """The server casts these to UUID (and person_id is cast downstream);
    validate here so a bad id fails as our error, not a 500 or a poisoned batch."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        uuid.UUID(value)
    except ValueError:
        raise SystemExit(
            f"{name} must be a UUID (got {value!r}); for local identifiers use "
            "--metadata instead") from None
    return value


@dataclass(frozen=True)
class FeedbackRequest:
    """One feedback row, validated at construction — the one config door."""

    comment: str
    feedback_type: str = DEFAULT_FEEDBACK_TYPE
    category: str = ""
    field_value: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    conversation_id: str = ""
    set_id: str = ""
    interaction_id: str = ""
    message_id: str = ""
    person_id: str = ""

    def __post_init__(self) -> None:
        if not self.comment.strip():
            raise SystemExit("a non-empty --comment is required")
        if self.feedback_type not in FEEDBACK_TYPES:
            raise SystemExit(
                f"feedback_type must be one of {', '.join(FEEDBACK_TYPES)}")
        for name in _UUID_FIELDS:
            object.__setattr__(self, name, _require_uuid(name, getattr(self, name)))

    def body(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "feedback_type": self.feedback_type,
            "comment": self.comment.strip(),
        }
        for name in ("category", "field_value", *_UUID_FIELDS):
            value = getattr(self, name)
            if value:
                payload[name] = value
        if self.metadata:
            payload["metadata"] = self.metadata
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_BODY_BYTES:
            raise SystemExit(
                f"feedback body exceeds {MAX_BODY_BYTES} bytes; trim the comment/metadata")
        return payload


def post_json(base: str, path: str, token: str, body: dict[str, Any],
              timeout: int = 30) -> tuple[int, dict[str, Any]]:
    """POST one JSON body; returns (http_status, parsed-or-empty response)."""
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
        try:
            return response.status, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return response.status, {}


class SendFeedback:
    """Construct-and-run: ship one FeedbackRequest to the Powerset API."""

    def __init__(self, request: FeedbackRequest, *, env_file: Path | None = None,
                 dry_run: bool = False) -> None:
        self.request = request
        self.env_file = env_file
        self.dry_run = dry_run

    def run(self) -> dict[str, Any]:
        body = self.request.body()
        if self.dry_run:
            return {"status": "dry_run", "path": FEEDBACK_PATH, "body": body}
        base = api_base(self.env_file)
        try:
            token = bearer_token()
        except SystemExit as exc:
            return {"status": "needs_auth", "error": str(exc)}
        try:
            http_status, response = post_json(base, FEEDBACK_PATH, token, body)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return {"status": "needs_auth", "http_status": exc.code,
                        "error": "Powerset rejected the token; run `$powerset login` and retry"}
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:500]
            except OSError:
                pass
            return {"status": "failed", "http_status": exc.code, "error": detail}
        except urllib.error.URLError as exc:
            return {"status": "failed", "error": f"network error: {exc.reason}"}
        return {"status": "submitted", "http_status": http_status,
                "feedback_id": str(response.get("id") or ""),
                "feedback_type": self.request.feedback_type}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--comment", required=True,
                   help="The synopsis: what was wrong and what was expected")
    p.add_argument("--feedback-type", default=DEFAULT_FEEDBACK_TYPE, choices=FEEDBACK_TYPES)
    p.add_argument("--category", default="",
                   help="Field family the issue is about, e.g. linkedin, name, title, company")
    p.add_argument("--field-value", default="",
                   help="The wrong value currently shown (e.g. the wrong LinkedIn URL)")
    p.add_argument("--metadata", default="",
                   help="JSON object with structured context: query, local slugs, guidance")
    p.add_argument("--conversation-id", default="", help="Prod conversation UUID if known")
    p.add_argument("--set-id", default="", help="Powerset set UUID (see POWERPACKS_DEFAULT_SET_ID)")
    p.add_argument("--interaction-id", default="")
    p.add_argument("--message-id", default="")
    p.add_argument("--person-id", default="",
                   help="PROD person UUID only; local ids go in --metadata")
    p.add_argument("--env-file", default=str(_REPO_ROOT / ".env"))
    p.add_argument("--dry-run", action="store_true",
                   help="Print the exact request body without sending")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    metadata: dict[str, Any] = {}
    if args.metadata:
        try:
            metadata = json.loads(args.metadata)
        except json.JSONDecodeError as exc:
            print(f"--metadata must be a JSON object: {exc}", file=sys.stderr)
            return 2
        if not isinstance(metadata, dict):
            print("--metadata must be a JSON object", file=sys.stderr)
            return 2
    try:
        request = FeedbackRequest(
            comment=args.comment, feedback_type=args.feedback_type,
            category=args.category, field_value=args.field_value,
            metadata=metadata, conversation_id=args.conversation_id,
            set_id=args.set_id, interaction_id=args.interaction_id,
            message_id=args.message_id, person_id=args.person_id)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    payload = SendFeedback(request, env_file=Path(args.env_file),
                           dry_run=args.dry_run).run()
    emit(payload)
    return {"submitted": 0, "dry_run": 0, "needs_auth": 3}.get(payload["status"], 1)


if __name__ == "__main__":
    raise SystemExit(main())

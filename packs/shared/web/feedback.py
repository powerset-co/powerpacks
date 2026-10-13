"""The People drawer's flag: a note about one person for the Powerset team, filed as one
`POST /v2/feedback` row through send_feedback, with that person as the drawer showed them (who they
are, their worth and share call) and the release. Whoever is signed in to Powerset sends it
(account.py).

Standard library only, like the server's persistent handler: it answers before the project's
Python environment exists.

Created: 2026-10-13
"""
from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path

from packs.powerset.primitives.send_feedback.send_feedback import FeedbackRequest, SendFeedback, default_set_id
from packs.shared.web import account

# The notes field's limit (web/src/components/shared/NotesField.tsx).
MAX_COMMENT = 4000
RELEASE_MANIFEST = Path(".release-please-manifest.json")


def send(root: Path, body: bytes) -> tuple[dict[str, object], HTTPStatus]:
    """`{"comment", "person"}` -> the sender's reply and its status: "submitted" with the row's id,
    else why not, in the page's words."""
    note = json.loads(body)
    comment = str(note.get("comment") or "").strip()
    if not comment or len(comment) > MAX_COMMENT:
        raise ValueError(f"Write a note of at most {MAX_COMMENT:,} characters.")
    release = json.loads((root / RELEASE_MANIFEST).read_text(encoding="utf-8"))["."]
    metadata = {"source": "powerpacks-people", "powerpacks_version": release, "person": note["person"]}
    request = FeedbackRequest(comment=comment, category="person", metadata=metadata,
                              set_id=default_set_id(account.config(root)))
    reply = SendFeedback(request, env_file=root / ".env").run()
    if reply["status"] == "submitted":
        return reply, HTTPStatus.OK
    if reply["status"] == "needs_auth":
        return {**reply, "error": "Sign in to Powerset again, then send."}, HTTPStatus.UNAUTHORIZED
    return {**reply, "error": "Powerset didn't take the note. Try again."}, HTTPStatus.BAD_GATEWAY

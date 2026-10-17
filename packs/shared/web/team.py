"""Team presence and local support threads, sent through the shared agent relay."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import UUID

from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.powerset.primitives.agent_inbox import agent_inbox
from packs.powerset.primitives.agent_inbox.messages import SupportRequest
from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as auth

SENT = Path(".powerpacks/support/sent")


def members(root: Path, email: str) -> dict:
    env_file = root / ".env"
    reply = agent_inbox._request(auth.api_base(env_file), "/v2/agents/presence", auth.bearer_token(env_file))
    rows = []
    for row in reply["agents"]:
        if row["email"].lower() == email.lower():
            continue
        rows.append({"operator_id": row["operator_id"], "name": row["name"], "email": row["email"],
                     "last_seen_at": row.get("last_seen_at") or ""})
    return {"me": {"email": email}, "members": rows}


def thread(root: Path, operator_id: str) -> dict:
    operator_id = str(UUID(operator_id))
    messages = []
    for path in (root / SENT).glob("*.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        if row["thread_id"] == operator_id:
            messages.append({"id": row["id"], "direction": "out", "text": row["text"], "at": row["created_at"]})
    for path in (root / ".powerpacks/inbox").glob("*.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        if row["kind"] == "support_result" and row["from"]["operator_id"] == operator_id:
            messages.append({"id": row["id"], "direction": "in", "text": row["payload"]["text"],
                             "at": row["created_at"]})
    messages.sort(key=lambda row: datetime.fromisoformat(row["at"].replace("Z", "+00:00")))
    return {"messages": messages}


def ask(root: Path, body: bytes) -> dict:
    form = json.loads(body)
    if not isinstance(form, dict) or not isinstance(form.get("operator_id"), str):
        raise ValueError("operator_id must be a uuid")
    operator_id = str(UUID(form["operator_id"]))
    text = form.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must not be empty")
    request = SupportRequest.parse({"text": text, "thread_id": operator_id})
    sent = agent_inbox.send(root / ".env", operator_id, "support_request", asdict(request))
    write_json(root / SENT / f"{sent['id']}.json",
               {"id": sent["id"], **asdict(request), "created_at": now_iso()})
    return thread(root, operator_id)

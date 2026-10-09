"""The sets the owner belongs to, for the People page: read from the cloud, kept in the store, created
and deleted through the cloud's `/v2/sets`.

A set is who can see the owner's shared network: every member of every set the owner is in. The owner
shares one network (the share list); sets are joined, created or deleted here and in the cloud app.

Invites ride the relay only: an invite is an agent message to an email (the relay delivers it when that
email has an account), the answer is an agent message back. The asks loop pulls both into
`.powerpacks/inbox/<id>.json`; the owner's sent invites are `.powerpacks/invites/<id>.json`, keyed by the
invite message's id. Nothing in the cloud's sets changes: an accepted invite is a set joined here.
Presence is `.powerpacks/presence.json`, the last relay heartbeat per operator, written by the asks loop.

Created: 2026-10-08
Changelog:
- 2026-10-08: invites over the relay, joined sets, and each member's last heartbeat.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import write_json
from packs.ingestion.primitives.deep_context_v2.db import queries_share
from packs.ingestion.primitives.deep_context_v2.db.queries_share import SetRow
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso
from packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys import api_base, bearer_token

SETS_PATH = "/v2/sets"
MESSAGES_PATH = "/v2/agent-messages"
INVITE = "set_invite"
REPLY = "set_invite_reply"
ACCEPTED = "accepted"
DECLINED = "declined"
PENDING = "pending"
TIMEOUT_SECONDS = 30


class NeedsSignIn(Exception):
    """Powerset has no usable sign-in on this machine."""


class CloudError(Exception):
    """The cloud refused or could not be reached; the words are what the page shows."""


@dataclass(frozen=True)
class Member:
    name: str
    email: str
    role: str
    operator_id: str = ""


@dataclass(frozen=True)
class SetView:
    set_id: str
    name: str
    role: str
    is_personal: bool
    member_count: int
    person_count: int
    members: tuple[Member, ...]
    refreshed_at: str


def _members(payload: list[dict[str, Any]]) -> list[Member]:
    found: list[Member] = []
    for row in payload:
        found.append(Member(str(row.get("name") or ""), str(row.get("email") or ""), str(row.get("role") or ""),
                            str(row.get("operator_id") or row.get("user_uuid") or "")))
    return found


class Sets:
    def __init__(self, conn: sqlite3.Connection, env_file: Path) -> None:
        self.conn = conn
        self.env_file = env_file
        self.data_root = env_file.parent / ".powerpacks"

    # ---- the cloud

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        try:
            token: str = bearer_token(self.env_file)
        except SystemExit as error:
            raise NeedsSignIn(str(error)) from error
        request = urllib.request.Request(
            api_base(self.env_file) + path, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw: bytes = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise NeedsSignIn("Powerset rejected the sign-in") from error
            raise CloudError(f"Powerset answered {error.code} for {path}") from error
        except urllib.error.URLError as error:
            raise CloudError(f"Couldn't reach Powerset: {error.reason}") from error

    def refresh(self) -> list[SetView]:
        """The cloud's sets, with members, written over the kept ones."""
        now: str = now_iso()
        rows: list[SetRow] = []
        for item in self._call("GET", SETS_PATH):
            detail: dict[str, Any] = self._call("GET", f"{SETS_PATH}/{urllib.parse.quote(str(item['id']), safe='')}")
            members: list[Member] = _members(detail.get("members") or [])
            rows.append((str(item["id"]), str(item["name"]), str(item.get("role") or ""), int(bool(item.get("is_personal"))),
                         int(item.get("member_count") or len(members)), int(item.get("person_count") or 0),
                         json.dumps([member.__dict__ for member in members], ensure_ascii=False), now))
        queries_share.replace_sets(self.conn, rows)
        return self.kept()

    def create(self, name: str) -> list[SetView]:
        self._call("POST", SETS_PATH, {"name": name, "description": None})
        return self.refresh()

    def delete(self, set_id: str) -> list[SetView]:
        self._call("DELETE", f"{SETS_PATH}/{urllib.parse.quote(set_id, safe='')}")
        return self.refresh()

    # ---- invites, over the relay

    def invite(self, set_id: str, email: str) -> None:
        """Send the invite to an email; the relay holds it until that email has an account."""
        name = next((view.name for view in self.kept() if view.set_id == set_id), "")
        sent = self._call("POST", MESSAGES_PATH, {"to": email, "kind": INVITE,
                                                  "payload": {"set_id": set_id, "set_name": name}})
        write_json(self.data_root / "invites" / f"{sent['id']}.json",
                   {"id": sent["id"], "set_id": set_id, "set_name": name, "email": email, "sent_at": now_iso()})

    def answer(self, invite_id: str, accepted: bool) -> None:
        """Tell the inviter's agent, then keep the answer on the invite."""
        path = self.data_root / "inbox" / f"{invite_id}.json"
        message: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        answer = ACCEPTED if accepted else DECLINED
        self._call("POST", MESSAGES_PATH, {"to": message["from"]["operator_id"], "kind": REPLY,
                                           "payload": {"invite_id": invite_id, "answer": answer}})
        write_json(path, {**message, "answer": answer, "answered_at": now_iso()})

    def _inbox(self, kind: str) -> list[dict[str, Any]]:
        found = [json.loads(path.read_text(encoding="utf-8")) for path in (self.data_root / "inbox").glob("*.json")]
        return sorted((message for message in found if message["kind"] == kind), key=lambda message: message["created_at"])

    def received(self) -> list[dict[str, Any]]:
        """Invites to this owner: pending ones to answer, accepted ones are sets joined here."""
        return self._inbox(INVITE)

    def sent(self) -> list[dict[str, Any]]:
        """This owner's invites, each with the answer its reply carried (pending until one arrives)."""
        replies = {reply["payload"]["invite_id"]: reply for reply in self._inbox(REPLY)}
        found: list[dict[str, Any]] = []
        for path in sorted((self.data_root / "invites").glob("*.json")):
            invite: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            reply = replies.get(invite["id"])
            found.append({**invite, "status": reply["payload"]["answer"] if reply else PENDING,
                          "name": reply["from"]["name"] if reply else "",
                          "operator_id": reply["from"]["operator_id"] if reply else ""})
        return found

    def presence(self) -> dict[str, str]:
        """The last relay heartbeat per operator id."""
        path = self.data_root / "presence.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    # ---- the store

    def kept(self) -> list[SetView]:
        found: list[SetView] = []
        for row in queries_share.sets(self.conn):
            members: list[Member] = _members(json.loads(row["members_json"]))
            found.append(SetView(row["set_id"], row["name"], row["role"], bool(row["is_personal"]), row["member_count"],
                                 row["person_count"], tuple(members), row["refreshed_at"]))
        return found


def _member(member: Member, seen: dict[str, str]) -> dict[str, Any]:
    return {**member.__dict__, "last_seen_at": seen.get(member.operator_id, "")}


def payload(sets: list[SetView], shared: int, default_set_id: str, *, received: list[dict[str, Any]] = (),
            sent: list[dict[str, Any]] = (), seen: dict[str, str] | None = None) -> dict[str, Any]:
    """The page's answer: the sets (cloud, then joined here), the invites waiting for an answer, how many
    people the owner shares, and the set searches default to."""
    seen = seen or {}
    items: list[dict[str, Any]] = []
    for view in sets:
        if view.is_personal and view.role != "owner":
            continue  # an app admin's list carries everyone's personal set; only the owner's own is local
        joined = [Member(invite["name"] or invite["email"], invite["email"], "member", invite["operator_id"])
                  for invite in sent if invite["set_id"] == view.set_id and invite["status"] == ACCEPTED]
        items.append({"set_id": view.set_id, "name": view.name, "role": view.role, "is_personal": view.is_personal,
                      "member_count": view.member_count + len(joined), "person_count": view.person_count,
                      "members": [_member(member, seen) for member in (*view.members, *joined)],
                      "invited": [{"id": invite["id"], "email": invite["email"], "status": invite["status"]}
                                  for invite in sent if invite["set_id"] == view.set_id and invite["status"] != ACCEPTED],
                      "refreshed_at": view.refreshed_at})
    for invite in received:
        if invite.get("answer") != ACCEPTED or any(item["set_id"] == invite["payload"]["set_id"] for item in items):
            continue
        inviter = Member(invite["from"]["name"], "", "owner", invite["from"]["operator_id"])
        items.append({"set_id": invite["payload"]["set_id"], "name": invite["payload"]["set_name"], "role": "member",
                      "is_personal": False, "member_count": 1, "person_count": 0, "members": [_member(inviter, seen)],
                      "invited": [], "refreshed_at": invite["answered_at"]})
    invites = [{"id": invite["id"], "set_name": invite["payload"]["set_name"], "from": invite["from"]["name"],
                "created_at": invite["created_at"]} for invite in received if "answer" not in invite]
    return {"sets": items, "invites": invites, "shared": shared, "default_set_id": default_set_id}

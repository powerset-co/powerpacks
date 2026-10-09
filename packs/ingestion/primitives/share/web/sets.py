"""The owner's sets, for the People page: local only, kept in the store; invites and answers ride the relay.

A set is a named group of people who see each other's shared networks. Sets live on this machine; the
cloud keeps no set information. Creating a set stores it here with the owner as its one member. An invite
is an agent message to an email (the relay delivers it when that email has an account); accepting stores
the set on the invitee's machine and sends an agent message back, and the owner's set lists the new member.
The asks loop pulls both kinds into `.powerpacks/inbox/<id>.json`; the owner's sent invites are
`.powerpacks/invites/<id>.json`, keyed by the invite message's id. Presence is `.powerpacks/presence.json`,
the last relay heartbeat per operator, written by the asks loop.

A set's people are the shared people of its members: the share_v1 summaries documents (one per person)
whose allowed_operator_ids hold any member's operator id.

Created: 2026-10-08
Changelog:
- 2026-10-08: local sets; no /v2/sets. Invites over the relay, joined sets stored on accept, each member's
  last heartbeat, and the set's people (and each member's) counted in the share_v1 namespace.
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import turbopuffer

from packs.indexing.primitives.upload_powerset.upload_powerset import share_namespace
from packs.ingestion.primitives.common.jsonio import write_json
from packs.ingestion.primitives.deep_context_v2.db import queries_share
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso
from packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys import api_base, bearer_token

ME_PATH = "/v2/team/me"
MESSAGES_PATH = "/v2/agent-messages"
INVITE = "set_invite"
REPLY = "set_invite_reply"
ACCEPTED = "accepted"
DECLINED = "declined"
PENDING = "pending"
OWNER = "owner"
MEMBER = "member"
PERSONAL_ID = "personal"
TIMEOUT_SECONDS = 30


class NeedsSignIn(Exception):
    """Powerset has no usable sign-in on this machine."""


class CloudError(Exception):
    """The relay refused or could not be reached; the words are what the page shows."""


@dataclass(frozen=True)
class Member:
    name: str
    email: str
    role: str
    operator_id: str


@dataclass(frozen=True)
class SetView:
    set_id: str
    name: str
    role: str
    members: tuple[Member, ...]


class Sets:
    def __init__(self, conn: sqlite3.Connection, env_file: Path) -> None:
        self.conn = conn
        self.env_file = env_file
        self.data_root = env_file.parent / ".powerpacks"
        self._me: Member | None = None

    # ---- the relay

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

    def me(self) -> Member:
        """This machine's signed-in operator, asked once per server."""
        if self._me is None:
            account: dict[str, Any] = self._call("GET", ME_PATH)
            self._me = Member(account["email"], account["email"], OWNER, account["operator_id"])
        return self._me

    # ---- sets, on this machine

    def create(self, name: str) -> None:
        me = self.me()
        queries_share.insert_set(self.conn, str(uuid.uuid4()), name, OWNER, json.dumps([asdict(me)]), now_iso())

    def delete(self, set_id: str) -> None:
        queries_share.delete_set(self.conn, set_id)

    def kept(self) -> list[SetView]:
        return [SetView(row["set_id"], row["name"], row["role"],
                        tuple(Member(**member) for member in json.loads(row["members_json"])))
                for row in queries_share.sets(self.conn)]

    # ---- invites, over the relay

    def invite(self, set_id: str, email: str) -> None:
        """Send the invite to an email; the relay holds it until that email has an account."""
        name = next(view.name for view in self.kept() if view.set_id == set_id)
        me = self.me()
        sent = self._call("POST", MESSAGES_PATH, {"to": email, "kind": INVITE,
                                                  "payload": {"set_id": set_id, "set_name": name, "from_email": me.email}})
        write_json(self.data_root / "invites" / f"{sent['id']}.json",
                   {"id": sent["id"], "set_id": set_id, "set_name": name, "email": email, "sent_at": now_iso()})

    def answer(self, invite_id: str, accepted: bool) -> None:
        """Tell the inviter's agent; an accepted invite is a set stored here with the inviter and me."""
        path = self.data_root / "inbox" / f"{invite_id}.json"
        message: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        answer = ACCEPTED if accepted else DECLINED
        self._call("POST", MESSAGES_PATH, {"to": message["from"]["operator_id"], "kind": REPLY,
                                           "payload": {"invite_id": invite_id, "answer": answer}})
        if accepted:
            inviter = Member(message["from"]["name"], message["payload"].get("from_email", ""), OWNER,
                             message["from"]["operator_id"])
            me = replace(self.me(), role=MEMBER)
            queries_share.insert_set(self.conn, message["payload"]["set_id"], message["payload"]["set_name"], MEMBER,
                                     json.dumps([asdict(inviter), asdict(me)]), now_iso())
        write_json(path, {**message, "answer": answer, "answered_at": now_iso()})

    def _inbox(self, kind: str) -> list[dict[str, Any]]:
        found = [json.loads(path.read_text(encoding="utf-8")) for path in (self.data_root / "inbox").glob("*.json")]
        return sorted((message for message in found if message["kind"] == kind), key=lambda message: message["created_at"])

    def received(self) -> list[dict[str, Any]]:
        """Invites to this owner not yet answered."""
        return [message for message in self._inbox(INVITE) if "answer" not in message]

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

    # ---- the shared network

    def people(self, operator_ids: list[str]) -> int:
        """People shared by any of these operators: share_v1 summaries documents, one per person."""
        client = turbopuffer.Turbopuffer(api_key=os.environ["TURBOPUFFER_API_KEY"],
                                         region=os.environ.get("TURBOPUFFER_REGION", "gcp-us-central1"))
        try:
            response = client.namespace(share_namespace("summaries")).query(
                filters=("allowed_operator_ids", "ContainsAny", operator_ids), aggregate_by={"people": ("Count",)})
        except turbopuffer.NotFoundError:
            return 0  # nobody has shared into share_v1 yet
        return int(response.aggregations["people"])


def _member(member: Member, seen: dict[str, str], shared: dict[str, int]) -> dict[str, Any]:
    return {**asdict(member), "last_seen_at": seen.get(member.operator_id, ""),
            "person_count": shared.get(member.operator_id, 0)}


def payload(sets: Sets, shared: int) -> dict[str, Any]:
    """The page's answer: the personal network, the sets here (each with its members, their presence, the
    invites still open and its people), the invites waiting for an answer, and how many people the owner shares."""
    seen = sets.presence()
    sent = sets.sent()
    me = sets.me()
    contributed: dict[str, int] = {}  # people each member shares, one count per operator per answer

    def counted(held: Member) -> dict[str, Any]:
        if held.operator_id not in contributed:
            contributed[held.operator_id] = sets.people([held.operator_id])
        return _member(held, seen, contributed)

    items: list[dict[str, Any]] = [{"set_id": PERSONAL_ID, "name": "Personal network", "role": OWNER,
                                    "is_personal": True, "member_count": 1, "person_count": shared,
                                    "members": [_member(me, seen, {me.operator_id: shared})], "invited": []}]
    for view in sets.kept():
        joined = [Member(invite["name"] or invite["email"], invite["email"], MEMBER, invite["operator_id"])
                  for invite in sent if invite["set_id"] == view.set_id and invite["status"] == ACCEPTED]
        members = [*view.members, *(member for member in joined
                                    if member.operator_id not in {held.operator_id for held in view.members})]
        items.append({"set_id": view.set_id, "name": view.name, "role": view.role, "is_personal": False,
                      "member_count": len(members),
                      "person_count": sets.people([member.operator_id for member in members]),
                      "members": [counted(held) for held in members],
                      "invited": [{"id": invite["id"], "email": invite["email"], "status": invite["status"]}
                                  for invite in sent if invite["set_id"] == view.set_id and invite["status"] != ACCEPTED]})
    invites = [{"id": invite["id"], "set_name": invite["payload"]["set_name"], "from": invite["from"]["name"],
                "from_email": invite["payload"].get("from_email", ""), "created_at": invite["created_at"]}
               for invite in sets.received()]
    return {"sets": items, "invites": invites, "shared": shared}

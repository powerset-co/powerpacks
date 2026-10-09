"""The owner's sets, for the People page: local only, kept in the store; invites and answers ride the relay.

A set is a named group of people who see each other's shared networks. Sets live on this machine; the
cloud keeps no set information. Creating a set stores it here with the owner as its one member. An invite
is an agent message to an email (the relay delivers it when that email has an account); accepting stores
the set on the invitee's machine and sends an agent message back, and the owner's set lists the new member.
Deleting (owner) or leaving (member) sends set_deleted to the members or set_left to the owner the same way.
The asks loop pulls both kinds into `.powerpacks/inbox/<id>.json`; the owner's sent invites are
`.powerpacks/invites/<id>.json`, keyed by the invite message's id. Presence is `.powerpacks/presence.json`,
the last relay heartbeat per operator, written by the asks loop.

A set's people are the shared people of its members: the share_v1 summaries documents (one per person)
whose allowed_operator_ids hold any member's operator id.

Created: 2026-10-08
Changelog:
- 2026-10-09: spell out owner collection in settle.
- 2026-10-09: deleting a set also tells those with a pending invite; their machine drops the invite.
- 2026-10-08: the owner sends the full member list to every member when it changes (set_members), so a
  third member's laptop learns the others.
- 2026-10-08: deleting a set tells its members' agents, leaving tells the owner's; each side applies it on read.
- 2026-10-08: local sets; no /v2/sets. Invites over the relay, joined sets stored on accept, each member's
  last heartbeat, and the set's people (and each member's) counted in the share_v1 namespace, cached an hour.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
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
DELETED = "set_deleted"  # the owner deleted the set: each member's machine drops it
LEFT = "set_left"        # a member left: the owner's machine drops them from the set
MEMBERS = "set_members"  # the owner's full member list, sent to every member when it changes
ACCEPTED = "accepted"
DECLINED = "declined"
PENDING = "pending"
OWNER = "owner"
MEMBER = "member"
PERSONAL_ID = "personal"
TIMEOUT_SECONDS = 30
# A count moves only when someone shares; the page asks every 10 s, so it reads this cache.
COUNT_SECONDS = 3600
_LOG = logging.getLogger(__name__)


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
        self._turbopuffer: turbopuffer.Turbopuffer | None = None
        self._counts: dict[frozenset[str], tuple[float, int]] = {}  # operator ids -> (counted at, people)

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

    def message(self, to: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """One agent message over the relay; the relay holds it until the recipient's laptop pulls it."""
        sent: dict[str, Any] = self._call("POST", MESSAGES_PATH, {"to": to, "kind": kind, "payload": payload})
        return sent

    def known_operators(self) -> set[str]:
        """Everyone in a set on this machine: the only senders whose asks and debug requests are answered."""
        return {member.operator_id for view in self.kept() for member in self.members(view)}

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
        """The owner deletes the set for everyone; a member leaves it. Either way the others' agents hear
        it over the relay, then the set goes from this machine."""
        view = next(view for view in self.kept() if view.set_id == set_id)
        me = self.me()
        if view.role == OWNER:
            kind = DELETED
            told = [member.operator_id for member in self.members(view) if member.operator_id != me.operator_id]
            told += [invite["email"] for invite in self.sent() if invite["set_id"] == set_id and invite["status"] == PENDING]
        else:
            kind, told = LEFT, [member.operator_id for member in view.members if member.role == OWNER]
        for to in told:
            self.message(to, kind, {"set_id": set_id})
        queries_share.delete_set(self.conn, set_id)

    def members(self, view: SetView) -> list[Member]:
        """The set's members: those stored with it, then those whose accept reached this owner."""
        held = {member.operator_id for member in view.members}
        joined = [Member(invite["name"] or invite["email"], invite["email"], MEMBER, invite["operator_id"])
                  for invite in self.sent() if invite["set_id"] == view.set_id and invite["status"] == ACCEPTED]
        return [*view.members, *(member for member in joined if member.operator_id not in held)]

    def settle(self) -> None:
        """Apply the deletes, leaves and member lists the relay delivered since the last read, once each;
        then, for each set this owner owns, send the full member list to every member if it changed."""
        for message in self._inbox(DELETED) + self._inbox(LEFT) + self._inbox(MEMBERS):
            if "applied_at" in message:
                continue
            set_id = message["payload"]["set_id"]
            # Only the set's owner deletes it or names its members; from anyone else these change nothing.
            owners = set()
            for view in self.kept():
                if view.set_id != set_id:
                    continue
                for member in view.members:
                    if member.role == OWNER:
                        owners.add(member.operator_id)
            if message["kind"] == DELETED:
                if message["from"]["operator_id"] in owners:
                    queries_share.delete_set(self.conn, set_id)
            elif message["kind"] == MEMBERS:
                if message["from"]["operator_id"] in owners:
                    queries_share.update_set_members(self.conn, set_id, json.dumps(message["payload"]["members"]))
            else:
                replies = {reply["payload"]["invite_id"]: reply for reply in self._inbox(REPLY)}
                for path in (self.data_root / "invites").glob("*.json"):
                    invite: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
                    reply = replies.get(invite["id"])
                    if invite["set_id"] == set_id and reply and reply["from"]["operator_id"] == message["from"]["operator_id"]:
                        write_json(path, {**invite, "left_at": message["created_at"]})
            write_json(self.data_root / "inbox" / f"{message['id']}.json", {**message, "applied_at": now_iso()})
        self._announce()

    def _announce(self) -> None:
        """Send each owned set's member list to its members when it differs from the last one sent. A send
        that fails is tried again on the next read."""
        path = self.data_root / "set-members-sent.json"
        announced: dict[str, list[str]] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        me = self.me()
        for view in self.kept():
            if view.role != OWNER:
                continue
            members = self.members(view)
            ids = sorted(member.operator_id for member in members)
            if announced.get(view.set_id, [me.operator_id]) == ids:
                continue
            try:
                for member in members:
                    if member.operator_id != me.operator_id:
                        self.message(member.operator_id, MEMBERS,
                                     {"set_id": view.set_id, "members": [asdict(held) for held in members]})
            except (CloudError, NeedsSignIn) as error:
                _LOG.warning("Set members not sent for %s: %s", view.set_id, error)
                continue
            announced[view.set_id] = ids
            write_json(path, announced)

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
        """Invites to this owner not yet answered, minus those whose set the inviter has since deleted."""
        deleted = {(message["from"]["operator_id"], message["payload"]["set_id"]) for message in self._inbox(DELETED)}
        return [message for message in self._inbox(INVITE) if "answer" not in message
                and (message["from"]["operator_id"], message["payload"]["set_id"]) not in deleted]

    def sent(self) -> list[dict[str, Any]]:
        """This owner's invites, each with the answer its reply carried (pending until one arrives)."""
        replies = {reply["payload"]["invite_id"]: reply for reply in self._inbox(REPLY)}
        found: list[dict[str, Any]] = []
        for path in sorted((self.data_root / "invites").glob("*.json")):
            invite: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            if "left_at" in invite:
                continue  # accepted, then left the set
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

    def shared_by(self, person_ids: list[str]) -> dict[str, tuple[str, ...]]:
        """Per person id, the operators who shared that person into share_v1 (asked fresh, not cached)."""
        try:
            response = self._client().namespace(share_namespace("summaries")).query(
                filters=("id", "In", person_ids), top_k=len(person_ids),
                include_attributes=["allowed_operator_ids"])
        except turbopuffer.NotFoundError:
            return {}
        return {str(row.id): tuple(getattr(row, "allowed_operator_ids", None) or ()) for row in response.rows or []}

    def _client(self) -> turbopuffer.Turbopuffer:
        if self._turbopuffer is None:
            self._turbopuffer = turbopuffer.Turbopuffer(api_key=os.environ["TURBOPUFFER_API_KEY"],
                                                        region=os.environ.get("TURBOPUFFER_REGION", "gcp-us-central1"))
        return self._turbopuffer

    def people(self, operator_ids: list[str]) -> int:
        """People shared by any of these operators: share_v1 summaries documents, one per person, counted
        at most once per COUNT_SECONDS for the same operators."""
        key = frozenset(operator_ids)
        held = self._counts.get(key)
        if held is not None and time.monotonic() - held[0] < COUNT_SECONDS:
            return held[1]
        try:
            response = self._client().namespace(share_namespace("summaries")).query(
                filters=("allowed_operator_ids", "ContainsAny", operator_ids), aggregate_by={"people": ("Count",)})
            aggregations: dict[str, Any] = response.aggregations or {}
            count = int(aggregations["people"])
        except turbopuffer.NotFoundError:
            count = 0  # nobody has shared into share_v1 yet
        self._counts[key] = (time.monotonic(), count)
        return count


def _member(member: Member, seen: dict[str, str], shared: dict[str, int]) -> dict[str, Any]:
    return {**asdict(member), "last_seen_at": seen.get(member.operator_id, ""),
            "person_count": shared.get(member.operator_id, 0)}


def payload(sets: Sets, shared: int) -> dict[str, Any]:
    """The page's answer: the personal network, the sets here (each with its members, their presence, the
    invites still open and its people), the invites waiting for an answer, and how many people the owner shares."""
    sets.settle()
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
        members = sets.members(view)
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

"""The owner's sets, for the People page: local only, kept in the store; invites and answers ride the relay.

A set is a named group of people who see each other's shared networks. Sets live on this machine; the
cloud keeps no set information. Creating a set stores it here with the owner as its one member. An invite
is an agent message to an email (the relay delivers it when that email has an account); accepting stores
the set on the invitee's machine and sends an agent message back, and the owner adds the new member.

Who writes what:
- The page's clicks (create, delete or leave, invite, accept or decline) write their own change: the
  sets table, `.powerpacks/invites/<id>.json` for an invite sent, and the inbox file of an invite answered.
- Everything that arrives over the relay (an invite's answer, a delete, a leave, a member list) is applied
  by the asks loop alone, through `apply_inbox()`, which deletes each message once applied.
- Reading (the page's GET) writes nothing.
Presence is `.powerpacks/presence.json`, the last relay heartbeat per operator, written by the asks loop.

A set's people are the shared people of its members: the share summaries documents (one per person)
whose allowed_operator_ids hold any member's operator id.

Created: 2026-10-08
Changelog:
- 2026-10-09: one writer per change: relay messages are applied only by the asks loop (apply_inbox) and
  deleted once applied; reading applies nothing. An accepted invite adds its member to the stored list
  once, so a set's members have one home. Relay calls go through agent_inbox.
- 2026-10-09: deleting a set also tells those with a pending invite; their machine drops the invite.
- 2026-10-08: the owner sends the full member list to every member when it changes (set_members), so a
  third member's laptop learns the others.
- 2026-10-08: deleting a set tells its members' agents, leaving tells the owner's.
- 2026-10-08: local sets; no /v2/sets. Invites over the relay, joined sets stored on accept, each member's
  last heartbeat, and the set's people (and each member's) counted in the share namespace, cached an hour.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, cast

import turbopuffer

from packs.indexing.primitives.upload_powerset.upload_powerset import share_namespace
from packs.ingestion.primitives.common.jsonio import write_json
from packs.ingestion.primitives.deep_context_v2.db import queries_share
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso
from packs.powerset.primitives.agent_inbox import agent_inbox
from packs.powerset.primitives.agent_inbox.messages import (
    Envelope, SetDeleted, SetInvite, SetInviteReply, SetLeft, SetMembers,
)

ME_PATH = "/v2/team/me"
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
# A count moves only when someone shares; the page asks every 10 s, so it reads this cache.
COUNT_SECONDS = 3600


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

    def owner(self) -> Member:
        return next(member for member in self.members if member.role == OWNER)


class Sets:
    def __init__(self, conn: sqlite3.Connection, env_file: Path) -> None:
        self.conn = conn
        self.env_file = env_file
        self.data_root = env_file.parent / ".powerpacks"
        self._me: Member | None = None
        self._turbopuffer: turbopuffer.Turbopuffer | None = None
        self._counts: dict[frozenset[str], tuple[float, int]] = {}  # operator ids -> (counted at, people)

    # ---- the relay

    def message(self, to: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """One agent message over the relay; the relay holds it until the recipient's laptop pulls it."""
        return agent_inbox.send(self.env_file, to, kind, payload)

    def me(self) -> Member:
        """This machine's signed-in operator, asked once per server."""
        if self._me is None:
            account = agent_inbox.request(self.env_file, "GET", ME_PATH)
            self._me = Member(account["email"], account["email"], OWNER, account["operator_id"])
        return self._me

    # ---- reading

    def kept(self) -> list[SetView]:
        """The sets on this machine, with their stored members."""
        views = []
        for row in queries_share.sets(self.conn):
            members = tuple(Member(**member) for member in json.loads(row["members_json"]))
            views.append(SetView(row["set_id"], row["name"], row["role"], members))
        return views

    def known_operators(self) -> set[str]:
        """Everyone in a set on this machine: the only senders whose asks and debug requests are answered."""
        known = set()
        for view in self.kept():
            for member in view.members:
                known.add(member.operator_id)
        return known

    def received(self) -> list[Envelope]:
        """Invites to this machine not yet answered."""
        return agent_inbox.read(self.data_root, INVITE)

    def sent(self) -> list[dict[str, Any]]:
        """This owner's invites still open: pending, or declined."""
        invites = []
        for path in sorted((self.data_root / "invites").glob("*.json")):
            invites.append(json.loads(path.read_text(encoding="utf-8")))
        return invites

    def presence(self) -> dict[str, str]:
        """The last relay heartbeat per operator id."""
        path = self.data_root / "presence.json"
        if not path.exists():
            return {}
        seen: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
        return seen

    # ---- the page's clicks

    def create(self, name: str) -> None:
        me = self.me()
        queries_share.insert_set(self.conn, str(uuid.uuid4()), name, OWNER, json.dumps([asdict(me)]), now_iso())

    def delete(self, set_id: str) -> None:
        """The owner deletes the set for everyone (its members and anyone with an open invite hear it); a
        member leaves it (the owner hears it). Then the set goes from this machine."""
        view = self._view(set_id)
        me = self.me()
        if view.role == OWNER:
            for member in view.members:
                if member.operator_id != me.operator_id:
                    self.message(member.operator_id, DELETED, {"set_id": set_id})
            for invite in self.sent():
                if invite["set_id"] == set_id and invite["status"] == PENDING:
                    self.message(invite["email"], DELETED, {"set_id": set_id})
        else:
            self.message(view.owner().operator_id, LEFT, {"set_id": set_id})
        queries_share.delete_set(self.conn, set_id)

    def invite(self, set_id: str, email: str) -> None:
        """Send the invite to an email; the relay holds it until that email has an account."""
        view = self._view(set_id)
        me = self.me()
        sent = self.message(email, INVITE, {"set_id": set_id, "set_name": view.name, "from_email": me.email})
        write_json(self.data_root / "invites" / f"{sent['id']}.json",
                   {"id": sent["id"], "set_id": set_id, "set_name": view.name, "email": email,
                    "status": PENDING, "sent_at": now_iso()})

    def answer(self, invite_id: str, accepted: bool) -> None:
        """Tell the inviter's agent; an accepted invite is a set stored here with the inviter and me."""
        envelope = next(envelope for envelope in self.received() if envelope.id == invite_id)
        invite = cast(SetInvite, envelope.payload)
        # The set is stored before the owner hears the accept, so the owner's member list finds it here.
        if accepted:
            inviter = Member(envelope.from_name, invite.from_email, OWNER, envelope.from_operator_id)
            me = replace(self.me(), role=MEMBER)
            queries_share.insert_set(self.conn, invite.set_id, invite.set_name, MEMBER,
                                     json.dumps([asdict(inviter), asdict(me)]), now_iso())
        self.message(envelope.from_operator_id, REPLY,
                     {"invite_id": invite_id, "answer": ACCEPTED if accepted else DECLINED})
        agent_inbox.remove(self.data_root, envelope)

    # ---- the relay's changes, applied by the asks loop only

    def apply_inbox(self) -> None:
        """Apply each set message the relay delivered, oldest first, then delete it from the inbox. Each
        change sends its messages before it writes, so a failed send leaves the message to apply again."""
        for envelope in agent_inbox.read(self.data_root, REPLY, DELETED, LEFT, MEMBERS):
            message = envelope.payload
            if isinstance(message, SetInviteReply):
                self._replied(envelope.from_name, envelope.from_operator_id, message)
            elif isinstance(message, SetDeleted):
                self._deleted(envelope.from_operator_id, message.set_id)
            elif isinstance(message, SetLeft):
                self._left(envelope.from_operator_id, message.set_id)
            elif isinstance(message, SetMembers):
                self._members_from_owner(envelope.from_operator_id, message)
            agent_inbox.remove(self.data_root, envelope)

    def _replied(self, name: str, operator_id: str, reply: SetInviteReply) -> None:
        """An answer to one of this owner's invites: a decline is shown on the invite; an accept adds the
        member to the set (if it still exists) and closes the invite."""
        path = self.data_root / "invites" / f"{reply.invite_id}.json"
        invite = json.loads(path.read_text(encoding="utf-8"))
        if reply.answer == DECLINED:
            write_json(path, {**invite, "status": DECLINED})
            return
        for view in self.kept():  # none when the set was deleted while the invite was open
            if view.set_id == invite["set_id"]:
                joined = Member(name or invite["email"], invite["email"], MEMBER, operator_id)
                members = view.members
                if operator_id not in {member.operator_id for member in members}:
                    members = (*members, joined)
                self._save_members(view, members)
        path.unlink()

    def _deleted(self, sender: str, set_id: str) -> None:
        """The owner deleted a set: drop it here, and drop any invite to it still unanswered."""
        for view in self.kept():
            if view.set_id == set_id and view.owner().operator_id == sender:
                queries_share.delete_set(self.conn, set_id)
        for envelope in self.received():
            invite = cast(SetInvite, envelope.payload)
            if envelope.from_operator_id == sender and invite.set_id == set_id:
                agent_inbox.remove(self.data_root, envelope)

    def _left(self, sender: str, set_id: str) -> None:
        """A member left one of this owner's sets."""
        for view in self.kept():
            if view.set_id == set_id and view.role == OWNER:
                staying = tuple(member for member in view.members if member.operator_id != sender)
                self._save_members(view, staying)

    def _members_from_owner(self, sender: str, members: SetMembers) -> None:
        """The owner's full member list for a set this machine joined."""
        for view in self.kept():
            if view.set_id == members.set_id and view.owner().operator_id == sender:
                listed = [asdict(member) for member in members.members]
                queries_share.update_set_members(self.conn, view.set_id, json.dumps(listed))

    def _save_members(self, view: SetView, members: tuple[Member, ...]) -> None:
        """Send an owned set's new member list to every other member, then store it."""
        listed = [asdict(member) for member in members]
        me = self.me()
        for member in members:
            if member.operator_id != me.operator_id:
                self.message(member.operator_id, MEMBERS, {"set_id": view.set_id, "members": listed})
        queries_share.update_set_members(self.conn, view.set_id, json.dumps(listed))

    def _view(self, set_id: str) -> SetView:
        return next(view for view in self.kept() if view.set_id == set_id)

    # ---- the shared network

    def shared_by(self, person_ids: list[str]) -> dict[str, tuple[str, ...]]:
        """Per person id, the operators who shared that person (asked fresh, not cached)."""
        try:
            response = self._client().namespace(share_namespace("summaries")).query(
                filters=("id", "In", person_ids), top_k=len(person_ids),
                include_attributes=["allowed_operator_ids"])
        except turbopuffer.NotFoundError:
            return {}
        shared: dict[str, tuple[str, ...]] = {}
        for row in response.rows or []:
            shared[str(row.id)] = tuple(getattr(row, "allowed_operator_ids", None) or ())
        return shared

    def _client(self) -> turbopuffer.Turbopuffer:
        if self._turbopuffer is None:
            self._turbopuffer = turbopuffer.Turbopuffer(api_key=os.environ["TURBOPUFFER_API_KEY"],
                                                        region=os.environ.get("TURBOPUFFER_REGION", "gcp-us-central1"))
        return self._turbopuffer

    def people(self, operator_ids: list[str]) -> int:
        """People shared by any of these operators: summaries documents, one per person, counted at most
        once per COUNT_SECONDS for the same operators."""
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
            count = 0  # nobody has shared yet
        self._counts[key] = (time.monotonic(), count)
        return count


def payload(sets: Sets, shared: int) -> dict[str, Any]:
    """The page's answer: the personal network, the sets here (each with its members, their presence, the
    invites still open and its people), the invites waiting for an answer, and how many people the owner
    shares."""
    seen = sets.presence()
    me = sets.me()

    def member_row(member: Member, person_count: int) -> dict[str, Any]:
        return {**asdict(member), "last_seen_at": seen.get(member.operator_id, ""), "person_count": person_count}

    personal = {"set_id": PERSONAL_ID, "name": "Personal network", "role": OWNER, "is_personal": True,
                "member_count": 1, "person_count": shared, "members": [member_row(me, shared)], "invited": []}
    items = [personal]
    sent = sets.sent()
    for view in sets.kept():
        operator_ids = [member.operator_id for member in view.members]
        members = []
        for member in view.members:
            members.append(member_row(member, sets.people([member.operator_id])))
        invited = []
        for sent_invite in sent:
            if sent_invite["set_id"] == view.set_id:
                invited.append({"id": sent_invite["id"], "email": sent_invite["email"],
                                "status": sent_invite["status"]})
        items.append({"set_id": view.set_id, "name": view.name, "role": view.role, "is_personal": False,
                      "member_count": len(view.members), "person_count": sets.people(operator_ids),
                      "members": members, "invited": invited})

    invites = []
    for envelope in sets.received():
        invite = cast(SetInvite, envelope.payload)
        invites.append({"id": envelope.id, "set_name": invite.set_name, "from": envelope.from_name,
                        "from_email": invite.from_email, "created_at": envelope.created_at})
    return {"sets": items, "invites": invites, "shared": shared}

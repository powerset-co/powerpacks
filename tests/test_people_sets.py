"""The People page's sets: local only; invites and their answers ride the relay as agent messages.

Each test is a few laptops (Jordan owns, Casey and Riley are invited) and a stub relay that records what
was sent. Delivering a message writes it into the recipient's inbox the way the asks loop's pull does,
then runs `apply_inbox()`, which the asks loop runs after every pull.

Changelog:
- 2026-10-09: relay messages are applied by apply_inbox (the loop) and deleted; members have one home.
- 2026-10-08: created.
"""
from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from packs.ingestion.primitives.common.jsonio import write_json
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.share.web.sets import Sets, payload
from packs.powerset.primitives.agent_inbox import agent_inbox
from packs.powerset.primitives.agent_inbox.agent_inbox import NeedsSignIn

NAMES = {"jordan": "Jordan Bravo", "casey": "Casey Delta", "riley": "Riley Echo"}
OPERATORS = {name: str(uuid.uuid4()) for name in NAMES}
# People each operator shared; a set's people are its members' union.
SHARED = {OPERATORS["jordan"]: 5, OPERATORS["casey"]: 5, OPERATORS["riley"]: 5}
NAME_OF = {operator_id: name for name, operator_id in OPERATORS.items()}
REAL_REQUEST = agent_inbox.request  # the stub relay replaces it in setUp


class SetsTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sent: list[dict[str, Any]] = []
        self.laptops = {name: self.laptop(name) for name in NAMES}
        self.jordan, self.casey, self.riley = self.laptops["jordan"], self.laptops["casey"], self.laptops["riley"]
        self.enterContext(mock.patch.object(Sets, "people", lambda sets, ids: sum(SHARED.get(i, 0) for i in ids)))
        self.enterContext(mock.patch.object(agent_inbox, "request", side_effect=self.relay))

    def laptop(self, name: str) -> Sets:
        home = self.root / name
        (home / ".powerpacks").mkdir(parents=True)
        conn = open_store(home / "store.sqlite")
        self.addCleanup(conn.close)
        return Sets(conn, home / ".env")

    def relay(self, env_file: Path, method: str, path: str, body: dict | None = None) -> dict:
        sender = env_file.parent.name
        if path == "/v2/team/me":
            return {"email": f"{sender}@example.com", "operator_id": OPERATORS[sender]}
        message = {"id": str(uuid.uuid4()), "sender": sender, **(body or {})}
        self.sent.append(message)
        return {"id": message["id"], "status": "sent"}

    def deliver(self, to: str) -> None:
        """The last message sent, as the recipient's loop pulls and applies it."""
        self.deliver_message(self.sent[-1], to)

    def deliver_message(self, message: dict[str, Any], to: str) -> None:
        laptop = self.laptops[to]
        created_at = f"2026-10-09T00:00:{self.sent.index(message):02d}Z"  # the relay stamps in send order
        write_json(laptop.data_root / "inbox" / f"{message['id']}.json",
                   {"id": message["id"], "kind": message["kind"], "payload": message["payload"],
                    "from": {"operator_id": OPERATORS[message["sender"]], "name": NAMES[message["sender"]]},
                    "created_at": created_at})
        laptop.apply_inbox()

    def joined(self, *invitees: str) -> str:
        """Jordan's set "Founders" with these invitees accepted, every laptop up to date; the set id."""
        self.jordan.create("Founders")
        set_id = self.jordan.kept()[0].set_id
        for name in invitees:
            self.jordan.invite(set_id, f"{name}@example.com")
            self.deliver(name)
            self.laptops[name].answer(self.sent[-1]["id"], accepted=True)
            before = len(self.sent)
            self.deliver("jordan")
            # Applying the accept sends the owner's new member list to every member.
            for message in self.sent[before:]:
                member = NAME_OF[message["to"]]
                self.deliver_message(message, member)
        return set_id

    def emails(self, name: str, set_index: int = 1) -> list[str]:
        members = payload(self.laptops[name], 0)["sets"][set_index]["members"]
        return [member["email"] for member in members]

    def test_invite_accept_round_trip(self) -> None:
        self.jordan.create("Founders")
        set_id = self.jordan.kept()[0].set_id
        self.jordan.invite(set_id, "casey@example.com")
        self.assertEqual((self.sent[0]["to"], self.sent[0]["payload"]),
                         ("casey@example.com", {"set_id": set_id, "set_name": "Founders",
                                                "from_email": "jordan@example.com"}))
        self.assertEqual(payload(self.jordan, 0)["sets"][1]["invited"],
                         [{"id": self.sent[0]["id"], "email": "casey@example.com", "status": "pending"}])

        self.deliver("casey")
        invites = payload(self.casey, 0)["invites"]
        self.assertEqual((invites[0]["from"], invites[0]["from_email"]), ("Jordan Bravo", "jordan@example.com"))
        self.casey.answer(invites[0]["id"], accepted=True)
        self.assertEqual(self.sent[-1]["payload"], {"invite_id": invites[0]["id"], "answer": "accepted"})
        self.assertEqual(payload(self.casey, 0)["invites"], [])
        self.assertEqual(self.emails("casey"), ["jordan@example.com", "casey@example.com"])

        self.deliver("jordan")
        founders = payload(self.jordan, 0)["sets"][1]
        self.assertEqual((founders["member_count"], founders["person_count"], founders["invited"]), (2, 10, []))
        self.assertEqual(founders["members"][-1]["name"], "Casey Delta")
        self.assertEqual([member["person_count"] for member in founders["members"]], [5, 5])
        # Applied once and gone: reading again changes nothing.
        self.assertEqual(list((self.jordan.data_root / "inbox").glob("*.json")), [])

    def test_a_third_member_learns_everyone_from_the_owner(self) -> None:
        self.joined("casey", "riley")
        everyone = ["jordan@example.com", "casey@example.com", "riley@example.com"]
        self.assertEqual(self.emails("jordan"), everyone)
        self.assertEqual(self.emails("casey"), everyone)
        self.assertEqual(self.emails("riley"), everyone)

    def test_a_member_list_from_someone_else_changes_nothing(self) -> None:
        set_id = self.joined("casey", "riley")
        self.riley.message(OPERATORS["casey"], "set_members", {"set_id": set_id, "members": []})
        self.deliver("casey")
        self.assertEqual(len(self.emails("casey")), 3)

    def test_a_failed_member_list_send_is_applied_on_the_next_pull(self) -> None:
        self.joined("casey")
        set_id = self.jordan.kept()[0].set_id
        self.jordan.invite(set_id, "riley@example.com")
        self.deliver("riley")
        self.riley.answer(self.sent[-1]["id"], accepted=True)
        reply = self.sent[-1]
        with mock.patch.object(Sets, "message", side_effect=agent_inbox.CloudError("relay down")), \
                self.assertRaises(agent_inbox.CloudError):
            self.deliver_message(reply, "jordan")
        self.assertEqual(len(self.emails("jordan")), 2)  # nothing half-written
        self.jordan.apply_inbox()  # the next pull
        self.assertEqual(self.emails("jordan"), ["jordan@example.com", "casey@example.com", "riley@example.com"])

    def test_accepting_a_second_invite_adds_no_second_member(self) -> None:
        set_id = self.joined("casey")
        self.jordan.invite(set_id, "casey@example.com")
        self.deliver("casey")
        self.casey.answer(self.sent[-1]["id"], accepted=True)
        self.deliver("jordan")
        self.assertEqual(self.emails("jordan"), ["jordan@example.com", "casey@example.com"])

    def test_decline_is_shown_to_the_owner_and_keeps_the_set_off_the_invitee(self) -> None:
        self.jordan.create("Founders")
        self.jordan.invite(self.jordan.kept()[0].set_id, "casey@example.com")
        self.deliver("casey")
        self.casey.answer(self.sent[-1]["id"], accepted=False)
        self.assertEqual(self.casey.kept(), [])
        self.deliver("jordan")
        self.assertEqual(payload(self.jordan, 0)["sets"][1]["invited"][0]["status"], "declined")

    def test_owner_delete_reaches_members(self) -> None:
        set_id = self.joined("casey")
        self.jordan.delete(set_id)
        self.assertEqual((self.sent[-1]["to"], self.sent[-1]["kind"]), (OPERATORS["casey"], "set_deleted"))
        self.assertEqual(self.jordan.kept(), [])
        self.deliver("casey")
        self.assertEqual([item["name"] for item in payload(self.casey, 0)["sets"]], ["Personal network"])

    def test_owner_delete_withdraws_a_pending_invite(self) -> None:
        self.jordan.create("Founders")
        set_id = self.jordan.kept()[0].set_id
        self.jordan.invite(set_id, "casey@example.com")
        self.deliver("casey")
        self.assertEqual(len(payload(self.casey, 0)["invites"]), 1)
        self.jordan.delete(set_id)
        self.assertEqual((self.sent[-1]["to"], self.sent[-1]["kind"]), ("casey@example.com", "set_deleted"))
        self.deliver("casey")
        self.assertEqual(payload(self.casey, 0)["invites"], [])

    def test_member_leave_reaches_the_owner(self) -> None:
        set_id = self.joined("casey")
        self.casey.delete(set_id)
        self.assertEqual((self.sent[-1]["to"], self.sent[-1]["kind"]), (OPERATORS["jordan"], "set_left"))
        self.assertEqual(self.casey.kept(), [])
        self.deliver("jordan")
        self.assertEqual(self.emails("jordan"), ["jordan@example.com"])

    def test_no_sign_in_is_said_so(self) -> None:
        with mock.patch.object(agent_inbox, "request", REAL_REQUEST), \
                mock.patch.object(agent_inbox.auth, "bearer_token", side_effect=SystemExit("not signed in")), \
                self.assertRaises(NeedsSignIn):
            self.jordan.create("Founders")


if __name__ == "__main__":
    unittest.main()

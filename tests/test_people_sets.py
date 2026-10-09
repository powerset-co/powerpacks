"""The People page's sets: local only; invites and their answers ride the relay as agent messages."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from packs.ingestion.primitives.common.jsonio import write_json
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.share.web.sets import NeedsSignIn, Sets, payload

ACCOUNTS = {"jordan": {"email": "jordan@example.com", "operator_id": "op-jordan"},
            "casey": {"email": "casey@example.com", "operator_id": "op-casey"},
            "riley": {"email": "riley@example.com", "operator_id": "op-riley"}}
# People each operator shared into share_v1; a set's people are its members' union.
SHARED = {"op-jordan": 5, "op-casey": 5, "op-riley": 5}


class RelayInviteTests(unittest.TestCase):
    """Jordan makes a set and invites Casey by email; Casey accepts; each side reads the other's message."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sent: list[dict] = []
        self.jordan = self.side("jordan")
        self.casey = self.side("casey")
        self.riley = self.side("riley")
        people = mock.patch.object(Sets, "people", lambda sets, ids: sum(SHARED.get(i, 0) for i in ids))
        people.start()
        self.addCleanup(people.stop)

    def side(self, name: str) -> Sets:
        root = Path(self.tmp.name) / name
        (root / ".powerpacks").mkdir(parents=True)
        conn = open_store(root / "store.sqlite")
        self.addCleanup(conn.close)
        sets = Sets(conn, root / ".env")
        sets.account = name
        return sets

    def relay(self, sets: Sets, method: str, path: str, body: dict | None = None):
        if path == "/v2/team/me":
            return ACCOUNTS[sets.account]
        if path == "/v2/agent-messages":
            self.sent.append(body)
            return {"id": f"m{len(self.sent)}", "status": "sent"}
        raise AssertionError(f"unexpected call {method} {path}")

    def deliver(self, to: Sets, sender: tuple[str, str]) -> None:
        """What the asks loop's inbox pull writes: the last relay message, with its sender."""
        message_id = f"m{len(self.sent)}"
        write_json(to.data_root / "inbox" / f"{message_id}.json",
                   {"id": message_id, "kind": self.sent[-1]["kind"], "payload": self.sent[-1]["payload"],
                    "from": {"operator_id": sender[0], "name": sender[1]}, "created_at": "2026-10-08T00:00:00Z"})

    def test_create_invite_accept_round_trip(self) -> None:
        with mock.patch.object(Sets, "_call", lambda sets, *args: self.relay(sets, *args)):
            self.jordan.create("Founders")
            set_id = self.jordan.kept()[0].set_id
            mine = payload(self.jordan, 0)
            self.assertEqual([(s["name"], s["member_count"], s["person_count"]) for s in mine["sets"]],
                             [("Personal network", 1, 0), ("Founders", 1, 5)])

            self.jordan.invite(set_id, "casey@example.com")
            self.assertEqual((self.sent[0]["to"], self.sent[0]["payload"]),
                             ("casey@example.com", {"set_id": set_id, "set_name": "Founders", "from_email": "jordan@example.com"}))
            self.assertEqual(payload(self.jordan, 0)["sets"][1]["invited"],
                             [{"id": "m1", "email": "casey@example.com", "status": "pending"}])

            self.deliver(self.casey, ("op-jordan", "Jordan Bravo"))
            theirs = payload(self.casey, 0)
            self.assertEqual((theirs["invites"][0]["from"], theirs["invites"][0]["from_email"]),
                             ("Jordan Bravo", "jordan@example.com"))
            self.casey.answer("m1", accepted=True)
            self.assertEqual((self.sent[1]["to"], self.sent[1]["payload"]), ("op-jordan", {"invite_id": "m1", "answer": "accepted"}))
            theirs = payload(self.casey, 0)
            self.assertEqual(theirs["invites"], [])
            joined = theirs["sets"][1]
            self.assertEqual((joined["set_id"], joined["role"], joined["person_count"]), (set_id, "member", 10))
            self.assertEqual([m["email"] for m in joined["members"]], ["jordan@example.com", "casey@example.com"])

            self.deliver(self.jordan, ("op-casey", "Casey Delta"))
            mine = payload(self.jordan, 0)
        founders = mine["sets"][1]
        self.assertEqual((founders["member_count"], founders["person_count"], founders["invited"]), (2, 10, []))
        self.assertEqual(founders["members"][-1]["name"], "Casey Delta")
        self.assertEqual([m["person_count"] for m in founders["members"]], [5, 5])
        json.dumps(mine)

    def deliver_to(self, to: Sets, sender: tuple[str, str], index: int) -> None:
        """One relay message, by its place in the sent list, as the asks loop's pull writes it."""
        body = self.sent[index]
        write_json(to.data_root / "inbox" / f"m{index + 1}.json",
                   {"id": f"m{index + 1}", "kind": body["kind"], "payload": body["payload"],
                    "from": {"operator_id": sender[0], "name": sender[1]},
                    "created_at": f"2026-10-08T00:00:{index:02d}Z"})  # the relay stamps them in send order

    def test_a_third_member_learns_everyone_from_the_owner(self) -> None:
        with mock.patch.object(Sets, "_call", lambda sets, *args: self.relay(sets, *args)):
            set_id = self.accepted()  # Jordan owns it, Casey joined
            payload(self.jordan, 0)   # the owner's read sends the list: Jordan + Casey
            self.jordan.invite(set_id, "riley@example.com")
            self.deliver_to(self.riley, ("op-jordan", "Jordan Bravo"), len(self.sent) - 1)
            invite_id = f"m{len(self.sent)}"
            self.riley.answer(invite_id, accepted=True)
            self.deliver_to(self.jordan, ("op-riley", "Riley Echo"), len(self.sent) - 1)
            payload(self.jordan, 0)   # Riley's accept changed the list: sent to Casey and Riley
            lists = [(index, body["to"]) for index, body in enumerate(self.sent) if body["kind"] == "set_members"]
            for index, to in lists:
                self.deliver_to({"op-casey": self.casey, "op-riley": self.riley}[to], ("op-jordan", "Jordan Bravo"), index)
            casey_view = payload(self.casey, 0)["sets"][1]
            riley_view = payload(self.riley, 0)["sets"][1]
        everyone = ["jordan@example.com", "casey@example.com", "riley@example.com"]
        self.assertEqual(sorted(m["email"] for m in casey_view["members"]), sorted(everyone))
        self.assertEqual(sorted(m["email"] for m in riley_view["members"]), sorted(everyone))
        self.assertEqual(casey_view["person_count"], 15)

    def test_a_member_list_from_someone_else_changes_nothing(self) -> None:
        with mock.patch.object(Sets, "_call", lambda sets, *args: self.relay(sets, *args)):
            set_id = self.accepted()
            write_json(self.casey.data_root / "inbox" / "x1.json", {
                "id": "x1", "kind": "set_members", "created_at": "2026-10-08T00:00:00Z",
                "from": {"operator_id": "op-riley", "name": "Riley Echo"},
                "payload": {"set_id": set_id, "members": []}})
            members = payload(self.casey, 0)["sets"][1]["members"]
        self.assertEqual([m["email"] for m in members], ["jordan@example.com", "casey@example.com"])

    def test_decline_keeps_the_set_off_the_invitee(self) -> None:
        with mock.patch.object(Sets, "_call", lambda sets, *args: self.relay(sets, *args)):
            self.jordan.create("Founders")
            self.jordan.invite(self.jordan.kept()[0].set_id, "casey@example.com")
            self.deliver(self.casey, ("op-jordan", "Jordan Bravo"))
            self.casey.answer("m1", accepted=False)
            self.assertEqual(self.casey.kept(), [])
            self.deliver(self.jordan, ("op-casey", "Casey Delta"))
            self.assertEqual(payload(self.jordan, 0)["sets"][1]["invited"][0]["status"], "declined")

    def accepted(self) -> str:
        """Jordan's set with Casey in it, both sides settled; returns the set id."""
        self.jordan.create("Founders")
        set_id = self.jordan.kept()[0].set_id
        self.jordan.invite(set_id, "casey@example.com")
        self.deliver(self.casey, ("op-jordan", "Jordan Bravo"))
        self.casey.answer("m1", accepted=True)
        self.deliver(self.jordan, ("op-casey", "Casey Delta"))
        return set_id

    def test_owner_delete_reaches_members(self) -> None:
        with mock.patch.object(Sets, "_call", lambda sets, *args: self.relay(sets, *args)):
            set_id = self.accepted()
            self.jordan.delete(set_id)
            self.assertEqual((self.sent[-1]["to"], self.sent[-1]["kind"], self.sent[-1]["payload"]),
                             ("op-casey", "set_deleted", {"set_id": set_id}))
            self.assertEqual(self.jordan.kept(), [])
            self.deliver(self.casey, ("op-jordan", "Jordan Bravo"))
            self.assertEqual([s["name"] for s in payload(self.casey, 0)["sets"]], ["Personal network"])

    def test_member_leave_reaches_the_owner(self) -> None:
        with mock.patch.object(Sets, "_call", lambda sets, *args: self.relay(sets, *args)):
            set_id = self.accepted()
            self.casey.delete(set_id)
            self.assertEqual((self.sent[-1]["to"], self.sent[-1]["kind"]), ("op-jordan", "set_left"))
            self.assertEqual(self.casey.kept(), [])
            self.deliver(self.jordan, ("op-casey", "Casey Delta"))
            founders = payload(self.jordan, 0)["sets"][1]
        self.assertEqual(([m["email"] for m in founders["members"]], founders["invited"]), (["jordan@example.com"], []))

    def test_no_sign_in_is_said_so(self) -> None:
        with mock.patch("packs.ingestion.primitives.share.web.sets.bearer_token", side_effect=SystemExit("not signed in")):
            with self.assertRaises(NeedsSignIn):
                self.jordan.create("Founders")


if __name__ == "__main__":
    unittest.main()

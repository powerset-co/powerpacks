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
            "casey": {"email": "casey@example.com", "operator_id": "op-casey"}}
# People each operator shared into share_v1; a set's people are its members' union.
SHARED = {"op-jordan": 5, "op-casey": 5}


class RelayInviteTests(unittest.TestCase):
    """Jordan makes a set and invites Casey by email; Casey accepts; each side reads the other's message."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sent: list[dict] = []
        self.jordan = self.side("jordan")
        self.casey = self.side("casey")
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
        json.dumps(mine)

    def test_decline_keeps_the_set_off_the_invitee(self) -> None:
        with mock.patch.object(Sets, "_call", lambda sets, *args: self.relay(sets, *args)):
            self.jordan.create("Founders")
            self.jordan.invite(self.jordan.kept()[0].set_id, "casey@example.com")
            self.deliver(self.casey, ("op-jordan", "Jordan Bravo"))
            self.casey.answer("m1", accepted=False)
            self.assertEqual(self.casey.kept(), [])
            self.deliver(self.jordan, ("op-casey", "Casey Delta"))
            self.assertEqual(payload(self.jordan, 0)["sets"][1]["invited"][0]["status"], "declined")

    def test_no_sign_in_is_said_so(self) -> None:
        with mock.patch("packs.ingestion.primitives.share.web.sets.bearer_token", side_effect=SystemExit("not signed in")):
            with self.assertRaises(NeedsSignIn):
                self.jordan.create("Founders")


if __name__ == "__main__":
    unittest.main()

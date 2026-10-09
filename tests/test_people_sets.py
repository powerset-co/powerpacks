"""The People page's sets routes: the cloud's answer is kept in the store; create and delete go through the cloud;
invites and their answers ride the relay as agent messages."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from packs.ingestion.primitives.common.jsonio import write_json
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.share.web.sets import NeedsSignIn, Sets, payload

CLOUD = {
    "list": [{"id": "s1", "name": "Personal Connections", "role": "owner", "is_personal": True, "member_count": 2, "person_count": 40}],
    "s1": {"members": [{"name": "Jordan Bravo", "email": "jordan@example.com", "role": "owner"},
                       {"name": "Casey Delta", "email": "casey@example.com", "role": "member"}]},
}


def cloud(self: Sets, method: str, path: str, body: dict | None = None):
    if method == "GET" and path == "/v2/sets":
        return CLOUD["list"]
    if method == "GET" and path in ("/v2/sets/s1", "/v2/sets/s2"):
        return CLOUD[path.rsplit("/", 1)[1]]
    if method == "POST" and path == "/v2/sets":
        CLOUD["list"].append({"id": "s2", "name": body["name"], "role": "owner", "is_personal": False, "member_count": 1, "person_count": 0})
        CLOUD["s2"] = {"members": [{"name": "Jordan Bravo", "email": "jordan@example.com", "role": "owner"}]}
        return {"id": "s2"}
    if method == "DELETE" and path == "/v2/sets/s2":
        CLOUD["list"] = [item for item in CLOUD["list"] if item["id"] != "s2"]
        return None
    raise AssertionError(f"unexpected call {method} {path}")


class SetsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = open_store(Path(self.tmp.name) / "store.sqlite")
        self.addCleanup(self.conn.close)
        self.sets = Sets(self.conn, Path(self.tmp.name) / ".env")

    def test_refresh_keeps_the_cloud_answer_and_create_delete_round_trip(self) -> None:
        with mock.patch.object(Sets, "_call", cloud):
            kept = self.sets.refresh()
            self.assertEqual([(s.name, s.role, s.is_personal, len(s.members)) for s in kept],
                             [("Personal Connections", "owner", True, 2)])
            # Kept in the store: readable without the cloud.
            self.assertEqual([s.set_id for s in self.sets.kept()], ["s1"])
            created = self.sets.create("Founders")
            self.assertEqual([s.name for s in created], ["Personal Connections", "Founders"])
            self.assertEqual([s.name for s in self.sets.delete("s2")], ["Personal Connections"])
        answer = payload(self.sets.kept(), 276, "s1")
        self.assertEqual((answer["shared"], answer["default_set_id"], answer["sets"][0]["members"][1]["name"]),
                         (276, "s1", "Casey Delta"))
        json.dumps(answer)

    def test_no_sign_in_is_said_so(self) -> None:
        with mock.patch("packs.ingestion.primitives.share.web.sets.bearer_token", side_effect=SystemExit("not signed in")):
            with self.assertRaises(NeedsSignIn):
                self.sets.refresh()


class RelayInviteTests(unittest.TestCase):
    """Jordan invites Casey by email; Casey accepts; each side reads the other's message from its inbox."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sent: list[dict] = []
        self.jordan = self.side("jordan")
        self.casey = self.side("casey")

    def side(self, name: str) -> Sets:
        root = Path(self.tmp.name) / name
        (root / ".powerpacks").mkdir(parents=True)
        conn = open_store(root / "store.sqlite")
        self.addCleanup(conn.close)
        return Sets(conn, root / ".env")

    def relay(self, sets: Sets, method: str, path: str, body: dict | None = None):
        if path == "/v2/agent-messages":
            self.sent.append(body)
            return {"id": f"m{len(self.sent)}", "status": "sent"}
        return cloud(sets, method, path, body)

    def deliver(self, to: Sets, sender: tuple[str, str]) -> None:
        """What the asks loop's inbox pull writes: the last relay message, with its sender."""
        message_id = f"m{len(self.sent)}"
        write_json(to.data_root / "inbox" / f"{message_id}.json",
                   {"id": message_id, "kind": self.sent[-1]["kind"], "payload": self.sent[-1]["payload"],
                    "from": {"operator_id": sender[0], "name": sender[1]}, "created_at": "2026-10-08T00:00:00Z"})

    def test_invite_accept_round_trip(self) -> None:
        with mock.patch.object(Sets, "_call", lambda sets, *args: self.relay(sets, *args)), \
                mock.patch("packs.ingestion.primitives.share.web.sets.bearer_token", return_value="token"), \
                mock.patch("packs.ingestion.primitives.share.web.sets._decode_jwt_email", return_value="jordan@example.com"):
            self.jordan.refresh()
            self.jordan.invite("s1", "casey@example.com")
            self.assertEqual(self.sent[0]["to"], "casey@example.com")
            mine = payload(self.jordan.kept(), 0, "", sent=self.jordan.sent())
            self.assertEqual(mine["sets"][0]["invited"], [{"id": "m1", "email": "casey@example.com", "status": "pending"}])

            self.deliver(self.casey, ("op-jordan", "Jordan Bravo"))
            theirs = payload([], 0, "", received=self.casey.received())
            self.assertEqual((theirs["invites"][0]["from"], theirs["invites"][0]["from_email"]),
                             ("Jordan Bravo", "jordan@example.com"))
            self.casey.answer("m1", accepted=True)
            self.assertEqual((self.sent[1]["to"], self.sent[1]["payload"]), ("op-jordan", {"invite_id": "m1", "answer": "accepted"}))
            theirs = payload([], 0, "", received=self.casey.received(), seen={"op-jordan": "2026-10-08T01:00:00Z"})
            self.assertEqual(theirs["invites"], [])
            self.assertEqual([(item["name"], item["members"][0]["last_seen_at"]) for item in theirs["sets"]],
                             [("Personal Connections", "2026-10-08T01:00:00Z")])

            self.deliver(self.jordan, ("op-casey", "Casey Delta"))
        mine = payload(self.jordan.kept(), 0, "", sent=self.jordan.sent(), seen={"op-casey": "2026-10-08T02:00:00Z"})
        self.assertEqual(mine["sets"][0]["invited"], [])
        self.assertEqual(mine["sets"][0]["members"][-1]["name"], "Casey Delta")
        self.assertEqual(mine["sets"][0]["members"][-1]["last_seen_at"], "2026-10-08T02:00:00Z")


if __name__ == "__main__":
    unittest.main()

"""The People page's sets routes: the cloud's answer is kept in the store; create and delete go through the cloud."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()

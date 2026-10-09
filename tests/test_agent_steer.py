"""Check steer messages: send, the open inbox, done, and replies, with the relay mocked.

Changelog:
- 2026-10-08: created.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.powerset.primitives.agent_steer import agent_steer

ENV_FILE = Path("/synthetic/.env")
SENDER = {"operator_id": "op-arthur", "name": "Arthur"}


def _message(message_id: str, kind: str, payload: dict, created_at: str, **extra) -> dict:
    return {"id": message_id, "kind": kind, "payload": payload, "from": SENDER,
            "created_at": created_at, **extra}


class AgentSteerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inbox = self.root / ".powerpacks" / "inbox"
        self.inbox.mkdir(parents=True)

    def _write(self, message: dict) -> None:
        (self.inbox / f"{message['id']}.json").write_text(json.dumps(message), encoding="utf-8")

    def test_send_posts_a_steer(self):
        with patch.object(agent_steer, "_post", return_value={"id": "m1", "status": "queued"}) as post:
            result = agent_steer.send(env_file=ENV_FILE, to="jake@example.com", text="run the doctor")
        post.assert_called_once_with(ENV_FILE, {"to": "jake@example.com", "kind": "steer",
                                                "payload": {"text": "run the doctor"}})
        self.assertEqual(result["id"], "m1")

    def test_inbox_lists_only_open_steers_oldest_first(self):
        self._write(_message("s2", "steer", {"text": "second"}, "2026-10-08T02:00:00Z"))
        self._write(_message("s1", "steer", {"text": "first"}, "2026-10-08T01:00:00Z"))
        self._write(_message("s0", "steer", {"text": "handled"}, "2026-10-08T00:00:00Z",
                             done_at="2026-10-08T00:30:00Z", note="ok"))
        self._write(_message("i1", "set_invite", {"set_id": "x"}, "2026-10-08T00:00:00Z"))
        listed = agent_steer.inbox(repo_root=self.root)
        self.assertEqual([row["id"] for row in listed], ["s1", "s2"])
        self.assertEqual(listed[0], {"id": "s1", "from": "Arthur", "created_at": "2026-10-08T01:00:00Z",
                                     "text": "first"})

    def test_done_marks_the_file_and_tells_the_sender(self):
        self._write(_message("s1", "steer", {"text": "run the doctor"}, "2026-10-08T01:00:00Z"))
        with patch.object(agent_steer, "_post", return_value={"id": "r1", "status": "queued"}) as post:
            agent_steer.done(repo_root=self.root, env_file=ENV_FILE, steer_id="s1", note="doctor is green")
        post.assert_called_once_with(ENV_FILE, {"to": "op-arthur", "kind": "steer_done",
                                                "payload": {"steer_id": "s1", "note": "doctor is green"}})
        saved = json.loads((self.inbox / "s1.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["note"], "doctor is green")
        self.assertIn("done_at", saved)
        self.assertEqual(agent_steer.inbox(repo_root=self.root), [])

    def test_replies_lists_steer_done_newest_first(self):
        self._write(_message("r1", "steer_done", {"steer_id": "s1", "note": "older"}, "2026-10-08T01:00:00Z"))
        self._write(_message("r2", "steer_done", {"steer_id": "s2", "note": "newer"}, "2026-10-08T02:00:00Z"))
        self._write(_message("s3", "steer", {"text": "not a reply"}, "2026-10-08T03:00:00Z"))
        listed = agent_steer.replies(repo_root=self.root)
        self.assertEqual([row["id"] for row in listed], ["r2", "r1"])
        self.assertEqual(listed[0], {"id": "r2", "from": "Arthur", "created_at": "2026-10-08T02:00:00Z",
                                     "steer_id": "s2", "note": "newer"})


if __name__ == "__main__":
    unittest.main()

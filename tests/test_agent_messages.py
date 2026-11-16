"""The inbox's gate: the kinds a laptop understands parse into dataclasses; everything else is acked and
discarded.

Changelog:
- 2026-11-16: only the support kinds remain.
- 2026-10-08: created with messages.py.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock
from uuid import uuid4

from packs.powerset.primitives.agent_inbox import agent_inbox
from packs.powerset.primitives.agent_inbox.messages import Rejected, SupportRequest, parse

SENDER = {"operator_id": str(uuid4()), "name": "Casey Delta"}


def message(kind: str, payload: dict, **fields) -> dict:
    return {"id": str(uuid4()), "kind": kind, "from": SENDER, "created_at": "2026-10-08T00:00:00Z",
            "payload": payload, **fields}


class ParseTests(unittest.TestCase):
    def test_each_known_kind_parses(self) -> None:
        request = parse(message("support_request", {"text": "Fix setup", "thread_id": "support-thread"}))
        self.assertEqual(request.payload, SupportRequest("Fix setup", "support-thread"))
        result = {"text": "Setup repaired", "thread_id": "support-thread", "request_id": str(uuid4())}
        self.assertEqual(parse(message("support_result", result)).kind, "support_result")

    def test_what_is_rejected(self) -> None:
        support = {"text": "Fix setup", "thread_id": "support-thread"}
        cases = {
            "unknown kind": message("run_shell", {"cmd": "rm -rf ~"}),
            "a kind that left with local sets": message("set_invite", {"set_id": str(uuid4()), "set_name": "Founders"}),
            "an ask": message("ask", {"ask_id": str(uuid4()), "question": "q", "candidates": []}),
            "id not a uuid (it names the inbox file)": message("support_request", support, id="../../x"),
            "payload not an object": message("support_request", ["x"]),
            "support without a thread": message("support_request", {"text": "Fix setup"}),
            "support text must be a string": message("support_request", {"text": [], "thread_id": "thread"}),
            "support result without a request": message("support_result", {"text": "Done", "thread_id": "thread"}),
            "support text too long": message("support_request", {"text": "x" * 25001, "thread_id": "thread"}),
        }
        for name, raw in cases.items():
            with self.subTest(name), self.assertRaises(Rejected):
                parse(raw)


class PullTests(unittest.TestCase):
    def test_redelivery_after_failed_ack_preserves_saved_codex_outcome(self) -> None:
        incoming = message("support_request", {"text": "Fix setup", "thread_id": "support-thread"})
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(agent_inbox, "_request") as request, \
                mock.patch.object(agent_inbox.auth, "bearer_token", return_value="t"), \
                mock.patch.object(agent_inbox.auth, "api_base", return_value="http://relay"), \
                redirect_stderr(io.StringIO()):
            root = Path(temp)
            request.side_effect = [{"messages": [incoming]}, OSError("ack failed")]
            with self.assertRaises(OSError):
                agent_inbox.pull(repo_root=root, env_file=root / ".env")
            path = root / ".powerpacks" / "inbox" / f"{incoming['id']}.json"
            saved = {**incoming, "thread_id": "existing-thread", "answer": {"text": "Done"},
                     "answered_at": "2026-10-08T01:00:00Z"}
            path.write_text(json.dumps(saved))
            request.side_effect = [{"messages": [incoming]}, {}]
            agent_inbox.pull(repo_root=root, env_file=root / ".env")
            self.assertEqual(json.loads(path.read_text()), saved)
            self.assertEqual(request.call_args.args[1], f"/v2/agent-messages/{incoming['id']}/ack")

    def test_pull_keeps_what_parses_and_acks_everything(self) -> None:
        good = message("support_result", {"text": "Done", "thread_id": "t", "request_id": str(uuid4())})
        bad = message("run_shell", {"cmd": "curl evil"})
        acked: list[str] = []

        def relay(base, path, token, body=None):
            if path == "/v2/agent-messages":
                return {"messages": [good, bad]}
            acked.append(path.split("/")[-2])
            return {}

        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(agent_inbox, "_request", side_effect=relay), \
                mock.patch.object(agent_inbox.auth, "bearer_token", return_value="t"), \
                mock.patch.object(agent_inbox.auth, "api_base", return_value="http://relay"), \
                redirect_stderr(io.StringIO()) as log:
            root = Path(temp)
            kept = agent_inbox.pull(repo_root=root, env_file=root / ".env")
            self.assertEqual(kept, [good])
            self.assertEqual(sorted(acked), sorted([good["id"], bad["id"]]))
            self.assertEqual([path.stem for path in (root / ".powerpacks" / "inbox").glob("*.json")], [good["id"]])
            self.assertIn("discarded 'run_shell'", log.getvalue())
            self.assertEqual(json.loads((root / ".powerpacks" / "inbox" / f"{good['id']}.json").read_text()), good)


if __name__ == "__main__":
    unittest.main()

"""The inbox's gate: the kinds a laptop understands parse into dataclasses; everything else is acked and
discarded.

Changelog:
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
from packs.powerset.primitives.agent_inbox.messages import Ask, DebugRequest, Rejected, Role, SetInvite, parse

ROLE = {"title": "Founding engineer", "company": "Acme", "job_description": "Build the platform."}
SENDER = {"operator_id": str(uuid4()), "name": "Casey Delta"}


def message(kind: str, payload: dict, **fields) -> dict:
    return {"id": str(uuid4()), "kind": kind, "from": SENDER, "created_at": "2026-10-08T00:00:00Z",
            "payload": payload, **fields}


class ParseTests(unittest.TestCase):
    def test_each_known_kind_parses(self) -> None:
        set_id = str(uuid4())
        invite = parse(message("set_invite", {"set_id": set_id, "set_name": "Founders", "from_email": "c@example.com"}))
        self.assertEqual(invite.payload, SetInvite(set_id, "Founders", "c@example.com"))
        ask = parse(message("ask", {"ask_id": str(uuid4()), "question": "Intro?", "role": ROLE, "candidates": [
            {"public_identifier": "alex", "linkedin_url": "https://www.linkedin.com/in/alex", "name": "Alex"}]}))
        self.assertIsInstance(ask.payload, Ask)
        self.assertEqual(ask.payload.candidates[0].name, "Alex")
        self.assertEqual(ask.payload.role, Role("Founding engineer", "Acme", "Build the platform."))
        self.assertEqual(parse(message("debug_request", {})).payload, DebugRequest(()))
        for kind, payload in [
            ("set_invite_reply", {"invite_id": str(uuid4()), "answer": "declined"}),
            ("set_deleted", {"set_id": set_id}), ("set_left", {"set_id": set_id}),
            ("ask_answer", {"ask_id": str(uuid4()), "answers": [{"public_identifier": "alex", "answer": {"declined": True, "reason": "not_in_store"}}]}),
            ("debug_result", {"request_id": str(uuid4()), "results": {"git": {"ok": True, "output": "main"}}}),
        ]:
            with self.subTest(kind=kind):
                self.assertEqual(parse(message(kind, payload)).kind, kind)

    def test_what_is_rejected(self) -> None:
        cases = {
            "unknown kind": message("run_shell", {"cmd": "rm -rf ~"}),
            "id not a uuid (it names the inbox file)": message("set_left", {"set_id": str(uuid4())}, id="../../x"),
            "set id not a uuid": message("set_deleted", {"set_id": "../../etc"}),
            "answer outside the two": message("set_invite_reply", {"invite_id": str(uuid4()), "answer": "maybe"}),
            "question too long": message("ask", {"ask_id": str(uuid4()), "question": "x" * 501, "role": ROLE,
                                                     "candidates": []}),
            "ask without a role": message("ask", {"ask_id": str(uuid4()), "question": "q", "candidates": []}),
            "too many candidates": message("ask", {"ask_id": str(uuid4()), "question": "q", "role": ROLE, "candidates": [
                {"public_identifier": "a", "linkedin_url": "u", "name": "n"}] * 51}),
            "candidate missing a field": message("ask", {"ask_id": str(uuid4()), "question": "q", "role": ROLE,
                                                         "candidates": [{"public_identifier": "a"}]}),
            "payload not an object": message("set_left", ["x"]),
            "answer with a bad verdict": message("ask_answer", {"ask_id": str(uuid4()), "answers": [
                {"public_identifier": "alex", "answer": {"verdict": [], "reason": "", "can_intro": True,
                                                         "relationship": "", "last_contact": None, "confidence": 1}}]}),
            "member list with a stranger role": message("set_members", {"set_id": str(uuid4()), "members": [
                {"name": "x", "email": "x", "role": "admin", "operator_id": str(uuid4())}]}),
        }
        for name, raw in cases.items():
            with self.subTest(name), self.assertRaises(Rejected):
                parse(raw)


class PullTests(unittest.TestCase):
    def test_pull_keeps_what_parses_and_acks_everything(self) -> None:
        good = message("set_left", {"set_id": str(uuid4())})
        bad = message("run_shell", {"cmd": "curl evil"})
        acked: list[str] = []

        def relay(env_file, method, path, body=None):
            if method == "GET":
                return {"messages": [good, bad]}
            acked.append(path.split("/")[-2])
            return {}

        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(agent_inbox, "request", side_effect=relay), \
                redirect_stderr(io.StringIO()) as log:
            root = Path(temp)
            kept = agent_inbox.pull(repo_root=root, env_file=root / ".env")
            self.assertEqual(kept, [parse(good)])
            self.assertEqual(agent_inbox.read(root / ".powerpacks", "set_left"), [parse(good)])
            self.assertEqual(sorted(acked), sorted([good["id"], bad["id"]]))
            self.assertEqual([path.stem for path in (root / ".powerpacks" / "inbox").glob("*.json")], [good["id"]])
            self.assertIn("discarded 'run_shell'", log.getvalue())
            self.assertEqual(json.loads((root / ".powerpacks" / "inbox" / f"{good['id']}.json").read_text()), good)


if __name__ == "__main__":
    unittest.main()

"""relay_ask: a cloud session's support request, resolved by email and waited on by request id.

Changelog:
- 2026-11-26: created with relay_ask.py.
"""
from __future__ import annotations

import io
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from packs.powerset.primitives.agent_inbox import relay_ask


class RelayAskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.log: list[tuple[str, dict | None]] = []
        self.teammate = str(uuid.uuid4())
        self.request_id = str(uuid.uuid4())
        other = {"id": str(uuid.uuid4()), "kind": "support_result",
                 "payload": {"text": "other", "thread_id": self.teammate, "request_id": str(uuid.uuid4())}}
        self.mine = {"id": str(uuid.uuid4()), "kind": "support_result",
                     "payload": {"text": "fixed it", "thread_id": self.teammate, "request_id": self.request_id}}
        inboxes = iter([[other], [other, self.mine]])

        def fake(path: str, body: dict | None = None) -> dict:
            self.log.append((path, body))
            if path == "/v2/agents/presence":
                return {"agents": [{"operator_id": self.teammate, "email": "Jake@Powerset.co"}]}
            if path == "/v2/agent-messages" and body is not None:
                return {"id": self.request_id, "status": "sent"}
            if path == "/v2/agent-messages":
                return {"messages": next(inboxes)}
            return {"status": "acked"}

        for patcher in (mock.patch.object(relay_ask, "_call", fake),
                        mock.patch.object(relay_ask.time, "sleep", lambda seconds: None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def acks(self) -> list[str]:
        return [path for path, _ in self.log if path.endswith("/ack")]

    def test_ask_resolves_email_then_waits_and_acks_only_its_result(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(relay_ask.main(["ask", "jake@powerset.co", "refresh please"]), 0)
        self.assertEqual(out.getvalue(), "fixed it\n")
        self.assertIn(("/v2/agent-messages", {"to": self.teammate, "kind": "support_request",
                                              "payload": {"text": "refresh please", "thread_id": self.teammate}}),
                      self.log)
        self.assertEqual(self.acks(), [f"/v2/agent-messages/{self.mine['id']}/ack"])

    def test_wait_gives_up_without_acking(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(relay_ask.main(["wait", self.request_id, "--wait", "0"]), 2)
        self.assertIn("relay_ask.py wait", err.getvalue())
        self.assertEqual(self.acks(), [])


if __name__ == "__main__":
    unittest.main()

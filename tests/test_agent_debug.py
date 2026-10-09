"""Check the read-only remote diagnostics against a synthetic install; the relay POST is mocked.

Changelog:
- 2026-10-08: cover a full request, a subset, an unknown check, output capping and `show`.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.common.jsonio import write_json
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.powerset.primitives.agent_debug import agent_debug

ARTHUR = "6f1b3c0e-0d53-4a8e-9c1e-2b7f6a1d9e10"


def _request(checks: list[str] | None = None) -> dict:
    return {"id": "req-1", "kind": agent_debug.REQUEST, "payload": {"checks": checks} if checks else {},
            "from": {"operator_id": ARTHUR, "name": "Arthur"}, "created_at": "2026-10-08T00:00:00Z"}


class AgentDebugTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.email=t@example.com", "-c", "user.name=T",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        write_json(self.root / ".release-please-manifest.json", {".": "3.21.2"})
        data = self.root / ".powerpacks"
        write_json(data / "install" / "manifest.json",
                   {"status": "failed", "step": "people", "message": "boom", "fingerprints": {"x": "y"}})
        (data / "install" / "server.log").write_text("started\nserving\n", encoding="utf-8")
        write_json(data / "upload-powerset" / "manifest.json",
                   {"status": "completed", "stage": "completed", "person_hashes": {"p1": "h1"}})
        (data / "device-id").write_text("device-1\n", encoding="utf-8")
        write_json(data / "presence.json", {ARTHUR: "2026-10-08T00:00:00Z"})
        write_json(data / "inbox" / "req-1.json", _request())
        open_store(data / "deep-context" / "deep-context-v2.sqlite").close()
        self.sent = []
        sender = patch.object(agent_debug, "_send",
                              side_effect=lambda env_file, to, kind, payload: self.sent.append((to, kind, payload))
                              or {"id": "res-1", "status": "queued"})
        sender.start()
        self.addCleanup(sender.stop)

    def _answer(self, checks: list[str] | None = None) -> dict:
        agent_debug.answer(_request(checks), repo_root=self.root, env_file=self.root / ".env")
        self.assertEqual(len(self.sent), 1)
        to, kind, payload = self.sent[0]
        self.assertEqual((to, kind, payload["request_id"]), (ARTHUR, agent_debug.RESULT, "req-1"))
        return payload["results"]

    def test_request_runs_every_check_and_answers_the_sender(self):
        results = self._answer()
        self.assertEqual(set(results), set(agent_debug.CHECKS))
        self.assertTrue(all(result["ok"] for result in results.values()), results)
        self.assertEqual(results["version"]["output"], "3.21.2")
        self.assertEqual(len(json.loads(results["git"]["output"])["commit"]), 40)
        self.assertEqual(json.loads(results["install"]["output"])["step"], "people")
        self.assertNotIn("fingerprints", results["install"]["output"])
        self.assertNotIn("person_hashes", results["upload"]["output"])
        store = json.loads(results["store"]["output"])
        self.assertEqual(store["schema_version"], str(agent_debug.SCHEMA_VERSION))
        self.assertIn("meta", store["rows"])
        self.assertEqual(results["server_log"]["output"], "started\nserving")
        self.assertEqual(json.loads(results["relay"]["output"])["inbox"], {agent_debug.REQUEST: 1})

    def test_subset_runs_only_the_named_checks(self):
        results = self._answer(["install", "server_log"])
        self.assertEqual(set(results), {"install", "server_log"})

    def test_unknown_and_failing_checks_report_not_ok(self):
        (self.root / ".powerpacks" / "upload-powerset" / "manifest.json").unlink()
        results = self._answer(["rm -rf /", "upload"])
        self.assertEqual(results["rm -rf /"], {"ok": False, "output": "unknown check"})
        self.assertFalse(results["upload"]["ok"])
        self.assertIn("FileNotFoundError", results["upload"]["output"])

    def test_output_keeps_the_tail_under_the_cap(self):
        lines = [f"line {index:05d} " + "x" * 200 for index in range(agent_debug.LOG_LINES)]
        (self.root / ".powerpacks" / "install" / "server.log").write_text("\n".join(lines), encoding="utf-8")
        output = self._answer(["server_log"])["server_log"]["output"]
        self.assertEqual(len(output), agent_debug.OUTPUT_CAP)
        self.assertTrue(output.endswith(lines[-1]))

    def test_show_finds_the_result_by_request_id(self):
        inbox = self.root / ".powerpacks" / "inbox"
        result = {"id": "res-1", "kind": agent_debug.RESULT, "payload": {"request_id": "req-1", "results": {}},
                  "from": {"operator_id": "jake", "name": "Jake"}, "created_at": "2026-10-08T00:00:00Z"}
        write_json(inbox / "res-1.json", result)
        self.assertEqual(agent_debug.find_result(inbox, "req-1"), result)
        self.assertIsNone(agent_debug.find_result(inbox, "req-2"))


if __name__ == "__main__":
    unittest.main()

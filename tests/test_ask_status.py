"""Check ask status persistence and printed owner answers with synthetic HTTP.

Changelog:
- 2026-10-08: cover ask status fetch, owner states, and CLI.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as auth
from packs.search.primitives.ask_status import ask_status


class AskStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = Path(self.temp.name) / "example-engineer"
        self.run_dir.mkdir()
        self.env_file = Path(self.temp.name) / ".env"
        self.ask = {"ask_id": "00000000-0000-4000-8000-000000000001", "question": "Who can build systems?"}
        self.run_dir.joinpath("ask.json").write_text(json.dumps(self.ask), encoding="utf-8")
        self.body = {
            **self.ask, "created_at": "2026-10-08T00:00:00Z", "pending": 2,
            "candidates": [{
                "public_identifier": "jordan-bravo-1a2b", "name": "Jordan Bravo", "owners": [
                    {"operator_id": "operator-one", "name": "Casey Alpha", "status": "answered",
                     "awake": False, "answer": {
                         "verdict": "recommend", "reason": "Builds systems.", "can_intro": True,
                         "relationship": "Former colleague", "last_contact": "2026-09", "confidence": 0.9,
                     }},
                    {"operator_id": "operator-two", "name": "Taylor Echo", "status": "leased",
                     "awake": True, "answer": None},
                    {"operator_id": "operator-three", "name": "Morgan Delta", "status": "submitted",
                     "awake": False, "answer": None},
                ],
            }, {"public_identifier": "avery-charlie-3c4d", "name": "Avery Charlie", "owners": []}],
        }

    def test_fetch_uses_existing_auth_writes_answers_and_prints_owner_states(self):
        response = io.BytesIO(json.dumps(self.body).encode())
        before = self.run_dir.joinpath("ask.json").read_bytes()
        with patch.object(auth, "bearer_token", return_value="synthetic-token") as token, \
                patch.object(auth, "api_base", return_value="https://api.example.com") as base, \
                patch.object(ask_status.urllib.request, "urlopen", return_value=response) as urlopen, \
                patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = ask_status.run(self.run_dir, env_file=self.env_file)
        token.assert_called_once_with(self.env_file)
        base.assert_called_once_with(self.env_file)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, f"https://api.example.com/v2/asks/{self.ask['ask_id']}")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-token")
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 30})
        self.assertEqual(result, self.body)
        self.assertEqual(json.loads(self.run_dir.joinpath("ask-answers.json").read_text()), self.body)
        self.assertEqual(self.run_dir.joinpath("ask.json").read_bytes(), before)
        self.assertEqual(stdout.getvalue().splitlines(), [
            "Jordan Bravo: Casey Alpha: answered recommend, Taylor Echo: pending, Morgan Delta: offline",
            "Avery Charlie",
        ])

    def test_all_verdicts_print_as_answered_even_when_owner_is_offline(self):
        for verdict in ("recommend", "not_fit", "unsure"):
            with self.subTest(verdict=verdict):
                self.body["candidates"][0]["owners"][0]["answer"]["verdict"] = verdict
                response = io.BytesIO(json.dumps(self.body).encode())
                with patch.object(auth, "bearer_token", return_value="synthetic-token"), \
                        patch.object(auth, "api_base", return_value="https://api.example.com"), \
                        patch.object(ask_status.urllib.request, "urlopen", return_value=response), \
                        patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    ask_status.run(self.run_dir, env_file=self.env_file)
                self.assertIn(f"Casey Alpha: answered {verdict}", stdout.getvalue())

    def test_failed_fetch_does_not_write_answers(self):
        error = urllib.error.HTTPError("https://api.example.com", 404, "not found", {}, None)
        with patch.object(auth, "bearer_token", return_value="synthetic-token"), \
                patch.object(auth, "api_base", return_value="https://api.example.com"), \
                patch.object(ask_status.urllib.request, "urlopen", side_effect=error), \
                self.assertRaises(urllib.error.HTTPError):
            ask_status.run(self.run_dir, env_file=self.env_file)
        self.assertFalse(self.run_dir.joinpath("ask-answers.json").exists())

    def test_cli_passes_run_dir_and_env_file(self):
        with patch.object(ask_status, "run", return_value=self.body) as run:
            result = ask_status.main(["--run-dir", str(self.run_dir), "--env-file", str(self.env_file)])
        self.assertEqual(result, 0)
        run.assert_called_once_with(self.run_dir, env_file=self.env_file)


if __name__ == "__main__":
    unittest.main()

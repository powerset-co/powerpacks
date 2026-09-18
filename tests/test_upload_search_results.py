"""Private hosted snapshots use the existing login and never modify local labels."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as auth
from packs.powerset.primitives.send_feedback import send_feedback as http
from packs.search.primitives.deep_search.results_web import snapshot
from packs.search.primitives.upload_search_results.upload_search_results import UploadSearchResults, main


class UploadSearchResultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = Path(self.temp.name) / "example-engineer"
        self.run_dir.mkdir()
        self.env_file = Path(self.temp.name) / ".env"
        self.response = {"id": "snapshot-id", "url": "https://search.powerset.dev/local-searches/snapshot-id",
                         "sharing_enabled": False}

    def test_signed_out_is_quiet_and_does_not_export_or_upload(self):
        with patch.object(auth, "bearer_token", side_effect=SystemExit("sign in")), \
                patch.object(snapshot, "export_snapshot") as export, \
                patch.object(http, "post_json") as post:
            self.assertEqual(UploadSearchResults(self.run_dir).run(), {"status": "needs_auth"})
        export.assert_not_called()
        post.assert_not_called()

    def test_upload_uses_existing_auth_and_only_the_render_snapshot(self):
        rendered = {"search": {"run_id": self.run_dir.name}}
        with patch.object(auth, "bearer_token", return_value="synthetic-token") as token, \
                patch.object(auth, "api_base", return_value="https://api.example.com") as base, \
                patch.object(snapshot, "export_snapshot", return_value=rendered) as export, \
                patch.object(http, "post_json", return_value=(200, self.response)) as post:
            result = UploadSearchResults(self.run_dir, env_file=self.env_file).run()
        token.assert_called_once_with(self.env_file)
        base.assert_called_once_with(self.env_file)
        export.assert_called_once_with(self.run_dir)
        post.assert_called_once_with("https://api.example.com", "/v2/local-searches", "synthetic-token",
                                     {"source_run_id": self.run_dir.name, "snapshot": rendered}, timeout=120)
        self.assertEqual(result, {"status": "uploaded", **self.response})

    def test_auth_rejection_and_network_failure_are_not_success(self):
        cases = [
            (urllib.error.HTTPError("https://api.example.com", 401, "unauthorized", {}, None), "needs_auth"),
            (urllib.error.HTTPError("https://api.example.com", 403, "forbidden", {}, None), "failed"),
            (urllib.error.HTTPError("https://api.example.com", 503, "unavailable", {}, None), "failed"),
            (urllib.error.URLError("offline"), "failed"),
            (TimeoutError(), "failed"),
        ]
        for error, status in cases:
            with self.subTest(error=error), \
                    patch.object(auth, "bearer_token", return_value="synthetic-token"), \
                    patch.object(snapshot, "export_snapshot", return_value={"search": {"run_id": "example"}}), \
                    patch.object(http, "post_json", side_effect=error):
                self.assertEqual(UploadSearchResults(self.run_dir).run()["status"], status)

    def test_cli_needs_auth_exits_successfully_without_login_prompt(self):
        with patch.object(auth, "bearer_token", side_effect=SystemExit("sign in")), \
                patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = main(["--run-dir", str(self.run_dir)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"status": "needs_auth"})

    def test_repeated_upload_includes_current_labels_without_changing_local_files(self):
        self.run_dir.joinpath("results.json").write_text(json.dumps({
            "title": "Engineer", "company": "Example", "created_at": "2026-09-18T00:00:00Z",
            "iterations": [], "summary": {"groups": {"send_worthy": [{
                "person": "person-one", "name": "Jordan Bravo", "why": "Builds systems.",
                "found_by": [], "rerank_score": 0.8,
            }]}},
        }))
        labels = self.run_dir / "fit-labels.jsonl"
        labels.write_text(json.dumps({"person_id": "person-one", "human": {"score": 4, "scale": 5, "note": "Review"}}) + "\n")
        before = {path.name: path.read_bytes() for path in self.run_dir.iterdir()}
        with patch.object(auth, "bearer_token", return_value="synthetic-token"), \
                patch.object(http, "post_json", return_value=(200, self.response)) as post:
            first = UploadSearchResults(self.run_dir).run()
            second = UploadSearchResults(self.run_dir).run()
        self.assertEqual(first, second)
        self.assertEqual(post.call_args_list[0], post.call_args_list[1])
        uploaded = post.call_args.args[3]["snapshot"]
        candidate = uploaded["search"]["candidates"][0]
        self.assertEqual((candidate["human_score"], candidate["human_note"]), (4, "Review"))
        self.assertNotIn(str(self.run_dir), json.dumps(uploaded))
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.run_dir.iterdir()})


if __name__ == "__main__":
    unittest.main()

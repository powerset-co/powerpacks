"""Verify snapshot uploads and shortlist asks with synthetic data and stubbed HTTP.

Changelog:
- 2026-10-08: Cover ask payloads, LinkedIn forms, skipped rows, and saved asks.
"""

from __future__ import annotations

import gzip
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as auth
from packs.search.primitives.deep_search.results_web import snapshot
from packs.search.primitives.upload_search_results import upload_search_results as upload
from packs.search.primitives.upload_search_results.upload_search_results import UploadSearchResults, main
from packs.shared.csv_io import CsvIO


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
                patch.object(upload, "post_gzip_json") as post:
            self.assertEqual(UploadSearchResults(self.run_dir).run(), {"status": "needs_auth"})
        export.assert_not_called()
        post.assert_not_called()

    def test_signed_out_ask_does_not_read_shortlist_resolve_set_or_upload(self):
        with patch.object(auth, "bearer_token", side_effect=SystemExit("sign in")), \
                patch.object(CsvIO, "read_dict_rows") as read, \
                patch.object(upload.pg, "fetch_default_set_id") as resolve, \
                patch.object(upload, "post_gzip_json") as post:
            result = UploadSearchResults(self.run_dir, ask="Who fits?").run()
        self.assertEqual(result, {"status": "needs_auth"})
        read.assert_not_called()
        resolve.assert_not_called()
        post.assert_not_called()
        self.assertFalse((self.run_dir / "ask.json").exists())

    def test_ask_cli_posts_shortlist_and_saves_response(self):
        urls = [
            "https://www.linkedin.com/in/jordan-bravo-1a2b/",
            "http://linkedin.com/in/JORDAN-BRAVO-2a3b?trk=search",
            "linkedin.com/in/jordan-bravo-3a4b#about",
            "www.linkedin.com/in/jordan-bravo-4a5b/",
            "https://uk.linkedin.com/in/jordan%2Dbravo-5a6b/?trk=search#about",
        ]
        rows = [{"Rank": 1, "Name": "Casey Example", "LinkedIn URL": ""}]
        rows.extend({"Rank": rank, "Name": "Jordan Bravo", "LinkedIn URL": url}
                    for rank, url in enumerate(urls, start=2))
        rows.append({"Rank": 7, "Name": "Casey Example", "LinkedIn URL": " "})
        CsvIO.write_dict_rows(self.run_dir / "shortlist.csv", ["Rank", "Name", "LinkedIn URL"], rows)
        slugs = [f"jordan-bravo-{suffix}" for suffix in ("1a2b", "2a3b", "3a4b", "4a5b", "5a6b")]
        ask = {"ask_id": "00000000-0000-4000-8000-000000000001", "candidates": [
            {"public_identifier": slug, "owners": ["owner-one", "owner-two"] if index == 0 else []}
            for index, slug in enumerate(slugs)
        ]}
        rendered = {"search": {"run_id": self.run_dir.name}}
        set_id = "00000000-0000-4000-8000-000000000002"
        with patch.object(auth, "bearer_token", return_value="synthetic-token"), \
                patch.object(auth, "api_base", return_value="https://api.example.com"), \
                patch.object(snapshot, "export_snapshot", return_value=rendered), \
                patch.object(upload.pg, "fetch_default_set_id", return_value={"set_id": set_id}) as resolve, \
                patch.object(upload, "post_gzip_json", return_value={**self.response, "ask": ask}) as post, \
                patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            code = main(["--run-dir", str(self.run_dir), "--env-file", str(self.env_file),
                         "--ask", "Who fits?"])
        self.assertEqual(code, 0)
        resolve.assert_called_once_with(env_file=self.env_file)
        post.assert_called_once_with("https://api.example.com", "/v2/local-searches", "synthetic-token", {
            "source_run_id": self.run_dir.name, "snapshot": rendered,
            "ask": {"question": "Who fits?", "set_id": set_id, "candidates": [
                {"public_identifier": slug, "linkedin_url": url, "name": "Jordan Bravo", "local_rank": rank}
                for rank, (slug, url) in enumerate(zip(slugs, urls), start=2)
            ]},
        }, timeout=120)
        self.assertEqual(json.loads((self.run_dir / "ask.json").read_text()), {**ask, "question": "Who fits?"})
        self.assertEqual(stderr.getvalue(), "asked 5 candidates, 2 skipped without LinkedIn, owners found for 1\n")
        self.assertEqual(json.loads(stdout.getvalue())["status"], "uploaded")

    def test_ask_without_default_set_does_not_upload(self):
        CsvIO.write_dict_rows(self.run_dir / "shortlist.csv", ["Rank", "Name", "LinkedIn URL"], [])
        with patch.object(auth, "bearer_token", return_value="synthetic-token"), \
                patch.object(snapshot, "export_snapshot", return_value={"search": {"run_id": "example"}}), \
                patch.object(upload.pg, "fetch_default_set_id", return_value={"set_id": None}), \
                patch.object(upload, "post_gzip_json") as post:
            result = UploadSearchResults(self.run_dir, ask="Who fits?").run()
        self.assertEqual(result["status"], "failed")
        post.assert_not_called()
        self.assertFalse((self.run_dir / "ask.json").exists())

    def test_upload_uses_existing_auth_and_only_the_render_snapshot(self):
        rendered = {"search": {"run_id": self.run_dir.name}}
        with patch.object(auth, "bearer_token", return_value="synthetic-token") as token, \
                patch.object(auth, "api_base", return_value="https://api.example.com") as base, \
                patch.object(snapshot, "export_snapshot", return_value=rendered) as export, \
                patch.object(upload, "post_gzip_json", return_value=self.response) as post:
            result = UploadSearchResults(self.run_dir, env_file=self.env_file).run()
        token.assert_called_once_with(self.env_file)
        base.assert_called_once_with(self.env_file)
        export.assert_called_once_with(self.run_dir)
        post.assert_called_once_with("https://api.example.com", "/v2/local-searches", "synthetic-token",
                                     {"source_run_id": self.run_dir.name, "snapshot": rendered}, timeout=120)
        self.assertEqual(result, {"status": "uploaded", **self.response})

    def test_body_is_gzip_encoded_json(self):
        body = {"source_run_id": "example", "snapshot": {"search": {"run_id": "example"}}}
        response = io.BytesIO(json.dumps(self.response).encode())
        with patch.object(upload.urllib.request, "urlopen", return_value=response) as urlopen:
            result = upload.post_gzip_json("https://api.example.com", "/v2/local-searches", "synthetic-token",
                                           body, timeout=120)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Content-encoding"), "gzip")
        self.assertEqual(json.loads(gzip.decompress(request.data)), body)
        self.assertEqual(result, self.response)

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
                    patch.object(upload, "post_gzip_json", side_effect=error):
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
                patch.object(upload, "post_gzip_json", return_value=self.response) as post:
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

"""search_feedback: run-scoped edit log plus one aggregated feedback row."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.powerset.primitives.send_feedback.send_feedback import SendFeedback
from packs.search.primitives.search_feedback.search_feedback import (
    EDITS_FILE,
    SENT_FILE,
    log_edit,
    main,
    read_edits,
    send,
)


class LogEditTests(unittest.TestCase):
    def test_log_appends_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "swe-sf"
            first = log_edit(run_dir, kind="filter_edit",
                             note="dropped seniority band senior/staff",
                             before="senior/staff", after="any")
            self.assertEqual(first["status"], "logged")
            self.assertEqual(first["count"], 1)
            second = log_edit(run_dir, kind="result_feedback",
                              note="Jordan Bravo is the wrong person for this query")
            self.assertEqual(second["count"], 2)
            edits = read_edits(run_dir)
            self.assertEqual([e["kind"] for e in edits],
                             ["filter_edit", "result_feedback"])
            self.assertEqual(edits[0]["before"], "senior/staff")
            self.assertNotIn("before", edits[1])

    def test_log_rejects_bad_kind_and_empty_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            with self.assertRaises(SystemExit):
                log_edit(run_dir, kind="vibes", note="x")
            with self.assertRaises(SystemExit):
                log_edit(run_dir, kind="filter_edit", note="  ")


class SendTests(unittest.TestCase):
    def test_send_without_edits_is_no_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(send(Path(tmp), dry_run=True)["status"], "no_edits")

    def test_dry_run_aggregates_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "pm-nyc"
            log_edit(run_dir, kind="query_edit",
                     note="narrowed to fintech PMs", after="fintech product managers in nyc")
            log_edit(run_dir, kind="filter_edit", note="kept founders in")
            (run_dir / "decision.json").write_text(json.dumps(
                {"surface": "people", "backend": "local", "depth": "fast",
                 "mode": "interactive", "reason": "synthetic"}), encoding="utf-8")
            payload = send(run_dir, dry_run=True)
            self.assertEqual(payload["status"], "dry_run")
            body = payload["body"]
            self.assertEqual(body["feedback_type"], "filter_edit")
            self.assertIn("pm-nyc", body["comment"])
            self.assertEqual(len(body["metadata"]["edits"]), 2)
            self.assertEqual(body["metadata"]["decision"]["backend"], "local")
            self.assertFalse((run_dir / SENT_FILE).is_file())

    def test_result_feedback_makes_it_bad_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "swe-sf"
            log_edit(run_dir, kind="filter_edit", note="removed location filter")
            log_edit(run_dir, kind="result_feedback", note="top result is stale")
            body = send(run_dir, dry_run=True)["body"]
            self.assertEqual(body["feedback_type"], "bad_search")

    def test_submit_rotates_log_into_sent_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "swe-sf"
            log_edit(run_dir, kind="pond_edit", note="rewrote pond 2 query")
            with patch.object(SendFeedback, "run", return_value={
                    "status": "submitted", "http_status": 200,
                    "feedback_id": "row-1", "feedback_type": "filter_edit"}):
                payload = send(run_dir)
            self.assertEqual(payload["status"], "submitted")
            self.assertFalse((run_dir / EDITS_FILE).is_file())
            sent = json.loads((run_dir / SENT_FILE).read_text(encoding="utf-8"))
            self.assertEqual(sent["feedback_id"], "row-1")
            self.assertEqual(sent["edit_count"], 1)
            self.assertEqual(sent["edits"][0]["kind"], "pond_edit")
            # a repeat send has nothing to ship; a rerun's new edit sends alone
            self.assertEqual(send(run_dir, dry_run=True)["status"], "no_edits")
            log_edit(run_dir, kind="filter_edit", note="rerun narrowed location")
            body = send(run_dir, dry_run=True)["body"]
            self.assertEqual(len(body["metadata"]["edits"]), 1)

    def test_needs_auth_keeps_the_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "swe-sf"
            log_edit(run_dir, kind="filter_edit", note="kept founders in")
            with patch.object(SendFeedback, "run", return_value={
                    "status": "needs_auth", "error": "run `$powerset login`"}):
                payload = send(run_dir)
            self.assertEqual(payload["status"], "needs_auth")
            self.assertTrue((run_dir / EDITS_FILE).is_file())
            self.assertFalse((run_dir / SENT_FILE).is_file())


class MainExitCodeTests(unittest.TestCase):
    def test_log_and_no_edits_send_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = str(Path(tmp) / "swe-sf")
            self.assertEqual(main(["log", "--run-dir", run_dir,
                                   "--kind", "query_edit", "--note", "n"]), 0)
            self.assertEqual(main(["send", "--run-dir", str(Path(tmp) / "empty"),
                                   "--dry-run"]), 0)

    def test_needs_auth_exits_zero_and_failed_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = str(Path(tmp) / "swe-sf")
            main(["log", "--run-dir", run_dir, "--kind", "filter_edit", "--note", "n"])
            with patch.object(SendFeedback, "run",
                              return_value={"status": "needs_auth", "error": "x"}):
                self.assertEqual(main(["send", "--run-dir", run_dir]), 0)
            with patch.object(SendFeedback, "run",
                              return_value={"status": "failed", "error": "x"}):
                self.assertEqual(main(["send", "--run-dir", run_dir]), 1)


if __name__ == "__main__":
    unittest.main()

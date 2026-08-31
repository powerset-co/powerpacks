"""search_feedback: run-scoped edit log plus one aggregated feedback row."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packs.search.primitives.search_feedback.search_feedback import (
    EDITS_FILE,
    SENT_FILE,
    log_edit,
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

    def test_already_sent_skips_resend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "swe-sf"
            log_edit(run_dir, kind="pond_edit", note="rewrote pond 2 query")
            (run_dir / SENT_FILE).write_text(json.dumps(
                {"sent_at": "2026-08-31T00:00:00Z", "edit_count": 1,
                 "feedback_id": "row-1"}), encoding="utf-8")
            payload = send(run_dir, dry_run=True)
            self.assertEqual(payload["status"], "already_sent")
            self.assertEqual(payload["feedback_id"], "row-1")
            log_edit(run_dir, kind="result_feedback", note="second thoughts on rank 3")
            self.assertEqual(send(run_dir, dry_run=True)["status"], "dry_run")

    def test_edits_file_name_is_stable(self) -> None:
        self.assertEqual(EDITS_FILE, "user-edits.jsonl")


if __name__ == "__main__":
    unittest.main()

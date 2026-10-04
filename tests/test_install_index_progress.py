"""Install reads current fixed indexing progress without starting paid work."""

import json
import tempfile
import unittest
from pathlib import Path

from packs.powerset.primitives.install.index_progress import read_index_progress

ENTRY = "2026-10-03T10:00:00Z"


class InstallIndexProgressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, name="setup-linkedin-modal", **changes):
        path = self.root / ".powerpacks/runs" / name / "status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"status": "running", "started_at": ENTRY, "updated_at": ENTRY,
                  "current_stage": "indexing", "progress": 0.5,
                  "stages": {"indexing": {"message": "Building search records",
                                          "payload": {"contacts": 12}}},
                  "stage_order": [{"id": "indexing", "label": "Building search index"}]}
        record.update(changes)
        path.write_text(json.dumps(record))
        return path

    def test_current_progress_preserves_counts_and_fraction(self):
        self.write()
        self.assertEqual(read_index_progress(self.root, ENTRY), {
            "status": "running", "current_stage": "indexing", "message": "Building search records",
            "progress": 0.5, "payload": {"contacts": 12},
        })

    def test_explicit_producer_counts_are_preserved(self):
        self.write(stages={"indexing": {"message": "Building search records",
                                       "payload": {"counts": {"completed": 3, "total": 12}}}})
        self.assertEqual(read_index_progress(self.root, ENTRY)["payload"],
                         {"counts": {"completed": 3, "total": 12}})

    def test_previous_run_is_ignored_even_if_recently_updated(self):
        self.write(started_at="2026-10-03T09:59:59Z", updated_at="2026-10-03T10:01:00Z")
        self.assertIsNone(read_index_progress(self.root, ENTRY))

    def test_reads_newest_current_run_in_historical_gmail_path(self):
        self.write()
        self.write("setup-gmail-modal", started_at="2026-10-03T10:00:01Z", progress=0.75)
        self.assertEqual(read_index_progress(self.root, ENTRY)["progress"], 0.75)

    def test_missing_progress_is_not_completion(self):
        self.assertIsNone(read_index_progress(self.root, ENTRY))

    def test_failed_progress_remains_failed(self):
        self.write(status="failed", progress=0.5,
                   stages={"indexing": {"message": "Download failed", "payload": {"error": "disconnected"}}})
        result = read_index_progress(self.root, ENTRY)
        self.assertEqual((result["status"], result["message"]), ("failed", "Download failed"))
        self.assertEqual(result["payload"], {"error": "disconnected"})

    def test_initial_stage_uses_producers_label_before_first_event(self):
        self.write(stages={}, progress=0.0)
        self.assertEqual(read_index_progress(self.root, ENTRY)["message"], "Building search index")

    def test_completed_progress_preserves_actual_terminal_status(self):
        self.write(status="completed", progress=1.0)
        self.assertEqual(read_index_progress(self.root, ENTRY)["status"], "completed")

    def test_invalid_documents_report_failure(self):
        for content in ("{", "[]", '{"started_at":"not a date"}',
                        json.dumps({"started_at": ENTRY, "status": "completed", "current_stage": "indexing",
                                    "progress": 3.0, "stages": {"indexing": {"message": "Done", "payload": {}}}})):
            with self.subTest(content=content):
                path = self.write()
                path.write_text(content)
                result = read_index_progress(self.root, ENTRY)
                self.assertEqual(result["status"], "failed")
                self.assertIn("could not be read", result["message"])

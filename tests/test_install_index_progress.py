"""Install reads current fixed indexing progress without starting paid work."""

import argparse
import json
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.powerset.primitives.install.index_progress import read_index_progress
from packs.shared.csv_io import CsvIO

with patch("dotenv.load_dotenv"):
    from packs.indexing.modal import linkedin_modal_pipeline as modal_pipeline
    from packs.indexing.modal.linkedin_modal_pipeline import GMAIL_STAGES, GMAIL_VERTICAL, PipelineProgress

ENTRY = "2026-10-03T10:00:00Z"


class InstallIndexProgressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()

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

    def test_actual_modal_producer_phases_counts_and_completion(self):
        producer = PipelineProgress(self.root / ".powerpacks/runs/setup-gmail-modal",
                                    GMAIL_STAGES, GMAIL_VERTICAL)
        started_at = producer.status["started_at"]
        self.assertEqual(read_index_progress(self.root, started_at), {
            "status": "running", "current_stage": "enriching", "message": "Enriching contacts",
            "progress": 0.0, "payload": {},
        })

        producer.event("enriching", "Enriched contacts locally", status="completed", payload={"contacts": 12})
        self.assertEqual(read_index_progress(self.root, started_at), {
            "status": "running", "current_stage": "enriching", "message": "Enriched contacts locally",
            "progress": 0.333, "payload": {"contacts": 12},
        })
        producer.event("importing", "Loaded 12 contacts", status="completed", payload={"contacts": 12})
        self.assertEqual(read_index_progress(self.root, started_at)["progress"], 0.667)
        for phase in ("seed", "estimate", "pipeline", "refresh-cache", "duckdb", "persist", "done"):
            with self.subTest(phase=phase):
                producer.event("indexing", f"Indexing: {phase}", payload={"phase": phase})
                self.assertEqual(read_index_progress(self.root, started_at), {
                    "status": "running", "current_stage": "indexing", "message": f"Indexing: {phase}",
                    "progress": 0.667, "payload": {"phase": phase},
                })

        result = {"contacts": 12, "duckdb": str(self.root / ".powerpacks/search-index/local-search.duckdb")}
        producer.event("indexing", "Search index is ready", status="completed", progress=1.0, payload=result)
        self.assertEqual(read_index_progress(self.root, started_at)["status"], "running")
        producer.finish(result)
        self.assertEqual(read_index_progress(self.root, started_at), {
            "status": "completed", "current_stage": "indexing", "message": "Search index is ready",
            "progress": 1.0, "payload": result,
        })

    def test_actual_modal_producer_failure_preserves_remote_error(self):
        producer = PipelineProgress(self.root / ".powerpacks/runs/setup-gmail-modal",
                                    GMAIL_STAGES, GMAIL_VERTICAL)
        payload = {"status": "failed", "phase": "pipeline", "exit_code": 1}
        producer.event("indexing", "Indexing failed: pipeline", status="failed", payload=payload)
        self.assertEqual(read_index_progress(self.root, producer.status["started_at"]), {
            "status": "failed", "current_stage": "indexing", "message": "Indexing failed: pipeline",
            "progress": 0.667, "payload": payload,
        })

    def test_index_people_owns_progress_and_default_download_in_current_checkout(self):
        people_csv = self.root / ".powerpacks/network-import/merged/people.csv"
        CsvIO.write_dict_rows(people_csv, PEOPLE_SCHEMA_COLUMNS, [
            {"id": "synthetic-jordan", "full_name": "Jordan Bravo", "summary": "Synthetic\ncontact"},
            {"id": "synthetic-casey", "full_name": "Casey Example"},
        ])
        other_checkout = self.root / "other-checkout/.powerpacks"
        expected_dest = self.root / ".powerpacks/search-index"

        def download(args):
            self.assertEqual(Path(args.dest), expected_dest)
            expected_dest.mkdir(parents=True)
            (expected_dest / "local-search.duckdb").write_bytes(b"offline downloaded artifact")
            return 0

        with (
            chdir(self.root),
            patch.object(modal_pipeline, "LOCAL_POWERPACKS", other_checkout),
            patch.object(modal_pipeline, "get_volume"),
            patch.object(modal_pipeline.modal.App, "lookup"),
            patch.object(modal_pipeline.modal.Sandbox, "create", return_value=argparse.Namespace(object_id="offline-sandbox")),
            patch.object(modal_pipeline, "build_image"),
            patch.object(modal_pipeline, "powerset_api_secret"),
            patch.object(modal_pipeline.modal.Secret, "from_name"),
            patch.object(modal_pipeline, "watch_run", return_value={"status": "completed", "phase": "done"}),
            patch.object(modal_pipeline, "cmd_download", side_effect=download),
        ):
            code = modal_pipeline.cmd_index_people(argparse.Namespace(
                people_csv=str(people_csv), dest=None, max_usd=0, timeout=1))

        self.assertEqual(code, 0)
        status_path = self.root / ".powerpacks/runs/setup-gmail-modal/status.json"
        started_at = json.loads(status_path.read_text())["started_at"]
        self.assertEqual(read_index_progress(self.root, started_at), {
            "status": "completed", "current_stage": "indexing", "message": "Search index is ready",
            "progress": 1.0, "payload": {
                "people_csv": str(people_csv), "contacts": 2,
                "duckdb": str(expected_dest / "local-search.duckdb"),
            },
        })
        self.assertTrue((status_path.parent / "events.jsonl").is_file())
        self.assertTrue((expected_dest / "local-search.duckdb").is_file())
        self.assertFalse(other_checkout.exists())

    def test_watch_run_mirrors_readable_phases_and_returns_actual_terminal_status(self):
        phases = [
            ("seed", "Preparing your contacts"),
            ("pipeline", "Building your search records"),
            ("duckdb", "Preparing search"),
            ("persist", "Saving your search index"),
            ("done", "Finishing up"),
        ]
        for terminal_status in ("completed", "failed"):
            with self.subTest(status=terminal_status):
                producer = PipelineProgress(self.root / ".powerpacks/runs/setup-gmail-modal",
                                            GMAIL_STAGES, GMAIL_VERTICAL)
                current_phases = phases if terminal_status == "completed" else phases[:3]
                remote = [{"status": "running", "phase": phase} for phase, _ in current_phases]
                remote[-1]["status"] = terminal_status
                if terminal_status == "failed":
                    remote[-1]["exit_code"] = 1
                with (
                    patch.object(modal_pipeline, "read_run_status", side_effect=remote),
                    patch.object(modal_pipeline.time, "sleep"),
                ):
                    result = modal_pipeline.watch_run("offline-index", producer, "indexing", "Indexing")

                self.assertEqual(result, remote[-1])
                events = [json.loads(line) for line in producer.events_path.read_text().splitlines()]
                self.assertEqual([event["message"] for event in events], [message for _, message in current_phases])
                self.assertEqual([event["payload"] for event in events], [{"phase": phase} for phase, _ in current_phases])
                self.assertEqual(read_index_progress(self.root, producer.status["started_at"]), {
                    "status": "running", "current_stage": "indexing", "message": current_phases[-1][1],
                    "progress": 0.667, "payload": {"phase": current_phases[-1][0]},
                })

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

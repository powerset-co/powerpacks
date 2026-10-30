"""The LinkedIn setup always runs its fixed sequence and writes the install record."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from packs.indexing.modal import api_runs
from packs.ingestion.primitives.deep_context_v2.run import INDEX_MAX_USD
from packs.ingestion.primitives.discover.linkedin.connections import LinkedInConnections
from packs.powerset.primitives.install import linkedin_setup as setup
from packs.powerset.primitives.install import report
from packs.powerset.primitives.install.status import InstallStatus


class LinkedInSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.records = []
        original = InstallStatus.write

        def write(status, *args, **kwargs):
            record = original(status, *args, **kwargs)
            self.records.append(record)
            return record

        def connections(*, on_count):
            on_count(1, None)
            on_count(2, 2)
            return {"status": "completed", "outcome": "read", "connections": 2, "added": 1, "read": 2, "total": 2}

        def mock(target, name, **kwargs):
            patcher = patch.object(target, name, **kwargs)
            value = patcher.start()
            self.addCleanup(patcher.stop)
            return value

        mock(InstallStatus, "write", autospec=True, side_effect=write)
        self.report = mock(report, "install")
        self.connections = mock(LinkedInConnections, "run", side_effect=connections)
        mock(setup, "load_env")
        mock(setup, "open_store")
        self.write_connections = mock(setup, "write_connections")
        self.people = self.root / ".powerpacks/search-index/people.csv"
        self.realize = mock(setup, "realize", return_value=self.people)
        self.validate = mock(setup, "validate_search_index", return_value={"status": "ok", "total_people": 2})
        self.upload = mock(api_runs, "upload", return_value="uploaded")
        self.start = mock(api_runs, "start", side_effect=lambda kind, *_args, **_kwargs: kind)
        self.payloads = [
            None,
            {"status": "running", "code": "enrich.count", "done": 1, "total": 2, "left_seconds": 3},
            {"status": "running", "code": "enrich.count", "done": 1, "total": 2, "left_seconds": 3},
            {"status": "completed", "code": "enrich.count", "done": 2, "total": 2, "left_seconds": 0},
            {"status": "completed", "code": "step.embed_companies", "done": 15, "total": 15,
             "left_seconds": 0},
        ]
        self.poll = mock(api_runs, "status", side_effect=lambda _label: self.payloads.pop(0))
        self.download = mock(api_runs, "download", return_value=100)
        self.sleep = mock(setup.time, "sleep")

    def run_setup(self):
        return setup.main(["--root", str(self.root)])

    def test_record_order_and_modal_words_counts_and_estimates(self):
        self.assertEqual(self.run_setup(), 0)
        events = list(dict.fromkeys(record["event"] for record in self.records))
        self.assertEqual(events, [
            "linkedin.reading", "linkedin.reading.read", "linkedin.reading.count", "linkedin.done.read",
            "index.linkedin", "index.linkedin.enrich.count", "index.preparing", "index.building",
            "index.building.step.embed_companies", "index.done", "validate.checking", "validate.done",
            "search.ready"])
        for record in self.records:
            self.assertEqual(record["plan"], ["linkedin", "index", "validate", "ready"])
            self.assertEqual(record["retry_command"], "bin/linkedin-setup")
            self.assertNotIn("deep_context", record["steps"])
            self.assertNotIn("enrich", record["steps"])
        self.assertTrue(all(record["installer_pid"] == os.getpid() for record in self.records[:-1]))
        self.assertEqual(self.records[-1]["installer_pid"], 0)
        self.assertEqual(self.records[-1]["message"], "Search is ready: 2 people searchable.")
        self.assertIsNone(self.records[-1]["action"])
        self.assertEqual(self.records[-1]["note"], "")
        modal = [record for record in self.records if record["message"] == "Importing your connections: 1 of 2"]
        self.assertEqual(len(modal), 1)
        self.assertEqual(modal[0]["action"]["details"], {"done": 1, "total": 2, "left_seconds": 3})
        building = next(record for record in self.records if record["message"] == "Understanding companies")
        self.assertEqual(building["action"]["details"], {"done": 15, "total": 15, "left_seconds": 0})
        self.assertEqual(self.start.call_args_list, [
            call("import-linkedin", "uploaded", source_user="linkedin"),
            call("index-people", "uploaded", max_usd=float(INDEX_MAX_USD))])
        self.assertEqual(self.download.call_args_list, [
            call("import-linkedin", "people.csv", self.root / ".powerpacks" / setup.LINKEDIN_CSV),
            call("index-people", "local-search.duckdb", self.people.parent / "local-search.duckdb"),
            call("index-people", "manifest.json", self.people.parent / "manifest.json")])
        self.realize.assert_called_once_with(self.root / ".powerpacks")
        self.validate.assert_called_once_with(self.people.parent / "local-search.duckdb", self.people)
        self.assertEqual(self.sleep.call_args_list, [call(3), call(3), call(3)])

    def test_second_invocation_repeats_every_stage(self):
        payloads = list(self.payloads)
        self.assertEqual(self.run_setup(), 0)
        first_run_count = len(self.records)
        self.payloads = payloads
        self.assertEqual(self.run_setup(), 0)
        readings = self.records[first_run_count:first_run_count + 3]
        self.assertEqual([record["event"] for record in readings], [
            "linkedin.reading", "linkedin.reading.read", "linkedin.reading.count"])
        for reading in readings:
            self.assertEqual(reading["steps"], {
                "linkedin": {"status": "running", "message": reading["message"]}})
        for operation in (self.connections, self.write_connections, self.realize, self.validate):
            self.assertEqual(operation.call_count, 2)
        self.assertEqual(self.start.call_count, 4)
        self.assertEqual(self.download.call_count, 6)

    def test_read_failures_and_sign_in_waits_stop_before_modal(self):
        for state, event, code in [("failed", "linkedin.failed", 1),
                                    ("needs_user_action", "linkedin.waiting", 20)]:
            with self.subTest(state=state):
                self.connections.side_effect = None
                self.connections.return_value = {"status": state, "message": "Sign in"}
                self.assertEqual(self.run_setup(), code)
                self.assertEqual(self.records[-1]["event"], event)
                self.assertEqual(self.records[-1]["installer_pid"], 0)
                self.upload.assert_not_called()
        self.assertEqual(self.report.call_args_list, [call(self.root), call(self.root)])

    def test_modal_failure_records_its_error_and_does_not_download(self):
        self.payloads = [{"status": "failed", "error": "Provider unavailable"}]
        self.assertEqual(self.run_setup(), 1)
        self.assertEqual(self.records[-1]["step"], "index")
        self.assertEqual(self.records[-1]["event"], "step.failed")
        self.assertEqual(self.records[-1]["message"], "Provider unavailable")
        self.report.assert_called_once_with(self.root)
        log = (self.root / ".powerpacks/install/install.log").read_text()
        self.assertIn("Traceback (most recent call last):", log)
        self.assertIn("RuntimeError: Provider unavailable", log)
        self.download.assert_not_called()
        self.validate.assert_not_called()

    def test_missing_download_or_failed_validation_never_claims_ready(self):
        self.download.return_value = -1
        self.assertEqual(self.run_setup(), 1)
        self.assertNotIn("ready", self.records[-1]["steps"])
        self.validate.assert_not_called()
        self.download.return_value = 100
        self.payloads = [{"status": "completed"}, {"status": "completed"}]
        self.validate.return_value = {"status": "fail", "summary": "Index missing people"}
        self.assertEqual(self.run_setup(), 1)
        self.assertEqual(self.records[-1]["step"], "validate")
        self.assertEqual(self.records[-1]["message"], "Index missing people")
        self.assertNotIn("ready", self.records[-1]["steps"])


if __name__ == "__main__":
    unittest.main()

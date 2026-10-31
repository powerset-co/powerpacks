"""The index run's human-readable progress and time estimates."""
import io
import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.indexing.modal import linkedin_modal_pipeline, run_indexing, run_linkedin

from packs.indexing.modal.run_indexing import progress_line
from packs.indexing.primitives.build_processing_pipeline.build_processing_pipeline import STEPS
from packs.powerset.primitives.install.status_prose import PROSE


class ProgressLineTests(unittest.TestCase):
    def test_every_step_and_phase_has_words_in_the_prose_table(self):
        for step in STEPS:
            self.assertIn(f"index.building.step.{step}", PROSE)
        for code in ("seed", "estimate", "pipeline", "companies", "refresh-cache", "duckdb", "persist"):
            self.assertIn(f"index.building.{code}", PROSE)
        for code in ("parse", "enrich", "enrich.count", "persist", "done"):
            self.assertIn(f"index.linkedin.{code}", PROSE)

    def test_started_steps_use_the_logged_name_even_when_parallel(self):
        for position, step in enumerate(reversed(STEPS)):
            self.assertEqual(progress_line(f"[build-processing] start {step}\n", position),
                             (f"step.{step}", position, len(STEPS), 0.0))
        self.assertIsNone(progress_line("[build-processing] completed build_company_corpus in 1s", 3))

    def test_fetch_start_and_counts_use_the_logged_rate(self):
        self.assertEqual(progress_line("[enrich-companies] fetching 700 company profiles at 700 rpm\n", 3),
                         ("companies", 0, 700, 60))
        self.assertEqual(progress_line("[enrich-companies] fetched 350/700 company profiles\n", 3, 700),
                         ("companies", 350, 700, 30))
        self.assertEqual(progress_line("[enrich-companies] fetched 700/700 company profiles\n", 3, 700),
                         ("companies", 700, 700, 0))
        self.assertEqual(progress_line("[enrich-companies] fetched 40/111 company profiles\n", 3, 600)[-1], 7.1)

    def test_the_time_left_is_the_whole_build_and_only_falls(self):
        people = 1000
        start = run_indexing.seconds_left("estimate", people)
        build = run_indexing.seconds_left("pipeline", people, company_seconds=120, steps_done=0)
        later = run_indexing.seconds_left("pipeline", people, company_seconds=0, steps_done=10)
        cache = run_indexing.seconds_left("refresh-cache", people)
        self.assertEqual(start, 60 + 180 + 200 + 30 + 30)
        self.assertEqual(build, 120 + 180 + 200 + 30 + 30)
        self.assertEqual(later, 60 + 200 + 30 + 30)
        self.assertEqual(cache, 200 + 30 + 30)
        self.assertEqual(run_indexing.seconds_left("persist", people), 30)

    def test_disabled_rate_limit_needs_no_fetch_time(self):
        self.assertEqual(progress_line("[enrich-companies] fetching 700 company profiles at 0 rpm", 3)[-1], 0.0)

    def test_other_lines_say_nothing(self):
        self.assertIsNone(progress_line("[enrich-companies] fetched 4/111 company profiles by slug\n", 3))
        self.assertIsNone(progress_line("[enrich-companies] finished fetching company profiles: 4/111 successful\n", 3))
        self.assertIsNone(progress_line("[enrich-companies] fetching 111 new company profiles from RapidAPI\n", 3))


class ModalProgressTests(unittest.TestCase):
    def test_crashing_child_replaces_unfinished_status_and_preserves_reported_failure(self):
        with patch("modal.App"), patch("modal.Volume.from_name"), patch("modal.Secret.from_name"), \
             patch.object(linkedin_modal_pipeline, "build_image"), \
             patch.object(linkedin_modal_pipeline, "powerset_api_secret"):
            wrapper = runpy.run_path(str(Path(run_indexing.__file__).with_name("app.py")))["_run"]
        for state in (None, "running", "completed", "failed"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                reported = {"status": state, "code": state, "error": "Provider unavailable"}
                child = "import sys; sys.exit(7)"
                if state:
                    child = (f"from pathlib import Path; Path({str(root / 'status.json')!r})"
                             f".write_text({json.dumps(reported)!r}); " + child)
                with patch.dict(wrapper.__globals__, {"Path": lambda _path: root}), \
                     patch.object(wrapper.__globals__["time"], "sleep"):
                    wrapper("index", "synthetic", [sys.executable, "-c", child])
                record = json.loads((root / "status.json").read_text())
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["code"], "failed")
                self.assertEqual(record["error"], "Provider unavailable" if state == "failed"
                                 else "the Modal run exited with code 7")

    def test_watcher_prints_changed_statuses_and_returns_the_terminal_payload(self):
        for state in ("completed", "failed"):
            with self.subTest(state=state):
                running = {"status": "running", "code": "enrich.count", "done": 1, "total": 2}
                terminal = {"status": state, "code": "done" if state == "completed" else "failed"}
                payloads = iter([None, running, running, terminal])
                with patch.object(linkedin_modal_pipeline.time, "sleep") as sleep, \
                     patch.object(sys, "stdout", io.StringIO()) as output:
                    self.assertEqual(linkedin_modal_pipeline.watch_run(lambda: next(payloads)), terminal)
                self.assertEqual(output.getvalue().splitlines(), ["enrich.count 1/2", f"{terminal['code']} None/None"])
                self.assertEqual(sleep.call_count, 3)

    def test_company_fetch_keeps_its_line_until_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "people.csv").write_text("id,full_name\n1,Jordan Bravo\n2,Sam Li\n")
            lines = io.StringIO(
                "[build-processing] start build_roles\n"
                "[enrich-companies] fetching 700 company profiles at 700 rpm\n"
                "[enrich-companies] fetched 350/700 company profiles\n"
                "[build-processing] completed build_roles in 1s\n"
                "[build-processing] start build_company_corpus\n"
                "[enrich-companies] fetched 700/700 company profiles\n"
                "[build-processing] start build_company_corpus\n")
            original_path = Path

            def path(value):
                return root / "work" if value == "/tmp/run/search-index" else original_path(value)

            with patch.object(sys, "argv", ["run_indexing", "--people-csv", str(root / "people.csv"), "--cache-root", str(root),
                                           "--run-vol", str(root), "--operator-id", "synthetic", "--no-refresh-cache"]), \
                 patch.object(run_indexing, "Path", side_effect=path), \
                 patch.object(run_indexing.subprocess, "Popen") as process, \
                 patch.object(run_indexing.subprocess, "run") as duckdb, \
                 patch.object(run_indexing, "write_status") as write:
                process.return_value.stderr = lines
                process.return_value.wait.return_value = 0
                duckdb.return_value.returncode = 0
                self.assertEqual(run_indexing.main(), 0)
            codes = [item.args[1]["code"] for item in write.call_args_list]
            self.assertEqual(codes, ["seed", "pipeline", "step.build_roles", "companies", "companies", "companies",
                                     "step.build_company_corpus", "duckdb", "persist", "done"])
            counts = [(item.args[1]["done"], item.args[1]["total"]) for item in write.call_args_list
                      if item.args[1].get("code") == "companies"]
            self.assertEqual(counts, [(0, 700), (350, 700), (700, 700)])

    def test_import_counts_reach_status_json_from_enrichment_stderr(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(sys, "stderr", io.StringIO()), \
             patch.object(run_linkedin.time, "monotonic", side_effect=[10, 20, 30]):
            progress = run_linkedin._ImportProgress(Path(tmp), {"status": "running"})
            progress.write("[enrich-people] LinkedIn profile enrichment progress: 10/20 processed (10 cached")
            progress.write(").\n")
            record = json.loads((Path(tmp) / "status.json").read_text())
            self.assertEqual(record["code"], "enrich.count")
            self.assertEqual((record["done"], record["total"], record["left_seconds"]), (10, 20, 10))
            progress.write("[enrich-people] LinkedIn profile enrichment progress: 20/20 processed (20 cached).\n")
            record = json.loads((Path(tmp) / "status.json").read_text())
            self.assertEqual((record["done"], record["total"], record["left_seconds"]), (20, 20, 0))


if __name__ == "__main__":
    unittest.main()

"""The index run's human-readable progress and time estimates."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.indexing.modal import run_indexing, run_linkedin

from packs.indexing.modal.run_indexing import progress_line
from packs.indexing.primitives.build_processing_pipeline.build_processing_pipeline import STEPS, STEP_WORDS


class ProgressLineTests(unittest.TestCase):
    def test_every_step_has_words(self):
        self.assertEqual(set(STEPS), set(STEP_WORDS))

    def test_started_steps_use_the_logged_name_even_when_parallel(self):
        for position, step in enumerate(reversed(STEPS)):
            message, done, total, steps, left = progress_line(f"[build-processing] start {step}\n", position)
            self.assertEqual(message, STEP_WORDS[step])
            self.assertEqual((done, total, steps), (position, len(STEPS), position))
            self.assertGreater(left, 0)
        self.assertIsNone(progress_line("[build-processing] completed build_company_corpus in 1s", 3))

    def test_fetch_start_and_counts_use_the_logged_rate(self):
        self.assertEqual(progress_line("[enrich-companies] fetching 700 company profiles at 700 rpm\n", 3),
                         ("Company profiles: 0 of 700", 0, 700, 3, 180))
        self.assertEqual(progress_line("[enrich-companies] fetched 350/700 company profiles\n", 3, 700),
                         ("Company profiles: 350 of 700", 350, 700, 3, 150))
        self.assertEqual(progress_line("[enrich-companies] fetched 700/700 company profiles\n", 3, 700),
                         ("Company profiles: 700 of 700", 700, 700, 3, 120))
        self.assertEqual(progress_line("[enrich-companies] fetched 40/111 company profiles\n", 3, 600)[-1],
                         127.1)

    def test_disabled_rate_limit_has_only_the_remaining_build_estimate(self):
        self.assertEqual(progress_line("[enrich-companies] fetching 700 company profiles at 0 rpm", 3)[-1], 120)

    def test_other_lines_say_nothing(self):
        self.assertIsNone(progress_line("[enrich-companies] fetched 4/111 company profiles by slug\n", 3))
        self.assertIsNone(progress_line("[enrich-companies] finished fetching company profiles: 4/111 successful\n", 3))
        self.assertIsNone(progress_line("[enrich-companies] fetching 111 new company profiles from RapidAPI\n", 3))


class ModalProgressTests(unittest.TestCase):
    def test_company_fetch_keeps_its_line_until_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
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

            with patch.object(sys, "argv", ["run_indexing", "--people-csv", "people.csv", "--cache-root", str(root),
                                           "--run-vol", str(root), "--operator-id", "synthetic", "--no-refresh-cache"]), \
                 patch.object(run_indexing, "Path", side_effect=path), \
                 patch.object(run_indexing.subprocess, "Popen") as process, \
                 patch.object(run_indexing.subprocess, "run") as duckdb, \
                 patch.object(run_indexing, "write_status") as write:
                process.return_value.stderr = lines
                process.return_value.wait.return_value = 0
                duckdb.return_value.returncode = 0
                self.assertEqual(run_indexing.main(), 0)
            messages = [item.args[1]["message"] for item in write.call_args_list if "message" in item.args[1]]
            self.assertEqual(messages, [STEP_WORDS["build_roles"], "Company profiles: 0 of 700",
                                        "Company profiles: 350 of 700", "Company profiles: 700 of 700",
                                        STEP_WORDS["build_company_corpus"]])

    def test_import_counts_reach_status_json_from_enrichment_stderr(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(sys, "stderr", io.StringIO()), \
             patch.object(run_linkedin.time, "monotonic", side_effect=[10, 20, 30]):
            progress = run_linkedin._ImportProgress(Path(tmp), {"status": "running"})
            progress.write("[enrich-people] LinkedIn profile enrichment progress: 10/20 processed (10 cached")
            progress.write(").\n")
            record = json.loads((Path(tmp) / "status.json").read_text())
            self.assertEqual(record["message"], "Importing your connections: 10 of 20")
            self.assertEqual((record["done"], record["total"], record["left_seconds"]), (10, 20, 10))
            progress.write("[enrich-people] LinkedIn profile enrichment progress: 20/20 processed (20 cached).\n")
            record = json.loads((Path(tmp) / "status.json").read_text())
            self.assertEqual((record["done"], record["total"], record["left_seconds"]), (20, 20, 0))


if __name__ == "__main__":
    unittest.main()

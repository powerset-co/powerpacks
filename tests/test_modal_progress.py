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
from packs.indexing.modal.sandbox_common import merge_cache_file, write_status
from packs.indexing.primitives.build_processing_pipeline.build_processing_pipeline import STEPS
from packs.powerset.primitives.install.status_prose import PROSE


class ProgressLineTests(unittest.TestCase):
    def test_every_step_and_phase_has_words_in_the_prose_table(self):
        for step in STEPS:
            self.assertIn(f"index.building.step.{step}", PROSE)
        for code in ("seed", "estimate", "pipeline", "companies", "duckdb", "persist"):
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
        start = run_indexing.seconds_left("estimate")
        build = run_indexing.seconds_left("pipeline", company_seconds=120, steps_done=0)
        later = run_indexing.seconds_left("pipeline", company_seconds=0, steps_done=10)
        self.assertEqual(start, 60 + 180 + 30 + 30)
        self.assertEqual(build, 120 + 180 + 30 + 30)
        self.assertEqual(later, 60 + 30 + 30)
        self.assertEqual(run_indexing.seconds_left("duckdb"), 60)
        self.assertEqual(run_indexing.seconds_left("persist"), 30)
        self.assertNotIn("refresh-cache", run_indexing.PHASES)
        self.assertNotIn("index.building.refresh-cache", PROSE)

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
            (root / "work/roles").mkdir(parents=True)
            (root / "work/roles/roles_with_dense_text.jsonl").write_text('{"title_hash": "new"}\n')
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
                 patch.object(run_indexing, "write_status") as write, \
                 patch.object(run_indexing, "merge_cache_file") as merge:
                process.return_value.stderr = lines
                process.return_value.wait.return_value = 0
                duckdb.return_value.returncode = 0
                self.assertEqual(run_indexing.main(), 0)
                merge.assert_not_called()
            codes = [item.args[1]["code"] for item in write.call_args_list]
            self.assertEqual(codes, ["seed", "pipeline", "step.build_roles", "companies", "companies", "companies",
                                     "step.build_company_corpus", "duckdb", "persist", "done"])
            counts = [(item.args[1]["done"], item.args[1]["total"]) for item in write.call_args_list
                      if item.args[1].get("code") == "companies"]
            self.assertEqual(counts, [(0, 700), (350, 700), (700, 700)])

    def test_cache_merge_follows_ready_and_preserves_every_cache_on_duckdb_failure(self):
        import duckdb

        for duckdb_code in (0, 7):
            with self.subTest(duckdb_code=duckdb_code), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                work, cache, run = root / "work", root / "cache", root / "run"
                work.mkdir()
                (root / "people.csv").write_text("id,full_name\n1,Jordan Bravo\n")
                (work / "local-search.duckdb").write_bytes(b"search index")
                (work / "manifest.json").write_text('{"people": 1}')
                for rel_src, rel_cache in run_indexing.WORK_TO_CACHE.items():
                    keys = run_indexing.CACHE_KEYS[rel_cache]
                    for target, value in ((work / rel_src, "new"), (cache / rel_cache, "old")):
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if target.suffix == ".parquet":
                            with duckdb.connect() as con:
                                columns = ", ".join(f"'{value}' AS {key}" for key in keys)
                                con.execute(f"COPY (SELECT {columns}) TO '{target}' (FORMAT PARQUET)")
                        else:
                            target.write_text(json.dumps(dict.fromkeys(keys, value)) + "\n")
                events = []

                def start_pipeline(*_args, **_kwargs):
                    # The pipeline adds paid rows to the seeds copied by the runner.
                    for rel_src, rel_cache in run_indexing.WORK_TO_CACHE.items():
                        if rel_cache.startswith("seeds/"):
                            keys = run_indexing.CACHE_KEYS[rel_cache]
                            with (work / rel_src).open("a") as handle:
                                handle.write(json.dumps(dict.fromkeys(keys, "new")) + "\n")
                    return pipeline.return_value

                def status(path, payload):
                    events.append(payload["code"])
                    write_status(path, payload)

                def merge(src, dest, keys):
                    terminal = json.loads((run / "status.json").read_text())
                    self.assertEqual(terminal["status"], "completed" if duckdb_code == 0 else "failed")
                    if duckdb_code == 0:
                        self.assertEqual((run / "local-search.duckdb").read_bytes(), b"search index")
                        self.assertEqual(json.loads((run / "manifest.json").read_text()), {"people": 1})
                    events.append("merge")
                    return merge_cache_file(src, dest, keys)

                with patch.object(sys, "argv", ["run_indexing", "--people-csv", str(root / "people.csv"),
                                               "--cache-root", str(cache), "--run-vol", str(run),
                                               "--operator-id", "synthetic"]), \
                     patch.object(run_indexing, "Path", side_effect=lambda p: work if p == "/tmp/run/search-index" else Path(p)), \
                     patch.object(run_indexing.subprocess, "Popen") as pipeline, \
                     patch.object(run_indexing.subprocess, "run") as build, \
                     patch.object(run_indexing, "write_status", side_effect=status), \
                     patch.object(run_indexing, "merge_cache_file", side_effect=merge):
                    pipeline.side_effect = start_pipeline
                    pipeline.return_value.stderr = io.StringIO("")
                    pipeline.return_value.wait.return_value = 0
                    build.return_value.returncode = duckdb_code
                    self.assertEqual(run_indexing.main(), duckdb_code)
                self.assertEqual(events, ["seed", "pipeline", "duckdb"] +
                                 (["persist", "done"] if duckdb_code == 0 else ["failed"]) +
                                 ["merge"] * len(run_indexing.WORK_TO_CACHE))
                for rel_cache, keys in run_indexing.CACHE_KEYS.items():
                    target = cache / rel_cache
                    if target.suffix == ".parquet":
                        with duckdb.connect() as con:
                            values = {row[0] for row in con.execute(f"SELECT {keys[0]} FROM '{target}'").fetchall()}
                    else:
                        values = {json.loads(line)[keys[0]] for line in target.read_text().splitlines()}
                    self.assertEqual(values, {"old", "new"}, rel_cache)

    def test_app_keeps_committing_after_completed_until_child_exits(self):
        with patch("modal.App"), patch("modal.Volume.from_name"), patch("modal.Secret.from_name"), \
             patch.object(linkedin_modal_pipeline, "build_image"), \
             patch.object(linkedin_modal_pipeline, "powerset_api_secret"):
            wrapper = runpy.run_path(str(Path(run_indexing.__file__).with_name("app.py")))["_run"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = []

            def poll():
                if "completed" not in events:
                    write_status(root, {"status": "completed"})
                    events.append("completed")
                    return None
                events.append("merge finished")
                return 0

            with patch.dict(wrapper.__globals__, {"Path": lambda _path: root}), \
                 patch.object(wrapper.__globals__["subprocess"], "Popen") as process, \
                 patch.object(wrapper.__globals__["time"], "sleep"), \
                 patch.object(wrapper.__globals__["volume"], "commit", side_effect=lambda: events.append("commit")):
                process.return_value.poll.side_effect = poll
                process.return_value.returncode = 0
                wrapper("index", "synthetic", ["index"])
            self.assertEqual(events, ["commit", "completed", "commit", "merge finished", "commit"])
            process.return_value.terminate.assert_not_called()
            process.return_value.kill.assert_not_called()

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

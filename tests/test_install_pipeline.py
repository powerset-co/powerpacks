"""Stage doubles prove the installer's spend and resume boundaries over deep-context v2.

Changelog:
- 2026-10-07: v2 stages (load, collect, synthesize, dedupe, worth, enrich, realize); the v1 seed,
  readiness, profile-prefetch, workflow-state and wacli tests are gone with those stages.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from packs.ingestion.primitives.deep_context_v2.db import queries, store
from packs.powerset.primitives.install import pipeline
from packs.powerset.primitives.install.pipeline import ProcessingOnboarding
from packs.powerset.primitives.install.status import InstallStatus
from packs.powerset.primitives.install.steps import InstallStep

PAID = ("synthesize", "dedupe", "worth", "enrich")


class InstallPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.reset_pipeline(Path(self.temp.name))
        stages = (
            (pipeline.ImportLoad, "run", "load"),
            (pipeline.Collect, "run", "collect"),
            (pipeline.Synthesize, "estimate", "synthesize-estimate"),
            (pipeline.Synthesize, "run", "synthesize"),
            (pipeline.Dedupe, "estimate", "dedupe-estimate"),
            (pipeline.Dedupe, "run", "dedupe"),
            (pipeline.Worth, "estimate", "worth-estimate"),
            (pipeline.Worth, "run", "worth"),
            (pipeline.Enrich, "estimate", "enrich-estimate"),
            (pipeline.Enrich, "run", "enrich"),
            (pipeline.Realize, "run", "realize"),
        )
        for primitive, method, name in stages:
            def operation(node, *args, name=name, method=method, **kwargs):
                payload = self.native(name, node=node, **kwargs)
                if method == "run":
                    return SimpleNamespace(status=payload["status"], counts={}, error=None)
                return payload
            mocked = patch.object(primitive, method, autospec=True, side_effect=operation)
            mocked.start()
            self.addCleanup(mocked.stop)
        functions = {
            "open_store": self.open_store,
            "archive_v1": lambda data_root: self.native("archive-v1"),
            "build_owner": lambda data_root, url, emails: self.native("owner", url=url, emails=emails),
            "estimate_run": lambda args: self.native("index-estimate", options=args),
            "validate_search_index": lambda *args, **kwargs: self.native("search-validate"),
        }
        for name, operation in functions.items():
            mocked = patch.object(pipeline, name, side_effect=operation)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.patch = patch.object(pipeline.subprocess, "run", side_effect=self.external)
        self.subprocess = self.patch.start()
        self.addCleanup(self.patch.stop)

    def reset_pipeline(self, root):
        self.root = root.resolve()
        self.status = InstallStatus(self.root)
        self.retry = "bin/onboard --source gmail --gmail-email casey@example.com --sync-after 2025-01-01"
        self.status.write('step.waiting', step=InstallStep.DEEP_CONTEXT, pid=os.getpid(), retry_command=self.retry)
        owner = self.root / ".powerpacks/deep-context/owner.json"
        owner.parent.mkdir(parents=True)
        owner.write_text("{}")
        self.people = self.root / ".powerpacks/network-import/merged/people.csv"
        self.index = self.root / ".powerpacks/search-index"
        self.csv = "id,full_name\nsynthetic-jordan,Jordan Bravo\n"
        self.pending = {name: False for name in PAID}  # which paid stages have work this run
        self.cost = {name: 0.1 for name in PAID}
        self.index_cost = 0.5
        self.validation_status = "ok"
        self.calls = []
        self.paid_commands = []
        self.failing_command = ""
        self.exiting_command = ""
        self.fail_at = 0
        self.failure_after = ""

    def open_store(self, path):
        """A real, empty v2 store with an owner row: the stage classes read it when they are built."""
        conn = store.open_store(path)
        owner = {"name": "Casey Owner", "emails": ["casey@example.com"], "phones": [], "linkedin_url": "",
                 "education": [], "work": [], "locations": [], "notes": ""}
        queries.upsert_owner(conn, json.dumps(owner), "synthetic", store.now_iso())
        conn.commit()
        return conn

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def native(self, command, *, node=None, **kwargs):
        self.calls.append((command, {"node": node, **kwargs}))
        payload = {"status": "completed"}
        if command == self.failing_command or len(self.calls) == self.fail_at:
            raise RuntimeError("[synthetic] source read failed")
        if command == self.exiting_command:
            raise SystemExit("PARALLEL_API_KEY not set")
        if command.endswith("-estimate") and command != "index-estimate":
            stage = command[: -len("-estimate")]
            payload = {"estimated_cost_usd": self.cost[stage] if self.pending[stage] else 0.0, "pending": int(self.pending[stage])}
        elif command in PAID:
            if self.pending[command]:
                self.paid_commands.append(command)
            self.pending[command] = False
        elif command == "realize":
            self.write(self.people, self.csv)
        elif command == "index-estimate":
            options = kwargs["options"]
            self.assertEqual(options.input, self.people)
            self.assertEqual(options.output_dir, self.index)
            self.assertTrue(options.dry_run)
            payload = {"status": "dry_run", "estimated_cost_usd": self.index_cost,
                       "estimated_paid_calls": {"role_enrichment": 1}}
        elif command == "import-linkedin":
            self.write(self.root / ".powerpacks/network-import/import/linkedin/people.csv", self.csv)
        elif command in ("index", "download"):
            if command == "index":
                self.paid_commands.append("index")
            self.write(self.index / "local-search.duckdb", "synthetic index")
            self.write(self.index / "manifest.json", '{"status":"ok"}')
            dispatch_path = self.root / ".powerpacks/runs/setup-gmail-modal/status.json"
            if command == "index" and dispatch_path.is_file():
                dispatch = json.loads(dispatch_path.read_text())
                dispatch["status"] = "completed"
                dispatch_path.write_text(json.dumps(dispatch))
        elif command == "search-validate":
            payload = {"status": self.validation_status, "total_people": 1,
                       "summary": "Search index ready: 1 people searchable."}
        if command == self.failure_after:
            raise RuntimeError("[synthetic] failed after saving native outputs")
        return payload

    def external(self, argv, **kwargs):
        if argv[0] == "open":
            return subprocess.CompletedProcess(argv, 0)
        self.assertIn("packs/indexing/modal/linkedin_modal_pipeline.py", argv)
        self.assertEqual(kwargs["cwd"], self.root)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        name = "download" if "download" in argv else "import-linkedin" if "import-linkedin" in argv else "index"
        payload = self.native(name, argv=argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload))

    def run_pipeline(self, *spend):
        return ProcessingOnboarding(self.root, approved_spend=spend).run()

    def did(self, stage):
        return any(name == stage for name, _ in self.calls)

    def indexed(self):
        return self.did("index")

    def all_pending(self):
        for name in PAID:
            self.pending[name] = True

    def test_new_linkedin_connections_import_before_load_once(self):
        connections = self.root / ".powerpacks/network-import/discover/linkedin/Connections.csv"
        self.write(connections, "First Name,Last Name,URL\nJordan,Bravo,https://www.linkedin.com/in/jordan-bravo\n")
        self.run_pipeline()
        names = [name for name, _ in self.calls]
        self.assertEqual(names.count("import-linkedin"), 1)
        self.assertLess(names.index("import-linkedin"), names.index("load"))
        self.run_pipeline()
        self.assertEqual([name for name, _ in self.calls].count("import-linkedin"), 1)

    def test_no_linkedin_connections_never_imports_linkedin(self):
        self.run_pipeline()
        self.assertFalse(self.did("import-linkedin"))

    def test_v1_state_is_archived_before_the_store_opens(self):
        self.run_pipeline()
        names = [name for name, _ in self.calls]
        self.assertLess(names.index("archive-v1"), names.index("load"))

    def test_over_threshold_synthesis_stops_with_estimate_and_exact_scoped_resume(self):
        self.all_pending()
        self.cost["synthesize"] = 500
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["status"]), ("deep_context", "waiting"))
        self.assertEqual(result["retry_command"], self.retry)
        self.assertEqual(result["installer_pid"], 0)
        self.assertEqual(result["action"]["step"], "synthesize")
        self.assertEqual(result["action"]["estimate"]["pending"], 1)
        self.assertEqual(result["action"]["continue_command"], self.retry + " --approve-spend synthesize")
        for stage in PAID:
            self.assertFalse(self.did(stage))
        self.assertFalse(self.indexed())

    def test_resuming_synthesis_stops_at_the_next_large_estimate(self):
        self.all_pending()
        self.cost["synthesize"] = 500
        self.cost["dedupe"] = 500
        self.run_pipeline()
        self.calls.clear()
        result = self.run_pipeline("synthesize")
        self.assertTrue(self.did("synthesize"))
        self.assertFalse(self.did("dedupe"))
        self.assertEqual(result["action"]["step"], "dedupe")
        self.assertEqual(result["action"]["continue_command"], self.retry + " --approve-spend dedupe")
        self.assertNotIn("--approve-spend", result["retry_command"])

    def test_over_threshold_enrichment_stops_before_any_provider_without_approval(self):
        self.pending["enrich"] = True
        self.cost["enrich"] = 500
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["action"]["step"]), ("enrich", "enrich"))
        self.assertFalse(self.did("enrich"))
        self.assertFalse(self.indexed())

    def test_routine_processing_runs_at_automatic_budget_boundaries(self):
        self.all_pending()
        for name in PAID:
            self.cost[name] = 499.99
        self.assertEqual(self.run_pipeline()["status"], "completed")
        self.assertCountEqual(self.paid_commands, [*PAID, "index"])

    def test_local_stages_never_spawn_subprocess_and_cached_index_revalidates_locally(self):
        self.all_pending()
        # setUp's subprocess fake accepts only the Modal commands.
        self.assertEqual(self.run_pipeline()["status"], "completed")
        self.calls.clear()
        with patch.object(pipeline.subprocess, "run", side_effect=AssertionError("Completed index must be reused")):
            result = self.run_pipeline()
            self.assertEqual((result["step"], result["status"]), ("ready", "completed"))
        self.assertFalse(self.indexed())
        self.assertEqual(self.calls[-1][0], "search-validate")

    def test_direct_runner_stops_on_native_payload_failures_and_action_requests(self):
        states = (("needs_approval", "waiting"), ("needs_user_action", "waiting"),
                  ("blocked_user_action", "waiting"), ("failed", "failed"), ("fail", "failed"),
                  ("missing", "failed"), ("blocked", "failed"), ("error", "failed"),
                  ("not_ready", "failed"), ("not-ready", "failed"))
        for native_state, expected_state in states:
            with self.subTest(native_state=native_state):
                flow = ProcessingOnboarding(self.root)
                payload = {"status": native_state, "reason": "Synthetic native stop"}
                with self.assertRaises(pipeline._Stopped):
                    flow._run("synthetic-stage", lambda: payload, "discover.reading")
                result = self.status.read()
                self.assertEqual(result["status"], expected_state)
                self.assertEqual(result["installer_pid"], 0)
                self.assertEqual(result["action"]["details"], payload)
                self.assertNotIn("ready", result["steps"])

    def test_direct_runner_captures_native_output_in_installation_log(self):
        def operation():
            print("Synthetic native output")
            print("Synthetic native progress", file=sys.stderr)
            return {"status": "completed"}

        result = ProcessingOnboarding(self.root)._run("synthetic-stage", operation, "discover.reading")
        self.assertEqual(result["status"], "completed")
        log = self.status.log_path.read_text()
        self.assertIn("[install] synthetic-stage", log)
        self.assertIn("Synthetic native output", log)
        self.assertIn("Synthetic native progress", log)

    def test_a_failed_stage_is_recorded_with_its_manifest_error(self):
        with patch.object(pipeline.Collect, "run",
                          return_value=SimpleNamespace(status="failed", counts={"bundles": 0}, error="chat.db unreadable")):
            result = self.run_pipeline()
        self.assertEqual((result["step"], result["status"]), ("deep_context", "failed"))
        self.assertEqual(result["action"]["details"]["error"], "chat.db unreadable")
        self.assertFalse(self.did("synthesize-estimate"))

    def test_large_index_asks_only_for_the_spend_then_finishes(self):
        self.index_cost = 600
        first = self.run_pipeline()
        self.assertEqual(first["action"]["continue_command"], self.retry + " --approve-spend index")
        second = self.run_pipeline("index")
        self.assertEqual((second["step"], second["status"]), ("ready", "completed"))

    def test_index_builds_without_asking_to_upload(self):
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["status"]), ("ready", "completed"))
        self.assertTrue(self.indexed())

    def test_index_native_estimate_uses_automatic_budget_and_retains_large_cost_approval(self):
        self.index_cost = 499.99
        result = self.run_pipeline()
        self.assertEqual(result["status"], "completed")
        argv = next(options["argv"] for name, options in self.calls if name == "index")
        self.assertEqual(float(argv[argv.index("--max-usd") + 1]), 499.99)
        self.reset_pipeline(self.root / "large-index-estimate")
        self.index_cost = 500
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["status"]), ("index", "waiting"))
        self.assertEqual(result["action"]["step"], "index")
        self.assertEqual(result["action"]["estimate"]["estimated_cost_usd"], 500)
        self.assertEqual(result["action"]["continue_command"], self.retry + " --approve-spend index")
        self.assertFalse(self.indexed())
        self.calls.clear()
        self.assertEqual(self.run_pipeline("index")["status"], "completed")
        argv = next(options["argv"] for name, options in self.calls if name == "index")
        self.assertEqual(float(argv[argv.index("--max-usd") + 1]), 500)

    def test_full_approved_run_uses_every_stage_then_only_validator_marks_ready(self):
        self.all_pending()
        result = self.run_pipeline(*PAID, "index")
        for stage in ("load", "collect", *PAID, "realize"):
            self.assertTrue(self.did(stage), stage)
        self.assertTrue(self.indexed())
        self.assertEqual((result["step"], result["status"]), ("ready", "completed"))
        self.assertNotIn("review", result["plan"])
        self.assertEqual(result["retry_command"], self.retry)
        self.assertEqual(self.calls[-1][0], "search-validate")
        self.assertEqual(result["action"]["kind"], "details")  # no review offer: the page counts them live

    def test_completed_index_resumes_without_rebuild_and_revalidates(self):
        self.run_pipeline("index")
        self.calls.clear()
        result = self.run_pipeline()
        self.assertFalse(self.indexed())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.calls[-1][0], "search-validate")
        self.calls.clear()
        self.assertEqual(self.run_pipeline()["status"], "completed")
        self.assertFalse(self.indexed())

    def test_invalid_existing_index_fails_until_explicit_rebuild(self):
        self.run_pipeline("index")
        self.validation_status = "fail"
        self.calls.clear()
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["status"]), ("validate", "failed"))
        self.assertFalse(self.indexed())
        self.validation_status = "ok"
        self.calls.clear()
        result = self.run_pipeline("index")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(self.indexed())

    def test_roster_change_prevents_old_index_reuse(self):
        self.run_pipeline("index")
        self.csv = "id,full_name\nsynthetic-casey,Casey Example\n"
        self.calls.clear()
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["status"]), ("ready", "completed"))
        self.assertTrue(self.indexed())  # rebuilt for the new roster, not reused

    def test_native_failure_is_logged_and_stops_before_paid_work(self):
        self.failing_command = "collect"
        result = self.run_pipeline("synthesize", "index")
        self.assertEqual(result["status"], "failed")
        self.assertIn("source read failed", self.status.log_path.read_text())
        self.assertFalse(self.did("synthesize"))
        self.assertFalse(self.indexed())

    def test_a_step_that_exits_is_a_recorded_failure_with_its_reason(self):
        self.exiting_command = "collect"
        result = self.run_pipeline("synthesize", "index")
        self.assertEqual(result["status"], "failed")
        self.assertIn("PARALLEL_API_KEY not set", result["action"]["details"]["error"])
        self.assertFalse(self.indexed())

    def test_ready_keeps_the_search_check_for_the_agent(self):
        result = self.run_pipeline("index")
        self.assertEqual(result["event"], "search.ready")
        self.assertEqual(result["action"]["details"]["validation"]["status"], "ok")
        self.assertEqual(result["action"]["details"]["left_to_fix"], [])

    def test_research_that_cannot_run_is_skipped_and_the_index_still_builds(self):
        self.pending["enrich"] = True
        self.exiting_command = "enrich"
        result = self.run_pipeline("index")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps"]["enrich"]["status"], "skipped")
        self.assertIn("PARALLEL_API_KEY not set", result["action"]["details"]["left_to_fix"][0]["error"])
        self.assertIn("Research and LinkedIn matching didn't finish", result["note"])
        self.assertTrue(self.indexed())

    def test_enrichment_that_fails_still_builds_the_index_and_says_what_to_fix(self):
        self.pending["enrich"] = True
        self.failing_command = "enrich"
        result = self.run_pipeline("index")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps"]["enrich"]["status"], "skipped")
        self.assertTrue(self.indexed())
        self.assertIn("source read failed", result["action"]["details"]["left_to_fix"][0]["error"])

    def test_each_stage_failure_resumes_without_repeating_completed_paid_work(self):
        self.all_pending()
        self.assertEqual(self.run_pipeline(*PAID, "index")["status"], "completed")
        baseline = self.calls.copy()
        matrix_root = self.root
        for position, command in enumerate(baseline, 1):
            with self.subTest(command=command, position=position):
                self.reset_pipeline(matrix_root / str(position))
                self.all_pending()
                self.fail_at = position
                result = self.run_pipeline(*PAID, "index")
                if command[0] in ("enrich", "enrich-estimate"):
                    # Search is built without a deferred step; it is skipped, not failed.
                    self.assertEqual((result["step"], result["status"]), ("ready", "completed"))
                    self.assertEqual(result["steps"]["enrich"]["status"], "skipped")
                    self.fail_at = 0
                    continue
                self.assertEqual(result["status"], "failed")
                self.assertEqual(len(self.calls), position)
                self.assertNotIn("ready", result["steps"])
                self.assertEqual(result["installer_pid"], 0)
                self.assertEqual(result["retry_command"], self.retry)
                indexed = (self.index / "manifest.json").is_file()
                self.fail_at = 0
                self.calls.clear()
                spend = [stage for stage in PAID if self.pending[stage]]
                if not indexed:
                    spend.append("index")
                result = self.run_pipeline(*spend)
                self.assertEqual((result["step"], result["status"]), ("ready", "completed"))
                self.assertEqual(self.indexed(), not indexed)
                self.assertCountEqual(self.paid_commands, [*PAID, "index"])
                self.calls.clear()
                self.assertEqual(self.run_pipeline()["status"], "completed")
                self.assertFalse(self.indexed())
                self.assertEqual(len(self.paid_commands), 5)

    def test_missing_owner_is_built_from_the_linkedin_session_and_gmail_address(self):
        (self.root / ".powerpacks/deep-context/owner.json").unlink()
        self.write(self.root / ".powerpacks/network-import/discover/linkedin/connections.json",
                   json.dumps({"complete": True, "owner_url": "https://www.linkedin.com/in/casey-owner"}))
        self.run_pipeline()
        owner = next(options for name, options in self.calls if name == "owner")
        self.assertEqual((owner["url"], owner["emails"]),
                         ("https://www.linkedin.com/in/casey-owner", ["casey@example.com"]))
        self.assertTrue(self.did("collect"))

    def test_missing_owner_without_a_linkedin_session_hands_off(self):
        (self.root / ".powerpacks/deep-context/owner.json").unlink()
        result = self.run_pipeline()
        self.assertEqual(result["action"]["kind"], "owner")
        self.assertEqual(result["status"], "waiting")
        self.assertFalse(self.did("owner"))
        self.assertFalse(self.did("load"))

    def dispatch(self):
        self.write(self.people, self.csv)
        started = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
        self.write(self.root / ".powerpacks/runs/setup-gmail-modal/status.json",
                   json.dumps({"status": "running", "current_stage": "indexing", "started_at": started,
                               "stages": {"indexing": {"payload": {"sandbox": "synthetic-sandbox"}}}}))

    def test_failed_modal_run_is_redispatched_not_downloaded_again(self):
        self.dispatch()
        path = self.root / ".powerpacks/runs/setup-gmail-modal/status.json"
        record = json.loads(path.read_text())
        record["status"] = "failed"
        record["stages"]["indexing"]["payload"] = {"phase": "pipeline", "error": "sandbox exited 1"}
        path.write_text(json.dumps(record))
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["status"]), ("ready", "completed"))
        self.assertFalse(self.did("download"))
        self.assertTrue(self.indexed())

    def test_interrupted_modal_dispatch_downloads_existing_job_without_new_spend(self):
        self.dispatch()
        result = self.run_pipeline()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(self.indexed())
        download = next(options["argv"] for name, options in self.calls if name == "download")
        self.assertEqual(download[-5:], ["--label", "gmail-index", "--wait", "--dest", ".powerpacks/search-index"])
        self.assertEqual(self.calls[-1][0], "search-validate")

    def test_failed_native_download_resumes_existing_dispatch_without_new_spend(self):
        self.dispatch()
        self.failing_command = "download"
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["status"]), ("index", "failed"))
        self.assertEqual(self.calls[-1][0], "download")
        self.assertFalse(self.indexed())
        self.failing_command = ""
        self.calls.clear()
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["status"]), ("ready", "completed"))
        self.assertFalse(self.indexed())
        self.assertEqual(self.paid_commands, [])
        self.assertTrue(self.did("download"))

    def test_modal_failure_and_native_cost_gate_stop_then_resume(self):
        matrix_root = self.root
        for returncode, expected_status in ((1, "failed"), (20, "waiting")):
            with self.subTest(returncode=returncode):
                self.reset_pipeline(matrix_root / str(returncode))
                payload = {"status": "failed" if returncode == 1 else "needs_approval",
                           "estimated_cost_usd": 30}
                with patch.object(pipeline.subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], returncode, json.dumps(payload))) as modal:
                    result = self.run_pipeline()
                self.assertEqual((result["step"], result["status"]), ("index", expected_status))
                self.assertEqual(result["action"]["details"]["returncode"], returncode)
                self.assertEqual(modal.call_count, 1)
                self.assertNotIn("ready", result["steps"])
                self.assertFalse(self.did("search-validate"))
                self.calls.clear()
                self.assertEqual(self.run_pipeline()["status"], "completed")
                self.assertCountEqual(self.paid_commands, ["index"])

    def test_saved_native_estimate_under_500_resumes_without_cost_confirmation(self):
        self.dispatch()
        path = self.root / ".powerpacks/runs/setup-gmail-modal/status.json"
        dispatch = json.loads(path.read_text())
        dispatch["status"] = "failed"
        dispatch["stages"]["indexing"]["payload"] = {
            "phase": "estimate", "error": "estimate exceeds --max-usd cap",
            "estimated_usd": 499.99, "max_usd": 25}
        path.write_text(json.dumps(dispatch))
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["status"]), ("ready", "completed"))
        self.assertFalse(self.did("download"))
        self.assertFalse(self.did("index-estimate"))
        argv = next(options["argv"] for name, options in self.calls if name == "index")
        self.assertEqual(float(argv[argv.index("--max-usd") + 1]), 499.99)
        self.assertCountEqual(self.paid_commands, ["index"])

    def test_native_cap_failure_resumes_same_uploaded_contacts_with_exact_estimate(self):
        self.dispatch()
        path = self.root / ".powerpacks/runs/setup-gmail-modal/status.json"
        dispatch = json.loads(path.read_text())
        dispatch["status"] = "failed"
        dispatch["stages"]["indexing"]["payload"] = {
            "phase": "estimate", "error": "estimate exceeds --max-usd cap",
            "estimated_usd": 500, "max_usd": 499.99}
        path.write_text(json.dumps(dispatch))
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["status"]), ("index", "waiting"))
        self.assertEqual(result["action"]["step"], "index")
        self.assertEqual(result["action"]["estimate"]["estimated_usd"], 500)
        self.assertEqual(result["action"]["continue_command"], self.retry + " --approve-spend index")
        self.assertFalse(self.indexed())
        self.assertFalse(self.did("download"))
        self.calls.clear()
        result = self.run_pipeline("index")
        self.assertEqual((result["step"], result["status"]), ("ready", "completed"))
        self.assertFalse(self.did("download"))
        self.assertFalse(self.did("index-estimate"))
        argv = next(options["argv"] for name, options in self.calls if name == "index")
        self.assertEqual(float(argv[argv.index("--max-usd") + 1]), 500)
        self.assertCountEqual(self.paid_commands, ["index"])
        self.calls.clear()
        self.assertEqual(self.run_pipeline()["status"], "completed")
        self.assertFalse(self.indexed())

    def test_paid_outputs_completed_before_command_failure_are_reused_on_resume(self):
        matrix_root = self.root
        for stage in (*PAID, "index"):
            with self.subTest(stage=stage):
                self.reset_pipeline(matrix_root / stage)
                self.all_pending()
                self.failure_after = stage
                result = self.run_pipeline(*PAID, "index")
                if stage == "enrich":
                    # Enrichment failing is deferred: search is still built.
                    self.assertEqual((result["status"], self.indexed()), ("completed", True))
                    self.failure_after = ""
                    continue
                self.assertEqual(result["status"], "failed")
                self.assertIn(stage, self.paid_commands)
                self.failure_after = ""
                self.calls.clear()
                spend = [name for name in PAID if self.pending[name]]
                if stage != "index":
                    spend.append("index")
                result = self.run_pipeline(*spend)
                self.assertEqual((result["step"], result["status"]), ("ready", "completed"))
                self.assertEqual(self.indexed(), stage != "index")
                self.assertCountEqual(self.paid_commands, [*PAID, "index"])

    def test_changed_roster_never_redispatches_unknown_previous_job_even_when_approved(self):
        self.dispatch()
        self.csv = "id,full_name\nsynthetic-casey,Casey Example\n"
        result = self.run_pipeline("index")
        self.assertEqual((result["step"], result["status"]), ("index", "waiting"))
        self.assertEqual(result["action"]["kind"], "recovery")
        self.assertEqual(result["installer_pid"], 0)
        self.assertFalse(self.indexed())
        self.assertFalse(self.did("download"))

    def test_finished_remote_job_with_interrupted_download_is_recovered(self):
        self.dispatch()
        path = self.root / ".powerpacks/runs/setup-gmail-modal/status.json"
        record = json.loads(path.read_text())
        record["status"] = "completed"
        path.write_text(json.dumps(record))
        result = self.run_pipeline()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(self.did("download"))
        self.assertFalse(self.indexed())

    def test_validator_failure_never_claims_ready(self):
        self.validation_status = "fail"
        result = self.run_pipeline("index")
        self.assertEqual((result["step"], result["status"]), ("validate", "failed"))
        self.assertNotIn("ready", result["steps"])

    def test_local_validation_preserves_verified_hosted_network_count(self):
        self.status.write('step.waiting', step=InstallStep.DEEP_CONTEXT, pid=0, retry_command=self.retry, network_name="Personal Network", person_count=328)
        result = self.run_pipeline("index")
        self.assertEqual(result["person_count"], 328)
        self.assertIn("1 people searchable", result["message"])


if __name__ == "__main__":
    unittest.main()

"""Native primitive doubles prove installer consent and resume boundaries."""
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

from packs.powerset.primitives.install import pipeline
from packs.powerset.primitives.install.pipeline import ProcessingOnboarding
from packs.powerset.primitives.install.status import InstallState, InstallStatus, InstallStep


class InstallPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.reset_pipeline(Path(self.temp.name))
        stages = (
            (pipeline.PeopleMerge, "run", "fan-in", True),
            (pipeline.EnsureParents, "run", "ensure-parents", True),
            (pipeline.CheckReadiness, "run", "check", False),
            (pipeline.Seed, "run", "seed", True),
            (pipeline.BuildOwner, "run", "owner", True),
            (pipeline.CollectPersonContext, "run", "collect", True),
            (pipeline.SynthesizePersonContext, "estimate", "synthesize-estimate", False),
            (pipeline.SynthesizePersonContext, "run", "synthesize", True),
            (pipeline.ComposeDossier, "run", "compose", True),
            (pipeline.ValidateDossiers, "run", "validate", False),
            (pipeline.ClusterMergeCandidates, "estimate", "cluster-estimate", False),
            (pipeline.ClusterMergeCandidates, "run", "cluster", True),
            (pipeline.BuildParents, "run", "parents", True),
            (pipeline.EnrichmentPipeline, "run", "enrich", False),
            (pipeline.PrefetchProfiles, "run", "profile-prefetch", True),
            (pipeline.ExportPeople, "run", "realize", False),
        )
        for primitive, method, name, serialized in stages:
            def operation(node, *args, name=name, serialized=serialized, **kwargs):
                payload = self.native(name, node=node, **kwargs)
                return SimpleNamespace(to_payload=lambda: payload) if serialized else payload
            mocked = patch.object(primitive, method, autospec=True, side_effect=operation)
            mocked.start()
            self.addCleanup(mocked.stop)
        functions = {
            "readiness_payload": lambda report: report,
            "workflow_state": self.workflow_state,
            "estimate_enrichment": self.estimate_enrichment,
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
        sleep = patch.object(pipeline.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def reset_pipeline(self, root):
        self.root = root.resolve()
        self.status = InstallStatus(self.root)
        self.retry = "bin/onboard --source gmail --gmail-email casey@example.com --sync-after 2025-01-01"
        self.status.write(step=InstallStep.DEEP_CONTEXT, status=InstallState.WAITING,
                          message="Ready", pid=os.getpid(), retry_command=self.retry)
        owner = self.root / ".powerpacks/deep-context/owner.json"
        owner.parent.mkdir(parents=True)
        owner.write_text("{}")
        self.people = self.root / ".powerpacks/network-import/merged/people.csv"
        self.index = self.root / ".powerpacks/search-index"
        self.csv = "id,full_name\nsynthetic-jordan,Jordan Bravo\n"
        self.synthesize = False
        self.cluster = False
        self.enrich = False
        self.review = False
        self.waits = 0
        self.seed = False
        self.profiles_missing = False
        self.validation_status = "ok"
        self.calls = []
        self.paid_commands = []
        self.failing_command = ""
        self.fail_at = 0
        self.failure_after = ""
        self.synthesis_cost = 0.1
        self.cluster_cost = 0.01
        self.enrichment_cost = 0.2
        self.index_cost = 0.5

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def native(self, command, *, node=None, **kwargs):
        self.calls.append((command, {"node": node, **kwargs}))
        payload = {"status": "completed"}
        if command == self.failing_command or len(self.calls) == self.fail_at:
            raise RuntimeError("[synthetic] source read failed")
        if command == "fan-in":
            self.write(self.people, self.csv)
        elif command == "check":
            payload = {"status": "completed", "checks": {
                "canonical_sqlite": {"status": "seed_required" if self.seed else "ok"},
                "owner_json": {"status": "absent" if not (self.root / ".powerpacks/deep-context/owner.json").exists() else "present"}},
                "next_command": "bin/deep-context owner --linkedin-url <url> --email <email>"}
        elif command == "seed":
            self.seed = False
        elif command == "collect":
            self.write(self.root / ".powerpacks/deep-context/raw/manifest.json", json.dumps(payload))
        elif command == "synthesize-estimate":
            payload = {"status": "dry_run", "people": int(self.synthesize), "jev_people": int(self.synthesize),
                       "estimated_cost_ceiling_usd": self.synthesis_cost if self.synthesize else 0}
        elif command == "cluster-estimate":
            payload = {"status": "dry_run", "estimated_input_tokens": 100 if self.cluster else 0,
                       "estimated_cost_usd": self.cluster_cost if self.cluster else 0}
        elif command == "enrich-estimate":
            payload = {"status": "dry_run", "estimated_usd": self.enrichment_cost,
                       "would_submit": 1, "judgment_count": 1}
        elif command in ("synthesize", "cluster", "enrich"):
            if getattr(self, command):
                self.paid_commands.append(command)
            setattr(self, command, False)
        elif command == "review-status":
            if self.status.read()["step"] == "review":
                self.assertEqual(self.status.read()["status"], "waiting")
                self.assertEqual(self.status.read()["installer_pid"], os.getpid())
                self.waits += 1
                if self.waits >= 2:
                    self.review = False
            payload = {"status": "waiting" if self.review else "ok",
                       "next_action": "enrich" if self.enrich else "review_linkedin" if self.review else "realize"}
        elif command == "realize":
            self.write(self.people, self.csv)
            payload["profiles_missing"] = int(self.profiles_missing)
        elif command == "profile-prefetch":
            self.assertTrue(node.fetch)
            self.paid_commands.append("profile-prefetch")
            self.profiles_missing = False
        elif command == "index-estimate":
            options = kwargs["options"]
            self.assertEqual(options.input, self.people)
            self.assertEqual(options.output_dir, self.index)
            self.assertTrue(options.dry_run)
            payload = {"status": "dry_run", "estimated_cost_usd": self.index_cost,
                       "estimated_paid_calls": {"role_enrichment": 1}}
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

    def workflow_state(self, db):
        self.assertEqual(db.db_path, self.root / ".powerpacks/deep-context/deep-context.sqlite")
        payload = self.native("review-status")
        return SimpleNamespace(next_action=payload["next_action"], selection="synthetic-selection")

    def estimate_enrichment(self, *args, **kwargs):
        payload = self.native("enrich-estimate")
        return SimpleNamespace(to_payload=lambda: payload, estimated_usd=payload["estimated_usd"],
                               research=SimpleNamespace(deduped_total=1, estimated_usd=0.2,
                                                        request_fingerprint="synthetic-request"))

    def external(self, argv, **kwargs):
        if argv[0] == "open":
            return subprocess.CompletedProcess(argv, 0)
        self.assertIn("packs/indexing/modal/linkedin_modal_pipeline.py", argv)
        self.assertEqual(kwargs["cwd"], self.root)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        payload = self.native("download" if "download" in argv else "index", argv=argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload))

    def run_pipeline(self, *spend, upload=False):
        return ProcessingOnboarding(self.root, approved_spend=spend, approve_upload=upload).run()

    def did(self, stage):
        return any(name == stage for name, _ in self.calls)

    def indexed(self):
        return self.did("index")

    def test_over_threshold_synthesis_stops_with_estimate_and_exact_scoped_resume(self):
        self.synthesize = True
        self.synthesis_cost = 500
        self.cluster = True
        self.enrich = True
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["status"]), ("deep_context", "waiting"))
        self.assertEqual(result["retry_command"], self.retry)
        self.assertEqual(result["installer_pid"], 0)
        self.assertEqual(result["action"]["step"], "synthesize")
        self.assertEqual(result["action"]["estimate"]["people"], 1)
        self.assertEqual(result["action"]["continue_command"], self.retry + " --approve-spend synthesize")
        self.assertFalse(self.did("synthesize"))
        self.assertFalse(self.did("cluster"))
        self.assertFalse(self.did("enrich"))
        self.assertFalse(self.indexed())

    def test_resuming_synthesis_preserves_collected_output_and_stops_at_cluster(self):
        self.synthesize = self.cluster = self.enrich = True
        self.synthesis_cost = 500
        self.cluster_cost = 500
        self.run_pipeline()
        self.calls.clear()
        result = self.run_pipeline("synthesize")
        self.assertTrue(self.did("synthesize"))
        self.assertFalse(self.did("collect"))
        self.assertFalse(self.did("cluster"))
        self.assertEqual(result["action"]["step"], "cluster")
        self.assertEqual(result["action"]["continue_command"], self.retry + " --approve-spend cluster")
        self.assertNotIn("--approve-spend", result["retry_command"])

    def test_over_threshold_enrichment_stops_before_any_provider_without_approval(self):
        self.enrich = True
        self.enrichment_cost = 500
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["action"]["step"]), ("enrich", "enrich"))
        self.assertFalse(self.did("enrich"))
        self.assertFalse(self.indexed())

    def test_routine_processing_runs_at_automatic_budget_boundaries(self):
        self.synthesize = self.cluster = self.enrich = True
        self.synthesis_cost = 499.99
        self.cluster_cost = self.enrichment_cost = 499.99
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["status"]), ("index", "waiting"))
        self.assertCountEqual(self.paid_commands, ["synthesize", "cluster", "enrich"])
        self.assertEqual(result["action"]["continue_command"], self.retry + " --approve-upload")
        self.subprocess.assert_not_called()
        self.assertEqual(self.run_pipeline(upload=True)["status"], "completed")
        self.assertCountEqual(self.paid_commands, ["synthesize", "cluster", "enrich", "index"])

    def test_local_stages_never_spawn_subprocess_and_cached_index_revalidates_locally(self):
        self.synthesize = self.cluster = self.enrich = True
        with patch.object(pipeline.subprocess, "run", side_effect=AssertionError("Local stages must run in process")):
            result = self.run_pipeline()
            self.assertEqual((result["step"], result["status"]), ("index", "waiting"))
        self.assertEqual(self.run_pipeline(upload=True)["status"], "completed")
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
                    flow._run("synthetic-stage", lambda: payload, "Reading synthetic input")
                result = self.status.read()
                self.assertEqual(result["status"], expected_state)
                self.assertEqual(result["installer_pid"], 0)
                self.assertEqual(result["action"]["result"], payload)
                self.assertNotIn("ready", result["steps"])

    def test_direct_runner_captures_native_output_in_installation_log(self):
        def operation():
            print("Synthetic native output")
            print("Synthetic native progress", file=sys.stderr)
            return {"status": "completed"}

        result = ProcessingOnboarding(self.root)._run("synthetic-stage", operation, "Reading synthetic input")
        self.assertEqual(result["status"], "completed")
        log = self.status.log_path.read_text()
        self.assertIn("[install] synthetic-stage", log)
        self.assertIn("Synthetic native output", log)
        self.assertIn("Synthetic native progress", log)

    def test_index_requires_upload_consent_without_redundant_spend_approval(self):
        for spend in ((), ("index",)):
            self.calls.clear()
            result = self.run_pipeline(*spend)
            self.assertEqual((result["step"], result["status"]), ("index", "waiting"))
            self.assertEqual(result["action"]["step"], "index")
            self.assertTrue(result["action"]["upload"])
            self.assertEqual(result["action"]["continue_command"], self.retry + " --approve-upload")
            self.assertFalse(self.indexed())
        self.calls.clear()
        self.assertEqual(self.run_pipeline(upload=True)["status"], "completed")
        self.assertTrue(self.indexed())

    def test_index_native_estimate_uses_automatic_budget_and_retains_large_cost_approval(self):
        self.index_cost = 499.99
        result = self.run_pipeline(upload=True)
        self.assertEqual(result["status"], "completed")
        argv = next(options["argv"] for name, options in self.calls if name == "index")
        self.assertEqual(float(argv[argv.index("--max-usd") + 1]), 499.99)
        self.reset_pipeline(self.root / "large-index-estimate")
        self.index_cost = 500
        result = self.run_pipeline(upload=True)
        self.assertEqual((result["step"], result["status"]), ("index", "waiting"))
        self.assertEqual(result["action"]["step"], "index")
        self.assertFalse(result["action"]["upload"])
        self.assertEqual(result["action"]["estimate"]["estimated_cost_usd"], 500)
        self.assertEqual(result["action"]["continue_command"],
                         self.retry + " --approve-spend index --approve-upload")
        self.assertFalse(self.indexed())
        self.calls.clear()
        self.assertEqual(self.run_pipeline("index", upload=True)["status"], "completed")
        argv = next(options["argv"] for name, options in self.calls if name == "index")
        self.assertEqual(float(argv[argv.index("--max-usd") + 1]), 500)

    def test_full_approved_run_uses_native_stages_then_only_validator_marks_ready(self):
        self.synthesize = self.cluster = self.enrich = True
        result = self.run_pipeline("synthesize", "cluster", "enrich", "index", upload=True)
        self.assertTrue(self.did("synthesize"))
        self.assertTrue(self.did("cluster"))
        self.assertTrue(self.did("enrich"))
        self.assertTrue(self.indexed())
        self.assertEqual((result["step"], result["status"]), ("ready", "completed"))
        self.assertNotIn("review", result["plan"])
        self.assertEqual(result["retry_command"], self.retry)
        self.assertEqual(self.calls[-1][0], "search-validate")

    def test_completed_native_index_resumes_without_reupload_and_revalidates(self):
        self.run_pipeline("index", upload=True)
        self.calls.clear()
        result = self.run_pipeline()
        self.assertFalse(self.indexed())
        self.assertFalse(self.did("collect"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.calls[-1][0], "search-validate")
        self.calls.clear()
        self.assertEqual(self.run_pipeline()["status"], "completed")
        self.assertFalse(self.indexed())

    def test_invalid_existing_index_fails_until_explicit_rebuild(self):
        self.run_pipeline("index", upload=True)
        self.validation_status = "fail"
        self.calls.clear()
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["status"]), ("validate", "failed"))
        self.assertFalse(self.indexed())
        self.validation_status = "ok"
        self.calls.clear()
        result = self.run_pipeline("index", upload=True)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(self.indexed())

    def test_roster_change_prevents_old_index_reuse(self):
        self.run_pipeline("index", upload=True)
        self.csv = "id,full_name\nsynthetic-casey,Casey Example\n"
        self.calls.clear()
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["status"]), ("index", "waiting"))
        self.assertFalse(self.indexed())

    def test_review_keeps_waiting_until_native_decisions_change(self):
        self.review = True
        result = self.run_pipeline("index", upload=True)
        self.assertEqual(self.waits, 2)
        self.assertTrue(self.sleep.called)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps"]["review"]["status"], "completed")
        self.assertIn("review", result["plan"])
        self.assertEqual(result["plan"].index("review") + 1, result["plan"].index("index"))

    def test_native_failure_is_logged_and_stops_before_paid_work(self):
        self.failing_command = "collect"
        result = self.run_pipeline("synthesize", "index", upload=True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("source read failed", self.status.log_path.read_text())
        self.assertFalse(self.did("synthesize"))
        self.assertFalse(self.indexed())

    def test_each_native_stage_failure_resumes_without_repeating_completed_paid_work(self):
        self.synthesize = self.cluster = self.enrich = self.seed = self.review = True
        self.assertEqual(self.run_pipeline("synthesize", "cluster", "enrich", "index", upload=True)["status"],
                         "completed")
        baseline = self.calls.copy()
        matrix_root = self.root
        for position, command in enumerate(baseline, 1):
            with self.subTest(command=command, position=position):
                self.reset_pipeline(matrix_root / str(position))
                self.synthesize = self.cluster = self.enrich = self.seed = self.review = True
                self.fail_at = position
                result = self.run_pipeline("synthesize", "cluster", "enrich", "index", upload=True)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(len(self.calls), position)
                self.assertNotIn("ready", result["steps"])
                self.assertEqual(result["installer_pid"], 0)
                self.assertEqual(result["retry_command"], self.retry)
                collected = (self.root / ".powerpacks/deep-context/raw/manifest.json").is_file()
                indexed = (self.index / "manifest.json").is_file()
                self.fail_at = 0
                self.calls.clear()
                spend = [stage for stage in ("synthesize", "cluster", "enrich") if getattr(self, stage)]
                if not indexed:
                    spend.append("index")
                result = self.run_pipeline(*spend, upload=True)
                self.assertEqual((result["step"], result["status"]), ("ready", "completed"))
                self.assertEqual(self.did("collect"), not collected)
                self.assertEqual(self.indexed(), not indexed)
                self.assertCountEqual(self.paid_commands, ["synthesize", "cluster", "enrich", "index"])
                self.calls.clear()
                self.assertEqual(self.run_pipeline()["status"], "completed")
                self.assertFalse(self.did("collect"))
                self.assertFalse(self.indexed())
                self.assertEqual(len(self.paid_commands), 4)

    def test_missing_owner_hands_off_without_silently_fetching_profile(self):
        (self.root / ".powerpacks/deep-context/owner.json").unlink()
        result = self.run_pipeline()
        self.assertEqual(result["action"]["kind"], "owner")
        self.assertEqual(result["status"], "waiting")
        self.assertFalse(self.did("owner"))
        self.assertFalse(self.did("collect"))

    def test_legacy_seed_runs_once_then_rechecks(self):
        self.seed = True
        result = self.run_pipeline()
        self.assertTrue(self.did("seed"))
        self.assertEqual(result["action"]["step"], "index")
        self.calls.clear()
        self.run_pipeline()
        self.assertFalse(self.did("seed"))

    def test_missing_profiles_are_fetched_in_the_same_flow(self):
        self.profiles_missing = True
        result = self.run_pipeline(upload=True)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(any(name == "profile-prefetch" and options["node"].fetch for name, options in self.calls))

    def test_profile_fetch_failure_resumes_without_repeating_completed_work(self):
        self.profiles_missing = True
        self.failing_command = "profile-prefetch"
        self.assertEqual(self.run_pipeline(upload=True)["status"], "failed")
        self.assertFalse(self.indexed())
        self.failing_command = ""
        self.calls.clear()
        self.assertEqual(self.run_pipeline(upload=True)["status"], "completed")
        self.assertFalse(self.did("collect"))
        self.assertCountEqual(self.paid_commands, ["profile-prefetch", "index"])
        self.calls.clear()
        self.assertEqual(self.run_pipeline()["status"], "completed")
        self.assertFalse(self.did("profile-prefetch"))
        self.assertFalse(self.indexed())

    def test_saved_wacli_store_reaches_native_readiness_and_collection(self):
        store = self.root / "synthetic-whatsapp-store"
        self.status.write(step=InstallStep.DEEP_CONTEXT, status=InstallState.WAITING,
                          message="Ready", pid=0, retry_command=self.retry + f" --wacli-store {store}")
        self.run_pipeline()
        for stage in ("check", "collect"):
            node = next(options["node"] for name, options in self.calls if name == stage)
            self.assertEqual(node.wacli_db, store / "wacli.db")

    def test_review_reuses_requested_installation_port(self):
        self.review = True
        result = ProcessingOnboarding(self.root, approved_spend=("index",),
                                      approve_upload=True, port=8899).run()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(any(call.args[0] == ["open", "http://127.0.0.1:8899/?stage=linkedin"]
                            for call in self.subprocess.call_args_list))

    def dispatch(self):
        self.write(self.people, self.csv)
        started = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
        self.write(self.root / ".powerpacks/runs/setup-gmail-modal/status.json",
                   json.dumps({"status": "running", "current_stage": "indexing", "started_at": started,
                               "stages": {"indexing": {"payload": {"sandbox": "synthetic-sandbox"}}}}))

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
        self.assertFalse(self.did("collect"))
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
                    result = self.run_pipeline(upload=True)
                self.assertEqual((result["step"], result["status"]), ("index", expected_status))
                self.assertEqual(result["action"]["result"]["returncode"], returncode)
                self.assertEqual(modal.call_count, 1)
                self.assertNotIn("ready", result["steps"])
                self.assertFalse(self.did("search-validate"))
                self.calls.clear()
                self.assertEqual(self.run_pipeline(upload=True)["status"], "completed")
                self.assertFalse(self.did("collect"))
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
        self.assertFalse(result["action"]["upload"])
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
        for stage in ("synthesize", "cluster", "enrich", "index"):
            with self.subTest(stage=stage):
                self.reset_pipeline(matrix_root / stage)
                self.synthesize = self.cluster = self.enrich = True
                self.failure_after = stage
                self.assertEqual(self.run_pipeline("synthesize", "cluster", "enrich", "index", upload=True)["status"],
                                 "failed")
                self.assertIn(stage, self.paid_commands)
                self.failure_after = ""
                self.calls.clear()
                spend = [name for name in ("synthesize", "cluster", "enrich") if getattr(self, name)]
                if stage != "index":
                    spend.append("index")
                result = self.run_pipeline(*spend, upload=True)
                self.assertEqual((result["step"], result["status"]), ("ready", "completed"))
                self.assertFalse(self.did("collect"))
                self.assertEqual(self.indexed(), stage != "index")
                self.assertCountEqual(self.paid_commands, ["synthesize", "cluster", "enrich", "index"])

    def test_changed_roster_never_redispatches_unknown_previous_job_even_when_approved(self):
        self.dispatch()
        self.csv = "id,full_name\nsynthetic-casey,Casey Example\n"
        result = self.run_pipeline("index", upload=True)
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
        result = self.run_pipeline("index", upload=True)
        self.assertEqual((result["step"], result["status"]), ("validate", "failed"))
        self.assertNotIn("ready", result["steps"])

    def test_local_validation_preserves_verified_hosted_network_count(self):
        self.status.write(step=InstallStep.DEEP_CONTEXT, status=InstallState.WAITING,
                          message="Ready", pid=0, retry_command=self.retry,
                          network_name="Personal Network", person_count=328)
        result = self.run_pipeline("index", upload=True)
        self.assertEqual(result["person_count"], 328)
        self.assertIn("1 people searchable", result["message"])


if __name__ == "__main__":
    unittest.main()

"""Isolated native-command doubles prove installer consent and resume boundaries."""
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from packs.powerset.primitives.install.pipeline import ProcessingOnboarding
from packs.powerset.primitives.install.status import InstallState, InstallStatus, InstallStep


class InstallPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
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
        self.commands = []
        self.failing_command = ""
        self.patch = patch("packs.powerset.primitives.install.pipeline.subprocess.run", side_effect=self.native)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def native(self, argv, *, cwd, stdin, stdout, stderr, text):
        self.assertEqual(cwd, self.root)
        self.assertEqual(stdin, subprocess.DEVNULL)
        self.commands.append(argv)
        command = argv[1] if argv[0].endswith("bin/deep-context") else Path(argv[5]).name
        payload = {"status": "completed"}
        if command == self.failing_command:
            stderr.write("[synthetic] source read failed\n")
            return subprocess.CompletedProcess(argv, 1, json.dumps({"status": "failed"}))
        if command == "index_contacts_pipeline.py":
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
        elif command == "synthesize":
            if "--dry-run" in argv:
                payload = {"status": "dry_run", "people": int(self.synthesize), "jev_people": int(self.synthesize),
                           "estimated_cost_ceiling_usd": 0.1 if self.synthesize else 0}
            else:
                self.synthesize = False
        elif command == "cluster":
            if "--dry-run" in argv:
                payload = {"status": "dry_run", "estimated_input_tokens": 100 if self.cluster else 0,
                           "estimated_cost_usd": 0.01 if self.cluster else 0}
            else:
                self.cluster = False
        elif command == "enrich":
            if "--dry-run" in argv:
                payload = {"status": "dry_run", "estimated_usd": 0.2, "would_submit": 1}
            else:
                self.enrich = False
        elif command == "review-status":
            if "--wait" in argv:
                self.assertEqual(self.status.read()["status"], "waiting")
                self.assertEqual(self.status.read()["installer_pid"], os.getpid())
                self.waits += 1
                if self.waits >= 2:
                    self.review = False
            payload = {"status": "waiting" if self.review else "ok",
                       "next_action": "enrich" if self.enrich else "review_linkedin" if self.review else "realize"}
        elif command == "review":
            payload = {"status": "reused", "url": "http://127.0.0.1:8765/?stage=linkedin"}
        elif command == "realize":
            self.write(self.people, self.csv)
            payload["profiles_missing"] = int(self.profiles_missing)
        elif command == "profile-prefetch":
            if "--fetch" in argv:
                self.profiles_missing = False
            else:
                payload = {"status": "dry_run", "estimated_rapidapi_calls": 1}
        elif command == "build_processing_pipeline.py":
            self.assertIn("--dry-run", argv)
            payload = {"status": "dry_run", "estimated_cost_usd": 0.5,
                       "estimated_paid_calls": {"role_enrichment": 1}}
        elif command == "linkedin_modal_pipeline.py":
            self.assertTrue("index-people" in argv or "download" in argv)
            self.write(self.index / "local-search.duckdb", "synthetic index")
            self.write(self.index / "manifest.json", '{"status":"ok"}')
            if "download" in argv:
                return subprocess.CompletedProcess(argv, 0, "downloaded native artifacts\n")
        elif command == "validate_search_index.py":
            payload = {"status": self.validation_status, "total_people": 1,
                       "summary": "Search index ready: 1 people searchable."}
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload))

    def run_pipeline(self, *spend, upload=False):
        return ProcessingOnboarding(self.root, approved_spend=spend, approve_upload=upload).run()

    def did(self, stage):
        return any(argv[0].endswith("bin/deep-context") and argv[1] == stage
                   and "--dry-run" not in argv for argv in self.commands)

    def indexed(self):
        return any("packs/indexing/modal/linkedin_modal_pipeline.py" in argv and "index-people" in argv
                   for argv in self.commands)

    def test_first_paid_stage_stops_with_estimate_and_exact_scoped_resume(self):
        self.synthesize = True
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
        self.run_pipeline()
        self.commands.clear()
        result = self.run_pipeline("synthesize")
        self.assertTrue(self.did("synthesize"))
        self.assertFalse(self.did("collect"))
        self.assertFalse(self.did("cluster"))
        self.assertEqual(result["action"]["step"], "cluster")
        self.assertEqual(result["action"]["continue_command"], self.retry + " --approve-spend cluster")
        self.assertNotIn("--approve-spend", result["retry_command"])

    def test_enrichment_stops_before_any_provider_without_approval(self):
        self.enrich = True
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["action"]["step"]), ("enrich", "enrich"))
        self.assertFalse(self.did("enrich"))
        self.assertFalse(self.indexed())

    def test_index_requires_both_spend_and_upload_authorization(self):
        for spend, upload in [((), False), (("index",), False), ((), True)]:
            self.commands.clear()
            result = self.run_pipeline(*spend, upload=upload)
            self.assertEqual((result["step"], result["status"]), ("index", "waiting"))
            self.assertEqual(result["action"]["step"], "index")
            self.assertTrue(result["action"]["upload"])
            self.assertEqual(result["action"]["continue_command"], self.retry + " --approve-spend index --approve-upload")
            self.assertFalse(self.indexed())

    def test_full_approved_run_uses_real_commands_then_only_validator_marks_ready(self):
        self.synthesize = self.cluster = self.enrich = True
        result = self.run_pipeline("synthesize", "cluster", "enrich", "index", upload=True)
        self.assertTrue(self.did("synthesize"))
        self.assertTrue(self.did("cluster"))
        self.assertTrue(self.did("enrich"))
        self.assertTrue(self.indexed())
        self.assertEqual((result["step"], result["status"], result["person_count"]), ("ready", "completed", 1))
        self.assertNotIn("review", result["plan"])
        self.assertEqual(result["retry_command"], self.retry)
        self.assertEqual(Path(self.commands[-1][5]).name, "validate_search_index.py")

    def test_completed_native_index_resumes_without_reupload_and_revalidates(self):
        self.run_pipeline("index", upload=True)
        self.commands.clear()
        result = self.run_pipeline()
        self.assertFalse(self.indexed())
        self.assertFalse(self.did("collect"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(Path(self.commands[-1][5]).name, "validate_search_index.py")
        self.commands.clear()
        self.assertEqual(self.run_pipeline()["status"], "completed")
        self.assertFalse(self.indexed())

    def test_invalid_existing_index_fails_until_explicit_rebuild(self):
        self.run_pipeline("index", upload=True)
        self.validation_status = "fail"
        self.commands.clear()
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["status"]), ("validate", "failed"))
        self.assertFalse(self.indexed())
        self.validation_status = "ok"
        self.commands.clear()
        result = self.run_pipeline("index", upload=True)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(self.indexed())

    def test_roster_change_prevents_old_index_reuse(self):
        self.run_pipeline("index", upload=True)
        self.csv = "id,full_name\nsynthetic-casey,Casey Example\n"
        self.commands.clear()
        result = self.run_pipeline()
        self.assertEqual((result["step"], result["status"]), ("index", "waiting"))
        self.assertFalse(self.indexed())

    def test_review_timeout_keeps_waiting_until_native_decisions_change(self):
        self.review = True
        result = self.run_pipeline("index", upload=True)
        self.assertEqual(self.waits, 2)
        review = next(argv for argv in self.commands if len(argv) > 1 and argv[1] == "review")
        self.assertEqual(review[-3:], ["--port", "8765", "--open"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps"]["review"]["status"], "completed")
        self.assertIn("review", result["plan"])
        self.assertEqual(result["plan"].index("review") + 1, result["plan"].index("index"))

    def test_native_failure_logs_stderr_and_stops_before_paid_work(self):
        self.failing_command = "collect"
        result = self.run_pipeline("synthesize", "index", upload=True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("source read failed", self.status.log_path.read_text())
        self.assertFalse(self.did("synthesize"))
        self.assertFalse(self.indexed())

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
        self.commands.clear()
        self.run_pipeline()
        self.assertFalse(self.did("seed"))

    def test_missing_profile_fetch_is_scoped_enrichment_approval(self):
        self.profiles_missing = True
        result = self.run_pipeline("index", upload=True)
        self.assertEqual(result["action"]["step"], "enrich")
        self.assertIn("profile-prefetch --fetch", result["action"]["command"])
        self.assertFalse(self.indexed())
        self.commands.clear()
        result = self.run_pipeline("enrich", "index", upload=True)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(any("--fetch" in argv for argv in self.commands))

    def test_saved_wacli_store_reaches_native_readiness_and_collection(self):
        store = self.root / "synthetic-whatsapp-store"
        self.status.write(step=InstallStep.DEEP_CONTEXT, status=InstallState.WAITING,
                          message="Ready", pid=0, retry_command=self.retry + f" --wacli-store {store}")
        self.run_pipeline()
        for stage in ("check", "collect"):
            argv = next(argv for argv in self.commands if argv[1] == stage)
            flag = argv.index("--wacli-db")
            self.assertEqual(argv[flag + 1], str(store / "wacli.db"))

    def test_review_reuses_requested_installation_port(self):
        self.review = True
        result = ProcessingOnboarding(self.root, approved_spend=("index",),
                                      approve_upload=True, port=8899).run()
        self.assertEqual(result["status"], "completed")
        review = next(argv for argv in self.commands if argv[1] == "review")
        self.assertEqual(review[-3:], ["--port", "8899", "--open"])

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
        download = next(argv for argv in self.commands if "download" in argv)
        self.assertEqual(download[-5:], ["--label", "gmail-index", "--wait", "--dest", ".powerpacks/search-index"])
        self.assertEqual(Path(self.commands[-1][5]).name, "validate_search_index.py")

    def test_changed_roster_never_redispatches_unknown_previous_job_even_when_approved(self):
        self.dispatch()
        self.csv = "id,full_name\nsynthetic-casey,Casey Example\n"
        result = self.run_pipeline("index", upload=True)
        self.assertEqual((result["step"], result["status"]), ("index", "waiting"))
        self.assertEqual(result["action"]["kind"], "recovery")
        self.assertEqual(result["installer_pid"], 0)
        self.assertFalse(self.indexed())
        self.assertFalse(any("download" in argv for argv in self.commands))

    def test_finished_remote_job_with_interrupted_download_is_recovered(self):
        self.dispatch()
        path = self.root / ".powerpacks/runs/setup-gmail-modal/status.json"
        record = json.loads(path.read_text())
        record["status"] = "completed"
        path.write_text(json.dumps(record))
        result = self.run_pipeline()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(any("download" in argv for argv in self.commands))
        self.assertFalse(self.indexed())

    def test_validator_failure_never_claims_ready(self):
        self.validation_status = "fail"
        result = self.run_pipeline("index", upload=True)
        self.assertEqual((result["step"], result["status"]), ("validate", "failed"))
        self.assertNotIn("ready", result["steps"])


if __name__ == "__main__":
    unittest.main()

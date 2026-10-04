"""Real processing command outcomes advance the existing install manifest."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.powerset.primitives.install import progress

from packs.powerset.primitives.install.progress import PROCESSING_COMMANDS, run_with_progress
from packs.ingestion.primitives.deep_context.db.models import EnrichRun, EnrichRunStatus, ParentRow
from packs.ingestion.primitives.deep_context.db.readiness import CANONICAL_DB
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.workflow_views import enrichment_work, workflow_state
from tests.deep_context_sqlite_test_helpers import seed_identity
from packs.powerset.primitives.install.status import InstallState, InstallStatus, InstallStep


class ProcessingProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.status = InstallStatus(self.root)

    def start(self):
        self.status.write(step=InstallStep.DEEP_CONTEXT, status=InstallState.WAITING,
                          message="Ready", pid=os.getpid(), account_email="casey@example.com")

    def test_normal_commands_without_install_page_do_not_create_status(self):
        self.assertEqual(run_with_progress(self.root, InstallStep.DEEP_CONTEXT, "Work", ["collect"], lambda: 0), 0)
        self.assertFalse(self.status.manifest_path.exists())

    def test_native_command_keeps_coordinator_resume_choices(self):
        command = "bin/onboard --gmail-email casey@example.com --sync-after 2025-10-03 --port 8899"
        self.status.write(step=InstallStep.DEEP_CONTEXT, status=InstallState.WAITING,
                          message="Ready", pid=0, retry_command=command)
        run_with_progress(self.root, InstallStep.DEEP_CONTEXT, "Collect", ["bin/deep-context", "collect"],
                          lambda: 0, complete=False)
        self.assertEqual(self.status.read()["retry_command"], command)

    def test_partial_processing_stays_waiting_and_keeps_account(self):
        self.start()
        def run():
            self.assertEqual(self.status.read()["status"], "running")
            return 0
        run_with_progress(self.root, InstallStep.DEEP_CONTEXT, "Reading messages", ["collect"], run, complete=False)
        result = self.status.read()
        self.assertEqual((result["step"], result["status"]), ("deep_context", "waiting"))
        self.assertEqual(result["account_email"], "casey@example.com")

    def test_only_index_and_validator_can_finish_readiness(self):
        self.start()
        for step, following in [(InstallStep.INDEX, "validate"),
                                (InstallStep.VALIDATE, "ready")]:
            run_with_progress(self.root, step, "Working", ["command"], lambda: 0)
            result = self.status.read()
            self.assertEqual(result["steps"][step]["status"], "completed")
            self.assertEqual(result["step"], following)
            self.assertEqual(result["status"], "completed" if following == "ready" else "waiting")

    def test_failure_and_user_action_never_mark_complete(self):
        for code, state in [(1, "failed"), (10, "waiting"), (20, "waiting")]:
            self.start()
            self.assertEqual(run_with_progress(self.root, InstallStep.INDEX, "Build", ["index", "--max-usd", "15"], lambda: code), code)
            result = self.status.read()
            self.assertEqual((result["step"], result["status"]), ("index", state))
            self.assertEqual(result["retry_command"], "index --max-usd 15")

    def test_exception_records_failure_and_preserves_artifacts(self):
        self.start()
        data = self.root / "contacts.csv"
        data.write_text("existing data")
        def run():
            raise RuntimeError("Disconnected")
        with self.assertRaisesRegex(RuntimeError, "Disconnected"):
            run_with_progress(self.root, InstallStep.INDEX, "Build", ["index"], run)
        self.assertEqual(self.status.read()["status"], "failed")
        self.assertEqual(data.read_text(), "existing data")


    def db(self):
        return Db(self.root / CANONICAL_DB)

    def pending_review(self):
        db = self.db()
        seed_identity(db, parent_id="parent", person_id="person", row_key="candidate:person",
                      name="Jordan Bravo", machine_worth="yes",
                      linkedin_url="https://www.linkedin.com/in/jordan-bravo")
        db.record_enrich_run(EnrichRun(EnrichRunStatus.COMPLETED, "synthetic",
                                       unfinished=enrichment_work(db)))
        self.assertEqual(workflow_state(db).next_action, "review_linkedin")
        return db

    def test_every_processing_command_has_one_stage(self):
        for command in ("ensure-parents", "seed", "collect", "synthesize", "compose", "cluster", "parents"):
            self.assertIs(PROCESSING_COMMANDS[command][0], InstallStep.DEEP_CONTEXT)
        for command in ("enrich", "finish-reviews", "assemble-synthetic", "profile-prefetch", "reconcile-deep-research"):
            self.assertIs(PROCESSING_COMMANDS[command][0], InstallStep.ENRICH)
        for command in ("review", "review-status"):
            self.assertIs(PROCESSING_COMMANDS[command][0], InstallStep.REVIEW)
        self.assertIs(PROCESSING_COMMANDS["realize"][0], InstallStep.INDEX)

    def test_enrichment_skips_review_when_sqlite_has_no_queue(self):
        self.start()
        self.db().project_rows((ParentRow("parent", "parent", "Jordan Bravo", "jordan-bravo"),))
        run_with_progress(self.root, InstallStep.ENRICH, "Enrich", ["enrich"], lambda: 0, complete=False)
        result = self.status.read()
        self.assertEqual((result["step"], result["status"]), ("index", "waiting"))
        self.assertEqual(result["steps"]["deep_context"]["status"], "completed")
        self.assertEqual(result["steps"]["enrich"]["status"], "completed")
        self.assertNotIn("review", result["plan"])
        self.assertNotIn("review", result["steps"])

    def test_unfinished_enrichment_stays_waiting_in_enrich(self):
        self.start()
        db = self.db()
        db.record_enrich_run(EnrichRun(EnrichRunStatus.RUNNING, "profiles"))
        run_with_progress(self.root, InstallStep.ENRICH, "Enrich", ["enrich"], lambda: 0, complete=False)
        result = self.status.read()
        self.assertEqual((result["step"], result["status"]), ("enrich", "waiting"))
        self.assertNotIn("review", result["plan"])
        self.assertNotIn("index", result["steps"])

    def test_review_wait_and_completion_read_actual_sqlite_queue(self):
        self.start()
        db = self.pending_review()
        run_with_progress(self.root, InstallStep.ENRICH, "Enrich", ["enrich"], lambda: 0, complete=False)
        result = self.status.read()
        self.assertEqual(result["plan"][-6:], ["deep_context", "enrich", "review", "index", "validate", "ready"])
        self.assertEqual((result["step"], result["status"]), ("review", "waiting"))
        self.assertEqual(result["action"]["kind"], "review")
        def wait():
            self.assertEqual((self.status.read()["step"], self.status.read()["status"]), ("review", "waiting"))
            return 0
        run_with_progress(self.root, InstallStep.REVIEW, "Review", ["review-status", "--wait"], wait, complete=False)
        self.assertEqual(self.status.read()["steps"]["review"]["status"], "waiting")
        def decide():
            db.decide_identity("candidate:person", "verify")
            return 0
        run_with_progress(self.root, InstallStep.REVIEW, "Review", ["review-status", "--wait"], decide, complete=False)
        result = self.status.read()
        self.assertEqual(result["steps"]["review"]["status"], "completed")
        self.assertEqual((result["step"], result["status"]), ("index", "waiting"))
        self.assertIsNone(result["action"])

    def test_opening_review_without_a_queue_does_not_change_install_status(self):
        self.start()
        self.db().project_rows((ParentRow("parent", "parent", "Jordan Bravo", "jordan-bravo"),))
        before = self.status.manifest_path.read_bytes()
        run_with_progress(self.root, InstallStep.REVIEW, "Review", ["review"], lambda: 0, complete=False)
        self.assertEqual(self.status.manifest_path.read_bytes(), before)

    def test_realize_only_prepares_index(self):
        self.start()
        run_with_progress(self.root, InstallStep.INDEX, "Prepare", ["realize"], lambda: 0, complete=False)
        result = self.status.read()
        self.assertEqual((result["step"], result["status"]), ("index", "waiting"))
        self.assertNotIn("ready", result["steps"])


    def test_reopening_review_after_ready_preserves_completed_stages(self):
        self.start()
        db = self.pending_review()
        db.decide_identity("candidate:person", "verify")
        self.status.write(step=InstallStep.REVIEW, status=InstallState.COMPLETED,
                          message="Done", pid=os.getpid())
        self.status.write(step=InstallStep.READY, status=InstallState.COMPLETED,
                          message="Ready", pid=os.getpid())
        before = self.status.manifest_path.read_bytes()
        run_with_progress(self.root, InstallStep.REVIEW, "Review", ["review"], lambda: 0, complete=False)
        self.assertEqual(self.status.manifest_path.read_bytes(), before)

    def test_actual_offline_review_status_wait_closes_review_without_claiming_ready(self):
        self.start()
        db = self.pending_review()
        run_with_progress(self.root, InstallStep.ENRICH, "Enrich", ["enrich"], lambda: 0, complete=False)
        db.decide_identity("candidate:person", "verify")
        result = subprocess.run([
            sys.executable, "-m", "packs.powerset.primitives.install.progress", "--command", "review-status", "--",
            "packs.ingestion.primitives.deep_context.review.reconcile_review_web", "status", "--wait", "--timeout", "1",
        ], cwd=self.root, env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            text=True, capture_output=True, check=True)
        self.assertIn('"next_action": "realize"', result.stdout)
        state = self.status.read()
        self.assertEqual((state["step"], state["status"]), ("index", "waiting"))
        self.assertEqual(state["steps"]["review"]["status"], "completed")
        self.assertNotIn("ready", state["steps"])


    def test_help_dry_run_and_default_profile_preview_leave_manifest_untouched(self):
        self.start()
        before = self.status.manifest_path.read_bytes()
        for command, arguments in (("enrich", ["--dry-run"]), ("enrich", ["--help"]),
                                   ("profile-prefetch", []), ("lookup", [])):
            with self.subTest(command=command, arguments=arguments), patch("sys.argv", [
                "progress", "--command", command, "--", "example.module", *arguments,
            ]), patch.object(progress.Path, "cwd", return_value=self.root), patch.object(progress.runpy, "run_module") as run:
                with self.assertRaises(SystemExit) as stopped:
                    progress.main()
                self.assertEqual(stopped.exception.code, 0)
                run.assert_called_once_with("example.module", run_name="__main__", alter_sys=True)
                self.assertEqual(self.status.manifest_path.read_bytes(), before)

    def test_finishing_identity_work_closes_existing_review_only_when_queue_resolves(self):
        self.start()
        db = self.pending_review()
        run_with_progress(self.root, InstallStep.ENRICH, "Enrich", ["enrich"], lambda: 0, complete=False)
        def finish():
            db.decide_identity("candidate:person", "verify")
            return 0
        run_with_progress(self.root, InstallStep.ENRICH, "Finish", ["finish-reviews"], finish, complete=False)
        result = self.status.read()
        self.assertEqual(result["steps"]["review"]["status"], "completed")
        self.assertEqual((result["step"], result["status"]), ("index", "waiting"))

"""Real processing command outcomes advance the existing install manifest."""
import os
import tempfile
import unittest
from pathlib import Path

from packs.powerset.primitives.install.progress import run_with_progress
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

    def test_partial_processing_stays_waiting_and_keeps_account(self):
        self.start()
        def run():
            self.assertEqual(self.status.read()["status"], "running")
            return 0
        run_with_progress(self.root, InstallStep.DEEP_CONTEXT, "Reading messages", ["collect"], run, complete=False)
        result = self.status.read()
        self.assertEqual((result["step"], result["status"]), ("deep_context", "waiting"))
        self.assertEqual(result["account_email"], "casey@example.com")

    def test_realize_index_and_validation_finish_only_in_order(self):
        self.start()
        for step, following in [(InstallStep.DEEP_CONTEXT, "index"), (InstallStep.INDEX, "validate"),
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

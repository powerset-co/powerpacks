"""The real coordinator orders existing workflows without touching user state."""
import contextlib
import fcntl
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from packs.powerset.primitives.install.onboard import main
from packs.powerset.primitives.install.status import InstallState, InstallStatus, InstallStep
from packs.powerset.primitives.install.workflow import SourceOnboarding


class InstallCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.status = InstallStatus(self.root)
        self.retry = (f"{self.root}/bin/onboard --source gmail --gmail-email casey@example.com"
                      " --sync-after 2025-10-03 --harness codex --port 8899")
        self.status.write(step=InstallStep.DEEP_CONTEXT, status=InstallState.WAITING,
                          message="Ready", pid=0, retry_command=self.retry)
        self.events = []
        self.account_code = 0
        self.account_email = "powerset@example.com"
        self.source_choices = []
        self.source_result = {"step": "deep_context", "status": "waiting", "action": {"kind": "processing"}}
        self.page = patch("packs.ingestion.primitives.deep_context.review.cli.start_server",
                          return_value={"url": "http://127.0.0.1:8899/install"})
        self.launch_page = self.page.start()
        self.account = patch("packs.powerset.primitives.install.onboard.Onboarding")
        self.account_class = self.account.start()
        self.account_class.side_effect = self.build_account
        self.sources = patch.object(SourceOnboarding, "run", autospec=True, side_effect=self.run_sources)
        self.run_source = self.sources.start()
        self.processing = patch("packs.powerset.primitives.install.pipeline.ProcessingOnboarding")
        self.processing_class = self.processing.start()
        self.processing_class.return_value.run.side_effect = self.run_processing
        self.addCleanup(patch.stopall)

    def build_account(self, root, *, harnesses, pid, retry_command):
        def run():
            self.events.append("account")
            state = {0: InstallState.COMPLETED, 1: InstallState.FAILED, 10: InstallState.WAITING}[self.account_code]
            self.status.write(step=InstallStep.NETWORK, status=state,
                              message="Account checked", pid=os.getpid(), retry_command=retry_command,
                              account_email=self.account_email)
            return self.account_code
        return SimpleNamespace(run=run, email=self.account_email)

    def run_sources(self, flow):
        self.events.append("imports")
        self.source_choices.append((flow.sources, flow.gmail_emails, flow.sync_after,
                                    flow.wacli_store, flow.skip_sources))
        self.status.write(step=InstallStep(self.source_result["step"]),
                          status=InstallState(self.source_result["status"]), message="Source checked",
                          pid=0, retry_command=flow.retry_command, plan=flow.plan,
                          action=self.source_result.get("action"))
        return self.source_result

    def run_processing(self):
        self.events.append("processing")
        return {"step": "ready", "status": "completed", "message": "Verified"}

    def run_command(self, *arguments):
        output = io.StringIO()
        with patch.object(sys, "argv", ["bin/onboard", "--root", str(self.root), *arguments]), \
                contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
            main()
        return result.exception.code, output.getvalue()

    def test_resume_keeps_choices_and_orders_account_imports_processing(self):
        code, output = self.run_command()
        self.assertEqual(code, 0)
        self.assertEqual(self.events, ["account", "imports", "processing"])
        self.assertIn("STATUS PAGE: http://127.0.0.1:8899/install", output)
        self.assertIn("Powerset search is ready; local setup will continue.", output)
        self.assertTrue(output.rstrip().endswith("DONE: Verified"))
        self.assertEqual(self.status.read()["retry_command"], self.retry)
        self.assertEqual(self.launch_page.call_args.kwargs["port"], 8899)
        self.assertEqual(self.processing_class.call_args.kwargs["port"], 8899)

    def test_approval_is_scoped_to_this_resume_not_saved_for_future_runs(self):
        self.run_command("--approve-spend", "index", "--approve-upload")
        self.assertEqual(self.processing_class.call_args.kwargs["approved_spend"], ("index",))
        self.assertTrue(self.processing_class.call_args.kwargs["approve_upload"])
        self.assertNotIn("approve", self.status.read()["retry_command"])

    def test_source_wait_does_not_start_processing(self):
        self.source_result = {"step": "gmail_login", "status": "waiting", "message": "Connect Gmail"}
        code, _ = self.run_command()
        self.assertEqual(code, 10)
        self.assertEqual(self.events, ["account", "imports"])
        self.processing_class.assert_not_called()

    def test_failed_account_does_not_run_imports_or_processing(self):
        self.account_code = 1
        code, _ = self.run_command()
        self.assertEqual(code, 1)
        self.assertEqual(self.events, ["account"])

    def test_account_wait_and_failure_resume_with_saved_source_choices(self):
        for account_code in (10, 1):
            with self.subTest(account_code=account_code):
                self.events.clear()
                self.account_code = account_code
                self.account_email = ""
                code, _ = self.run_command()
                self.assertEqual(code, account_code)
                self.assertEqual(self.events, ["account"])
                self.assertEqual(self.status.read()["retry_command"], self.retry)
                self.events.clear()
                self.account_code = 0
                self.account_email = "powerset@example.com"
                code, output = self.run_command()
                self.assertEqual(code, 0)
                self.assertEqual(self.events, ["account", "imports", "processing"])
                self.assertTrue(output.rstrip().endswith("DONE: Verified"))
                self.assertEqual(self.source_choices[-1][:3], (("gmail",), ("casey@example.com",), "2025-10-03"))
                self.assertEqual(self.account_class.call_args.kwargs["harnesses"], ["codex"])
                self.assertEqual(self.processing_class.call_args.kwargs["port"], 8899)

    def test_each_source_wait_and_failure_resumes_through_same_coordinator(self):
        self.retry = (f"{self.root}/bin/onboard --source linkedin --source gmail --source imessage"
                      " --source whatsapp --gmail-email casey@example.com --sync-after 2025-10-03"
                      f" --wacli-store {self.root}/synthetic-whatsapp --harness pi --port 8899")
        self.status.write(step=InstallStep.SOURCES, status=InstallState.WAITING,
                          message="Ready", pid=0, retry_command=self.retry)
        steps = ("gmail_tools", "gmail_login", "gmail_sync", "gmail_import", "imessage_access",
                 "imessage_import", "whatsapp_tools", "whatsapp_login", "whatsapp_sync", "whatsapp_import",
                 "linkedin")
        for step in steps:
            for state, expected_code in (("waiting", 10), ("failed", 1)):
                with self.subTest(step=step, state=state):
                    self.events.clear()
                    self.source_result = {"step": step, "status": state, "message": "Synthetic source pause"}
                    code, _ = self.run_command()
                    self.assertEqual(code, expected_code)
                    self.assertEqual(self.events, ["account", "imports"])
                    choices = self.source_choices[-1]
                    self.assertEqual(self.status.read()["retry_command"], self.retry)
                    self.events.clear()
                    self.source_result = {"step": "deep_context", "status": "waiting", "action": {"kind": "processing"}}
                    code, output = self.run_command()
                    self.assertEqual(code, 0)
                    self.assertEqual(self.events, ["account", "imports", "processing"])
                    self.assertEqual(self.source_choices[-1], choices)
                    self.assertEqual(choices[:3], (("linkedin", "gmail", "imessage", "whatsapp"),
                                                  ("casey@example.com",), "2025-10-03"))
                    self.assertEqual(choices[3], self.root / "synthetic-whatsapp")
                    self.assertEqual(self.account_class.call_args.kwargs["harnesses"], ["pi"])
                    self.assertEqual(self.processing_class.call_args.kwargs["port"], 8899)
                    self.assertTrue(output.rstrip().endswith("DONE: Verified"))

    def test_existing_coordinator_rejects_duplicate_before_side_effects(self):
        with (self.status.directory / "onboard.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, output = self.run_command()
        self.assertEqual(code, 10)
        self.assertIn("already running", output)
        self.assertEqual(self.events, [])
        self.launch_page.assert_not_called()


if __name__ == "__main__":
    unittest.main()

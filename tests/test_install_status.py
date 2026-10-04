"""Installer progress reflects completion, human waits, and interrupted work."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.powerset.primitives.install.status import InstallState, InstallStatus, InstallStep


class InstallStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.status = InstallStatus(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_install_waits_without_animating(self) -> None:
        self.assertEqual(self.status.read()["status"], "waiting")

    def test_invalid_manifest_has_actionable_failure(self) -> None:
        self.status.directory.mkdir(parents=True)
        self.status.manifest_path.write_text("broken json")
        self.assertEqual(self.status.read()["status"], "failed")
        self.assertIn("rerun", self.status.read()["message"])

    def test_running_install_is_read_from_atomic_manifest(self) -> None:
        self.status.write(step=InstallStep.DEPENDENCIES, status=InstallState.RUNNING,
                          message="Installing dependencies", pid=os.getpid())
        record = self.status.read()
        self.assertEqual(record["primitive"], "powerpacks_install")
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["step"], "dependencies")
        self.assertEqual(record["retry_command"], "bin/bootstrap")
        self.assertEqual(record["log_path"], str(self.root / ".powerpacks/install/install.log"))
        self.assertFalse((self.root / ".powerpacks/install/manifest.tmp").exists())

    def test_dead_installer_is_paused_with_resume_guidance(self) -> None:
        self.status.write(step=InstallStep.DEPENDENCIES, status=InstallState.RUNNING,
                          message="Installing dependencies", pid=99999999)
        record = self.status.read()
        self.assertEqual(record["status"], "waiting")
        self.assertEqual(record["installer_pid"], 0)
        self.assertEqual(record["action"]["command"], "bin/bootstrap")

    def test_human_wait_survives_installer_exit(self) -> None:
        self.status.write(step=InstallStep.TOOLS, status=InstallState.WAITING,
                          message="Approve the free Gmail tools in chat", pid=99999999,
                          retry_command="bin/bootstrap --tools")
        record = self.status.read()
        self.assertEqual(record["status"], "waiting")
        self.assertEqual(record["retry_command"], "bin/bootstrap --tools")

    def test_interrupted_browser_login_does_not_wait_forever(self) -> None:
        self.status.write(step=InstallStep.ACCOUNT, status=InstallState.WAITING,
                          message="Waiting for sign-in", pid=99999999)
        record = self.status.read()
        self.assertEqual(record["status"], "waiting")
        self.assertEqual(record["steps"]["account"]["status"], "waiting")
        self.assertEqual(record["action"]["kind"], "resume")

    def test_live_source_wait_is_interrupted_when_its_owner_dies(self) -> None:
        for step in (InstallStep.GMAIL_LOGIN, InstallStep.IMESSAGE_ACCESS):
            with self.subTest(step=step):
                self.status.write(step=step, status=InstallState.WAITING,
                                  message="Waiting for access", pid=99999999)
                self.assertEqual(self.status.read()["action"]["kind"], "resume")

    def test_missing_configuration_wait_has_no_running_owner(self) -> None:
        self.status.write(step=InstallStep.GMAIL_LOGIN, status=InstallState.WAITING,
                          message="Set up Gmail access", pid=0, action={"kind": "gmail"})
        self.assertEqual(self.status.read()["status"], "waiting")

    def test_empty_network_wait_remains_actionable_after_installer_exit(self) -> None:
        self.status.write(step=InstallStep.NETWORK, status=InstallState.WAITING,
                          message="Choose another account or connect contacts", pid=99999999)
        self.assertEqual(self.status.read()["status"], "waiting")

    def test_completed_install_survives_installer_exit(self) -> None:
        self.status.write(step=InstallStep.READY, status=InstallState.COMPLETED,
                          message="Powerpacks is installed", pid=99999999)
        self.assertEqual(self.status.read()["status"], "completed")

    def test_failed_replacement_keeps_previous_readable_manifest(self) -> None:
        self.status.write(step=InstallStep.RUNTIME, status=InstallState.RUNNING,
                          message="Preparing Python", pid=os.getpid())
        with patch("pathlib.Path.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.status.write(step=InstallStep.READY, status=InstallState.COMPLETED,
                                  message="Powerpacks is installed", pid=os.getpid())
        self.assertEqual(self.status.read()["step"], "runtime")

    def test_steps_keep_skipped_login_and_verified_account_through_completion(self) -> None:
        self.status.write(step=InstallStep.RUNTIME, status=InstallState.RUNNING,
                          message="Preparing your Mac", pid=os.getpid())
        self.status.write(step=InstallStep.ACCOUNT, status=InstallState.SKIPPED,
                          message="Already signed in", pid=os.getpid(), account_email="casey@example.com")
        self.status.write(step=InstallStep.NETWORK, status=InstallState.RUNNING,
                          message="Checking your network", pid=os.getpid())
        self.status.write(step=InstallStep.READY, status=InstallState.COMPLETED,
                          message="Ready", pid=os.getpid(), network_name="Personal Network", person_count=4)
        record = self.status.read()
        self.assertEqual(record["steps"]["runtime"]["status"], "completed")
        self.assertEqual(record["steps"]["account"]["status"], "skipped")
        self.assertEqual(record["steps"]["network"]["status"], "completed")
        self.assertEqual(record["account_email"], "casey@example.com")
        self.assertEqual(record["person_count"], 4)

    def test_retry_clears_previous_identity_and_does_not_claim_unchecked_steps(self) -> None:
        self.status.write(step=InstallStep.NETWORK, status=InstallState.WAITING,
                          message="Empty network", pid=os.getpid(), account_email="casey@example.com",
                          network_name="Personal Network", person_count=0)
        self.status.write(step=InstallStep.RUNTIME, status=InstallState.RUNNING,
                          message="Preparing your Mac", pid=os.getpid())
        record = self.status.read()
        self.assertEqual(list(record["steps"]), ["runtime"])
        self.assertIsNone(record["account_email"])
        self.assertIsNone(record["person_count"])

    def test_source_wait_keeps_install_history_and_clears_action_on_resume(self) -> None:
        self.status.write(step=InstallStep.SKILLS, status=InstallState.COMPLETED,
                          message="Installed", pid=os.getpid())
        plan = ["skills", "sources", "whatsapp_login", "whatsapp_sync"]
        self.status.write(step=InstallStep.WHATSAPP_LOGIN, status=InstallState.WAITING,
                          message="Scan with WhatsApp", pid=os.getpid(), plan=plan,
                          action={"kind": "qr"})
        self.assertEqual(self.status.read()["action"], {"kind": "qr"})
        self.status.write(step=InstallStep.WHATSAPP_SYNC, status=InstallState.RUNNING,
                          message="Syncing", pid=os.getpid())
        self.assertIsNone(self.status.read()["action"])
        self.assertEqual(self.status.read()["plan"], plan)
        self.assertEqual(self.status.read()["steps"]["skills"]["status"], "completed")

    def test_waiting_step_does_not_advance_to_completed_on_failure(self) -> None:
        self.status.write(step=InstallStep.ACCOUNT, status=InstallState.WAITING,
                          message="Waiting for sign-in", pid=os.getpid())
        self.status.write(step=InstallStep.ACCOUNT, status=InstallState.FAILED,
                          message="Sign-in timed out", pid=os.getpid())
        record = self.status.read()
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["steps"]["account"]["status"], "failed")


    def test_processing_transition_does_not_claim_previous_run_completed(self) -> None:
        self.status.write(step=InstallStep.ENRICH, status=InstallState.RUNNING,
                          message="Enriching", pid=os.getpid())
        self.status.write(step=InstallStep.INDEX, status=InstallState.WAITING,
                          message="Ready", pid=os.getpid())
        self.assertEqual(self.status.read()["steps"]["enrich"]["status"], "running")

    def test_source_restart_clears_every_processing_stage_including_review(self) -> None:
        for step in (InstallStep.DEEP_CONTEXT, InstallStep.ENRICH, InstallStep.REVIEW,
                     InstallStep.INDEX, InstallStep.VALIDATE, InstallStep.READY):
            self.status.write(step=step, status=InstallState.COMPLETED,
                              message="Done", pid=os.getpid())
        self.status.write(step=InstallStep.SOURCES, status=InstallState.WAITING,
                          message="Choose sources", pid=os.getpid())
        self.assertEqual(list(self.status.read()["steps"]), ["sources"])


if __name__ == "__main__":
    unittest.main()

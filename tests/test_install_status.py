"""Installer progress reflects completion, human waits, and interrupted work."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.powerset.primitives.install.status import InstallStatus
from packs.powerset.primitives.install.steps import InstallStep


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
        self.assertEqual(self.status.read()["event"], "install.unreadable")

    def test_running_install_is_read_from_atomic_manifest(self) -> None:
        self.status.write('tools.preparing', step=InstallStep.DEPENDENCIES, pid=os.getpid())
        record = self.status.read()
        self.assertEqual(record["primitive"], "powerpacks_install")
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["step"], "dependencies")
        self.assertEqual(record["retry_command"], "bin/bootstrap")
        self.assertEqual(record["log_path"], str(self.root / ".powerpacks/install/install.log"))
        self.assertFalse((self.root / ".powerpacks/install/manifest.tmp").exists())

    def test_dead_installer_is_paused_with_resume_guidance(self) -> None:
        self.status.write('tools.preparing', step=InstallStep.DEPENDENCIES, pid=99999999)
        record = self.status.read()
        self.assertEqual(record["status"], "waiting")
        self.assertEqual(record["installer_pid"], 0)
        self.assertEqual(record["action"]["command"], "bin/bootstrap")

    def test_human_wait_survives_installer_exit(self) -> None:
        self.status.write('step.waiting', step=InstallStep.GMAIL_TOOLS, pid=99999999, retry_command="bin/onboard --source gmail")
        record = self.status.read()
        self.assertEqual(record["status"], "waiting")
        self.assertEqual(record["retry_command"], "bin/onboard --source gmail")

    def test_interrupted_browser_login_does_not_wait_forever(self) -> None:
        self.status.write('account.signing_in', pid=99999999)
        record = self.status.read()
        self.assertEqual(record["status"], "waiting")
        self.assertEqual(record["steps"]["account"]["status"], "waiting")
        self.assertEqual(record["action"]["kind"], "resume")

    def test_live_source_wait_is_interrupted_when_its_owner_dies(self) -> None:
        for step in (InstallStep.GMAIL_LOGIN, InstallStep.IMESSAGE_ACCESS):
            with self.subTest(step=step):
                self.status.write('step.waiting', step=step, pid=99999999)
                self.assertEqual(self.status.read()["action"]["kind"], "resume")

    def test_missing_configuration_wait_has_no_running_owner(self) -> None:
        self.status.write('gmail.which_accounts', pid=0)
        self.assertEqual(self.status.read()["status"], "waiting")

    def test_empty_network_wait_remains_actionable_after_installer_exit(self) -> None:
        self.status.write('step.waiting', step=InstallStep.NETWORK, pid=99999999)
        self.assertEqual(self.status.read()["status"], "waiting")

    def test_completed_install_survives_installer_exit(self) -> None:
        self.status.write('install.local_only_done', pid=99999999)
        self.assertEqual(self.status.read()["status"], "completed")

    def test_failed_replacement_keeps_previous_readable_manifest(self) -> None:
        self.status.write('install.preparing_mac', pid=os.getpid())
        with patch("pathlib.Path.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.status.write('install.local_only_done', pid=os.getpid())
        self.assertEqual(self.status.read()["step"], "runtime")

    def test_steps_keep_skipped_login_and_verified_account_through_completion(self) -> None:
        self.status.write('install.preparing_mac', pid=os.getpid())
        self.status.write('source.skipped', step=InstallStep.ACCOUNT, pid=os.getpid(), account_email="casey@example.com")
        self.status.write('network.checking', pid=os.getpid())
        self.status.write('install.local_only_done', pid=os.getpid(), network_name="Personal Network", person_count=4)
        record = self.status.read()
        self.assertEqual(record["steps"]["runtime"]["status"], "completed")
        self.assertEqual(record["steps"]["account"]["status"], "skipped")
        self.assertEqual(record["steps"]["network"]["status"], "completed")
        self.assertEqual(record["account_email"], "casey@example.com")
        self.assertEqual(record["person_count"], 4)

    def test_retry_clears_previous_identity_and_does_not_claim_unchecked_steps(self) -> None:
        self.status.write('step.waiting', step=InstallStep.NETWORK, pid=os.getpid(), account_email="casey@example.com", network_name="Personal Network", person_count=0)
        self.status.write('install.preparing_mac', pid=os.getpid())
        record = self.status.read()
        self.assertEqual(list(record["steps"]), ["runtime"])
        self.assertIsNone(record["account_email"])
        self.assertIsNone(record["person_count"])

    def test_source_wait_keeps_install_history_and_clears_action_on_resume(self) -> None:
        self.status.write('install.skills_ready', pid=os.getpid())
        plan = ["skills", "sources", "whatsapp_login", "whatsapp_sync"]
        self.status.write('whatsapp.qr', pid=os.getpid(), plan=plan)
        self.assertEqual(self.status.read()["action"], {"kind": "qr"})
        self.status.write('whatsapp.downloading', pid=os.getpid())
        self.assertIsNone(self.status.read()["action"])
        self.assertEqual(self.status.read()["plan"], plan)
        self.assertEqual(self.status.read()["steps"]["skills"]["status"], "completed")

    def test_waiting_step_does_not_advance_to_completed_on_failure(self) -> None:
        self.status.write('account.signing_in', pid=os.getpid())
        self.status.write('account.login_failed', pid=os.getpid())
        record = self.status.read()
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["steps"]["account"]["status"], "failed")


    def test_processing_transition_does_not_claim_previous_run_completed(self) -> None:
        self.status.write('enrich.running', pid=os.getpid())
        self.status.write('step.waiting', step=InstallStep.INDEX, pid=os.getpid())
        self.assertEqual(self.status.read()["steps"]["enrich"]["status"], "running")

    def test_source_restart_clears_every_processing_stage_including_review(self) -> None:
        for step in (InstallStep.DEEP_CONTEXT, InstallStep.ENRICH, InstallStep.REVIEW,
                     InstallStep.INDEX, InstallStep.VALIDATE, InstallStep.READY):
            self.status.write('tools.ready', step=step, pid=os.getpid())
        self.status.write('step.waiting', step=InstallStep.SOURCES, pid=os.getpid())
        self.assertEqual(list(self.status.read()["steps"]), ["sources"])


    def test_every_write_takes_its_words_and_state_from_the_script(self) -> None:
        record = self.status.write("gmail.connect", pid=os.getpid(), email="casey@example.com",
                                   details={"email": "casey@example.com"})
        self.assertEqual((record["event"], record["step"], record["status"]), ("gmail.connect", "gmail_login", "running"))
        self.assertEqual(record["message"], "Connecting casey@example.com")
        self.assertEqual(record["action"], {"kind": "details", "details": {"email": "casey@example.com"}})
        record = self.status.write("gmail.connect.waiting", pid=os.getpid(), details={"email": "casey@example.com"})
        self.assertEqual(record["status"], "waiting")
        self.assertIn("I’ll continue here", record["note"])
        self.assertEqual(record["action"], {"kind": "gmail", "details": {"email": "casey@example.com"}})
        self.assertEqual(self.status.read()["prose"]["rows"][1]["label"], "Logging in to your accounts")

    def test_an_event_without_its_own_step_lands_on_the_current_one(self) -> None:
        self.status.write("gmail.syncing", pid=os.getpid())
        self.assertEqual(self.status.write("step.failed", pid=os.getpid())["step"], "gmail_sync")


if __name__ == "__main__":
    unittest.main()

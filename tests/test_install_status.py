"""Installer progress reflects completion, human waits, and interrupted work."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from packs.powerset.primitives.install import controller
from packs.powerset.primitives.install.controller import InstallController
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

    def test_a_step_keeps_when_it_started_across_its_events(self) -> None:
        first = self.status.write("discover.learning", pid=os.getpid())
        self.assertTrue(first["step_started_at"])
        with patch("packs.powerset.primitives.install.status.now_iso", return_value="2099-01-01T00:00:00Z"):
            later = self.status.write("discover.learning.count", pid=os.getpid(), done=3, total=9)
            self.assertEqual(later["step_started_at"], first["step_started_at"])
            self.assertEqual(later["message"], "Learning about your contacts: 3 of 9")
            moved = self.status.write("enrich.running", pid=os.getpid())
        self.assertEqual(moved["step_started_at"], "2099-01-01T00:00:00Z")
        self.assertEqual(self.status.read()["step_started_at"], "2099-01-01T00:00:00Z")

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

    def test_source_restart_clears_every_processing_stage(self) -> None:
        for step in (InstallStep.DEEP_CONTEXT, InstallStep.ENRICH, InstallStep.INDEX, InstallStep.VALIDATE,
                     InstallStep.READY):
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


class ForgetLinkedInTests(unittest.TestCase):
    """`forget_linkedin` moves the sign-in profile, the connections file and its record aside."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "root"
        self.profile = Path(self.tmp.name) / "browser-profiles" / "linkedin"
        self.profile.mkdir(parents=True)
        (self.profile / "Default").mkdir()
        csv = self.root / controller.CONNECTIONS_CSV
        csv.parent.mkdir(parents=True)
        csv.write_text("First Name,Last Name\n", encoding="utf-8")
        (self.root / controller.SCRAPE_RECORD).write_text("{}", encoding="utf-8")

    def test_moves_each_path_to_a_backup_and_deletes_nothing(self) -> None:
        with patch.object(controller, "BROWSER_PROFILE", self.profile):
            payload = InstallController(self.root).forget_linkedin()
        self.assertEqual(payload["status"], "forgotten")
        self.assertEqual(len(payload["moved"]), 3)
        self.assertFalse(self.profile.exists())
        self.assertFalse((self.root / controller.CONNECTIONS_CSV).exists())
        self.assertFalse((self.root / controller.SCRAPE_RECORD).exists())
        for moved in payload["moved"]:
            self.assertIn(".bkup-", moved)
            self.assertTrue(Path(moved).exists())
        self.assertTrue((Path(payload["moved"][0]) / "Default").is_dir())

    def test_page_image_is_served_once_the_read_took_one(self) -> None:
        install = InstallController(self.root)
        self.assertIsNone(install.linkedin_page_image())
        (self.root / controller.PAGE_SHOT).write_bytes(b"PNG")
        self.assertEqual(install.linkedin_page_image(), b"PNG")
        with patch.object(controller, "BROWSER_PROFILE", self.profile):
            payload = install.forget_linkedin()
        self.assertEqual(len(payload["moved"]), 4)
        self.assertIsNone(install.linkedin_page_image())

    def test_first_look_reads_the_indexed_people_then_the_linkedin_import(self) -> None:
        install = InstallController(self.root)
        self.assertEqual(install.first_look(), {"people": [], "total": 0})
        header = "public_identifier,full_name,current_title,current_company,headline,profile_picture_url\n"
        linkedin = self.root / controller.LINKEDIN_PEOPLE
        linkedin.parent.mkdir(parents=True, exist_ok=True)
        linkedin.write_text(header + "a-b,A B,CTO,Acme,,http://p/a\nc-d,C D,,,,\ne-f,E F,Founder,Beta,,\n", encoding="utf-8")
        look = install.first_look(limit=1)
        self.assertEqual(look["total"], 3)
        self.assertEqual([person["name"] for person in look["people"]], ["A B"])
        self.assertEqual(look["people"][0]["avatar_url"], "http://p/a")
        indexed = self.root / controller.INDEXED_PEOPLE
        indexed.parent.mkdir(parents=True, exist_ok=True)
        indexed.write_text(header + "g-h,G H,VP,Gamma,,\n", encoding="utf-8")
        self.assertEqual([person["name"] for person in install.first_look()["people"]], ["G H"])

    def test_nothing_to_move_is_fine(self) -> None:
        with patch.object(controller, "BROWSER_PROFILE", self.profile / "absent"):
            payload = InstallController(Path(self.tmp.name) / "empty").forget_linkedin()
        self.assertEqual(payload, {"status": "forgotten", "moved": []})


class ConnectTests(unittest.TestCase):
    """The Connect screen signs in to one source before setup, in the background, into its own record."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.controller = InstallController(self.root)

    def test_connect_runs_the_sources_logins_only_in_the_background(self) -> None:
        with patch("packs.powerset.primitives.install.controller.subprocess.Popen") as popen:
            self.assertEqual(self.controller.connect("gmail", [" owner@gmail.com ", "work@example.com", "owner@gmail.com"]),
                             {"status": "started", "source": "gmail"})
        self.assertEqual(popen.call_args.args[0][1:], [
            "-m", "packs.powerset.primitives.install.onboard", "--root", str(self.root),
            "--source", "gmail", "--logins-only",
            "--gmail-email", "owner@gmail.com", "--gmail-email", "work@example.com"])
        self.assertEqual(popen.call_args.kwargs["cwd"], self.root)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_connect_answers_once_the_run_has_written_its_own_status(self) -> None:
        record = InstallStatus(self.root, "connect")
        record.write("whatsapp.failed", pid=1, retry_command="bin/onboard --source whatsapp --logins-only")
        # A live pid: a read reports a running record whose process is gone as paused.
        run = MagicMock(pid=os.getpid())
        run.poll.return_value = None

        def first_write(_seconds: float) -> None:
            record.write("whatsapp.checking", pid=run.pid, retry_command="bin/onboard --source whatsapp --logins-only")

        with patch("packs.powerset.primitives.install.controller.subprocess.Popen", return_value=run), \
                patch("packs.powerset.primitives.install.controller.time.sleep", side_effect=first_write) as sleep:
            self.controller.connect("whatsapp")
        sleep.assert_called_once()
        self.assertEqual(self.controller.connection()["installer_pid"], run.pid)

    def test_connect_takes_only_a_source_with_a_login(self) -> None:
        with patch("packs.powerset.primitives.install.controller.subprocess.Popen") as popen:
            for source in ("imessage", "powerset", None):
                with self.assertRaises(ValueError):
                    self.controller.connect(source, None)
            for emails in ([], ["not-an-address"], "owner@gmail.com"):
                with self.assertRaises(ValueError):
                    self.controller.connect("gmail", emails)
        popen.assert_not_called()

    def test_the_connect_record_carries_its_qr_and_leaves_setups_alone(self) -> None:
        InstallStatus(self.root, "connect").write("whatsapp.qr", pid=os.getpid(),
                                                  retry_command="bin/onboard --source whatsapp --logins-only")
        qr = self.root / ".powerpacks/messages/wacli-login-qr.png"
        qr.parent.mkdir(parents=True)
        qr.write_bytes(b"png")
        self.assertTrue(self.controller.connection()["action"]["qr_url"].startswith("/api/install/qr?connect=1&t="))
        self.assertEqual(self.controller.qr_image(connect=True), b"png")
        self.assertIsNone(self.controller.qr_image())
        self.assertFalse((self.root / ".powerpacks/install/manifest.json").exists())

if __name__ == "__main__":
    unittest.main()

"""Import tools prepare binaries without login, syncing, or source data changes."""
from __future__ import annotations

import unittest
import os
from unittest.mock import patch

from packs.ingestion.primitives.discover.messages.wacli import binary
from packs.ingestion.primitives.discover.messages.wacli.runtime import PrimitiveBlocked
from packs.ingestion.primitives.setup.automations import msgvault_home, shell
from packs.powerset.primitives.install import tools


class ImportToolsTests(unittest.TestCase):
    def setUp(self):
        browser = patch.object(tools, "ensure_chromium", return_value={"status": "ok", "installed": False, "node_path": "/n"})
        self.ensure_chromium = browser.start()
        self.addCleanup(browser.stop)

    @patch.dict(os.environ, {"POWERPACKS_DESKTOP": "1"})
    @patch.object(msgvault_home, "ensure_msgvault", return_value={"installed": True})
    def test_gmail_and_linkedin_get_the_powerpacks_browser(self, _msgvault):
        for source in ("gmail", "linkedin"):
            self.assertEqual(tools.ImportTools(sources=(source,)).run()["status"], "ok")
        self.assertEqual(self.ensure_chromium.call_count, 2)

    @patch.dict(os.environ, {"POWERPACKS_DESKTOP": "1"})
    def test_browser_download_failure_fails_the_step(self):
        self.ensure_chromium.return_value = {"status": "error", "message": "Powerpacks could not download its browser: offline"}
        result = tools.ImportTools(sources=("linkedin",)).run()
        self.assertEqual(result["status"], "failed")
        self.assertIn("offline", result["message"])

    def test_tool_discovery_keeps_the_selected_python_first(self):
        with patch.dict(os.environ, {"PATH": "/selected/python/bin:/usr/bin"}):
            tools.ImportTools(sources=()).run()
            self.assertEqual(os.environ["PATH"].split(os.pathsep)[0], "/selected/python/bin")

    @patch.dict(os.environ, {"POWERPACKS_DESKTOP": "1"})
    @patch.object(shell, "run_command")
    @patch.object(msgvault_home, "ensure_msgvault", return_value={"installed": True})
    @patch("shutil.which", side_effect=lambda name: "/fixture/tmux" if name == "tmux" else None)
    @patch("pathlib.Path.is_dir", return_value=True)
    def test_desktop_skips_node_browser_gcloud_brew(self, _directory, _which, msgvault, run):
        self.assertEqual(tools.ImportTools(sources=("gmail", "linkedin")).run()["status"], "ok")
        run.assert_not_called()
        msgvault.assert_called_once_with(install=True)
        self.assertIn(str(tools.Path.home() / ".powerpacks/google-cloud-sdk/bin"), os.environ["PATH"].split(os.pathsep))
        self.assertEqual(os.environ["CLOUDSDK_PYTHON"], tools.sys.executable)

    @patch.dict(os.environ, {"POWERPACKS_DESKTOP": "1"})
    @patch("shutil.which", return_value=None)
    def test_desktop_needs_no_homebrew(self, _which):
        result = tools.ImportTools(sources=("linkedin",)).run()
        self.assertEqual(result["status"], "ok")

    @patch.object(shell, "run_command")
    @patch.object(msgvault_home, "ensure_msgvault", return_value={"installed": True})
    @patch.object(binary, "ensure_wacli_report", return_value={"status": "ok", "action": "current"})
    @patch("shutil.which", return_value="/fixture/bin/tool")
    @patch("pathlib.Path.is_dir", return_value=True)
    def test_rerun_does_not_install_current_tools(self, _directory, _which, wacli, msgvault, run):
        for _ in range(2):
            self.assertEqual(tools.ImportTools(sources=("gmail", "whatsapp")).run()["status"], "ok")
        run.assert_not_called()
        self.assertEqual(msgvault.call_count, 2)
        self.assertEqual(wacli.call_count, 2)

    @patch.object(shell, "run_command")
    @patch.object(msgvault_home, "ensure_msgvault")
    @patch.object(binary, "ensure_wacli_report", return_value={"status": "ok", "action": "current"})
    @patch("shutil.which", return_value="/fixture/bin/tool")
    def test_whatsapp_needs_no_gmail_browser_or_auth_tools(self, _which, _wacli, msgvault, run):
        self.assertEqual(tools.ImportTools(sources=("whatsapp",)).run()["status"], "ok")
        msgvault.assert_not_called()
        run.assert_not_called()
        self.ensure_chromium.assert_not_called()

    @patch.object(binary, "ensure_wacli_report", side_effect=PrimitiveBlocked({"message": "Failed to download wacli"}))
    @patch("shutil.which", return_value="/fixture/bin/tool")
    def test_interrupted_wacli_download_is_failure_not_human_action(self, _which, _wacli):
        result = tools.ImportTools(sources=("whatsapp",)).run()
        self.assertEqual(result["status"], "failed")
        self.assertIn("Failed to download", result["message"])

    @patch.object(binary, "ensure_wacli_report", return_value={"status": "ok", "action": "current"})
    @patch("shutil.which", return_value=None)
    def test_missing_homebrew_is_a_password_action(self, _which, _wacli):
        result = tools.ImportTools(sources=("whatsapp",)).run()
        self.assertEqual(result["status"], "needs_user_action")
        self.assertIn("password", result["message"])
        self.assertIn("raw.githubusercontent.com/Homebrew/install/HEAD/install.sh", result["command"])
        self.assertNotIn("Install it", result["message"])

    @patch.object(shell, "run_command", return_value=shell.CommandResult(ok=True))
    @patch.object(msgvault_home, "ensure_msgvault", return_value={"installed": True})
    @patch("shutil.which", side_effect=lambda name: None if name in {"node", "npm"} else "/fixture/bin/" + name)
    @patch("pathlib.Path.is_dir", return_value=True)
    def test_node_and_npm_use_one_install(self, _directory, _which, _msgvault, run):
        self.assertEqual(tools.ImportTools(sources=("gmail",)).run()["status"], "ok")
        run.assert_called_once_with(["/fixture/bin/brew", "install", "node"], timeout=900)

    @patch.dict(os.environ, {"POWERPACKS_DESKTOP": "1"})
    @patch.object(shell, "run_command")
    @patch.object(binary, "ensure_wacli_report", return_value={"status": "ok", "action": "current"})
    @patch("shutil.which", side_effect=lambda name: "/fixture/bin/brew" if name == "brew" else None)
    def test_whatsapp_on_the_desktop_app_needs_no_homebrew(self, _which, _wacli, run):
        # The QR code is drawn in Python (wacli/qr.py): nothing is installed for it, Homebrew or not.
        self.assertEqual(tools.ImportTools(sources=("whatsapp",)).run()["status"], "ok")
        run.assert_not_called()

    @patch.object(shell, "run_command", return_value=shell.CommandResult(ok=False, stderr="download interrupted"))
    @patch.object(binary, "ensure_wacli_report", return_value={"status": "ok", "action": "current"})
    @patch("shutil.which", side_effect=lambda name: None if name == "tmux" else "/fixture/bin/" + name)
    def test_brew_download_failure_does_not_send_user_to_terminal(self, _which, _wacli, _run):
        result = tools.ImportTools(sources=("whatsapp",)).run()
        self.assertEqual(result["status"], "failed")
        self.assertIn("download interrupted", result["message"])


if __name__ == "__main__":
    unittest.main()
